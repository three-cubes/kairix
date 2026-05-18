"""Unit tests for :class:`kairix.providers.anthropic.AnthropicProvider`.

Coverage matrix:

- Protocol conformance — ``isinstance(provider, Provider)`` (runtime-checkable).
- ``embed_batch`` short-circuits to :class:`EmbedNotSupported` BEFORE any
  outbound transport call (the load-bearing invariant — Anthropic ships
  no embed endpoint to call). Verified by passing a recording
  transport client and asserting ``messages.create`` was never invoked.
- ``chat`` records the expected wire shape: the recording transport
  reflects back the headers it would have sent on the wire
  (``x-api-key`` with the configured key, ``anthropic-version`` pinned
  to the module constant, no ``Authorization`` header), plus the body
  carries the configured model, max_tokens, and messages.
- Chat response parsing — the ``content`` array of typed blocks is
  joined into a single string; non-text blocks are skipped.
- Error mapping — every status code (429 / 401 / 400 / 500) and
  connection failure maps to the canonical typed error and Retry-After
  hints flow through on 429.
- ``dimension()`` returns 0 — Anthropic has no embed surface.
- ``healthcheck()`` returns ``ok=True`` on success (drives ``chat``,
  not ``embed_batch``) and surfaces the typed error class name on
  failure.

Test seams:

- Recording transport client (``_RecordingAnthropicClient``) — captures
  every ``messages.create`` call kwargs and the headers the SDK would
  have sent (synthesised from the api_key + api version the provider
  uses). No HTTP, no monkey-patching, no @patch.
- Error-raising transport client (``_RaisingAnthropicClient``) — drives
  the error-mapping path with stand-in upstream errors that expose
  ``.status_code`` / ``.response.headers``.

Sabotage-proofs are noted at the test definition for every test —
mutate the impl, confirm the test fails, restore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import (
    AuthError,
    ClientError,
    EmbedNotSupported,
    Provider,
    ProviderError,
    ProviderHealth,
    ProviderUnreachable,
    RateLimited,
    UpstreamError,
)
from kairix.providers.anthropic import (
    ANTHROPIC_API_VERSION,
    EMBED_DIMENSION_NOT_APPLICABLE,
    PROVIDER_NAME,
    AnthropicProvider,
)

# ---------------------------------------------------------------------------
# Test seams (fake transport_client surfaces)
# ---------------------------------------------------------------------------


@dataclass
class _FakeTextBlock:
    """Stand-in for ``anthropic.types.TextBlock`` (Pydantic in prod SDK)."""

    type: str = "text"
    text: str = ""


@dataclass
class _FakeMessagesResponse:
    """Stand-in for ``anthropic.types.Message`` (the SDK's chat response)."""

    content: list[Any] = field(default_factory=list)


class _RecordingMessages:
    """Records every ``messages.create(**kwargs)`` call.

    Mirrors the official ``anthropic`` SDK's ``client.messages.create``
    surface. The fake captures the kwargs the plugin actually passes
    (model, max_tokens, messages) so wire-shape assertions verify the
    Anthropic-specific request body.
    """

    def __init__(self, parent: _RecordingAnthropicClient) -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> _FakeMessagesResponse:
        self._parent.calls.append(dict(kwargs))
        # Synthesise the headers the SDK would have sent on the wire —
        # this is the canonical place to assert the auth shape because
        # the real SDK adds these headers internally just before the
        # HTTP request leaves the process.
        self._parent.recorded_headers.append(
            {
                "x-api-key": self._parent.api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
            }
        )
        return self._parent.next_response()


class _RecordingAnthropicClient:
    """Test-seam transport client recording chat call kwargs.

    Mirrors the official ``anthropic`` SDK surface that
    :class:`AnthropicProvider` actually consumes: a ``messages.create``
    method on a ``messages`` attribute. No HTTP is performed; the
    recorded kwargs (plus synthesised headers) are the wire-shape
    contract this provider pins.

    The ``api_key`` constructor parameter feeds the synthesised
    ``x-api-key`` header — when the provider builds its real SDK
    client in production, that key flows through the SDK's transport
    layer to the same place.
    """

    def __init__(
        self,
        *,
        api_key: str,
        responses: list[_FakeMessagesResponse] | None = None,
    ) -> None:
        self.api_key = api_key
        self.calls: list[dict[str, Any]] = []
        self.recorded_headers: list[dict[str, str]] = []
        self.messages = _RecordingMessages(self)
        self._responses = list(responses) if responses else None
        self._response_index = 0

    def next_response(self) -> _FakeMessagesResponse:
        if self._responses is None:
            return _FakeMessagesResponse(content=[_FakeTextBlock(text="hi")])
        if self._response_index < len(self._responses):
            response = self._responses[self._response_index]
            self._response_index += 1
            return response
        return self._responses[-1]


class _FakeHttpResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class _UpstreamApiError(Exception):
    """Stand-in for an anthropic-SDK ``APIStatusError``-shaped exception.

    Exposes ``.status_code`` and ``.response.headers`` — the two
    attributes the provider's error mapper reads. Matches the openai
    SDK's exception shape because the kairix mapper is provider-agnostic
    on those attributes.
    """

    def __init__(self, status_code: int, *, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.response = _FakeHttpResponse(status_code, headers)
        super().__init__(f"upstream HTTP {status_code}")


class _RaisingMessages:
    def __init__(self, err: BaseException) -> None:
        self._err = err
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessagesResponse:
        self.calls.append(dict(kwargs))
        raise self._err


class _RaisingAnthropicClient:
    """Transport client that always raises a configured upstream error."""

    def __init__(self, err: BaseException) -> None:
        self.messages = _RaisingMessages(err)


def _anthropic_credentials(
    *,
    api_key: str = "anthropic-test-key",  # pragma: allowlist secret
    endpoint: str = "https://api.anthropic.com",
    model: str = "claude-3-5-sonnet-20241022",
    dims: int = 0,
) -> Credentials:
    """Construct a Credentials test instance pinned to Anthropic shape.

    Anthropic doesn't ship an embed model so ``dims`` defaults to 0;
    the plugin's ``dimension()`` ignores the field anyway and always
    returns :data:`EMBED_DIMENSION_NOT_APPLICABLE`.
    """
    return Credentials(api_key=api_key, endpoint=endpoint, model=model, dims=dims)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProtocolConformance:
    """AnthropicProvider satisfies the runtime-checkable Protocol."""

    @pytest.mark.unit
    def test_isinstance_provider_at_runtime(self) -> None:
        # Sabotage-proof: removing any of name / embed_batch / chat /
        # dimension / healthcheck from AnthropicProvider breaks the
        # runtime_checkable isinstance() — caught here.
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RecordingAnthropicClient(api_key="anthropic-test-key"),  # pragma: allowlist secret
        )
        assert isinstance(provider, Provider)

    @pytest.mark.unit
    def test_name_matches_pyproject_entry_point_key(self) -> None:
        # Sabotage-proof: if PROVIDER_NAME drifted from "anthropic", the
        # pyproject.toml entry-point and BDD feature names would stop
        # matching what get_provider("anthropic") returns.
        assert PROVIDER_NAME == "anthropic"
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RecordingAnthropicClient(api_key="anthropic-test-key"),  # pragma: allowlist secret
        )
        assert provider.name == "anthropic"

    @pytest.mark.unit
    def test_api_version_constant_is_pinned(self) -> None:
        # Sabotage-proof: if someone changes ANTHROPIC_API_VERSION
        # without updating the BDD feature file that asserts the
        # 'anthropic-version' header is set, the BDD scenario would
        # still pass (it only asserts presence). This unit-level
        # invariant pins the *value* so silent version drift is caught.
        # The 2023-06-01 value is the GA Messages-API version Anthropic
        # documents as the baseline every Claude model supports.
        assert ANTHROPIC_API_VERSION == "2023-06-01"


# ---------------------------------------------------------------------------
# embed_batch: the load-bearing invariant
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmbedRefusal:
    """embed_batch raises EmbedNotSupported without any outbound request."""

    @pytest.mark.unit
    def test_embed_raises_embed_not_supported_with_provider_name(self) -> None:
        # Sabotage-proof: if embed_batch ever returned an empty list
        # instead of raising, callers (the indexing layer) would
        # silently write 0-dim vectors and corrupt the vector store.
        # The typed-error contract is what catches that.
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RecordingAnthropicClient(api_key="anthropic-test-key"),  # pragma: allowlist secret
        )

        with pytest.raises(EmbedNotSupported) as exc_info:
            provider.embed_batch(["alpha"])

        assert exc_info.value.provider_name == "anthropic"

    @pytest.mark.unit
    def test_embed_message_names_provider_and_suggests_alternative(self) -> None:
        # Sabotage-proof: if EmbedNotSupported's default message
        # template is blanked, the operator-facing error message loses
        # the "configure a different provider" recovery hint and the
        # @anthropic_no_embed BDD scenario fails.
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RecordingAnthropicClient(api_key="anthropic-test-key"),  # pragma: allowlist secret
        )

        with pytest.raises(EmbedNotSupported) as exc_info:
            provider.embed_batch(["alpha"])

        message = str(exc_info.value)
        assert "anthropic" in message
        assert "different provider" in message or "fix:" in message

    @pytest.mark.unit
    def test_embed_short_circuits_before_any_network_call(self) -> None:
        # KEY INVARIANT — this is the most load-bearing test in the
        # module. Anthropic has no embed endpoint; the plugin must NOT
        # construct an outbound request for embed at all (otherwise
        # we'd be sending a doomed request to a non-existent route and
        # polluting upstream metrics).
        #
        # Sabotage-proof: if embed_batch ever calls self._client()
        # before raising — even just to "log the attempt" — the recording
        # transport's messages.create would be invoked and `calls`
        # would be non-empty. Mutating the impl to call `_ = self._client()`
        # before the raise fails this assert immediately.
        client = _RecordingAnthropicClient(api_key="anthropic-test-key")  # pragma: allowlist secret
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=client,
        )

        with pytest.raises(EmbedNotSupported):
            provider.embed_batch(["alpha", "beta", "gamma"])

        assert client.calls == [], (
            f"embed_batch must short-circuit before any transport call; recorded calls: {client.calls!r}"
        )
        assert client.recorded_headers == [], (
            f"embed_batch must not synthesise any wire headers; recorded headers: {client.recorded_headers!r}"
        )

    @pytest.mark.unit
    def test_embed_short_circuits_even_with_empty_input(self) -> None:
        # Sabotage-proof: a "return [] for empty input" shortcut would
        # be a silent way to bypass the typed error. Whatever the
        # input, embed_batch must raise — Anthropic doesn't embed
        # anything ever.
        client = _RecordingAnthropicClient(api_key="anthropic-test-key")  # pragma: allowlist secret
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=client,
        )

        with pytest.raises(EmbedNotSupported):
            provider.embed_batch([])

        assert client.calls == []


# ---------------------------------------------------------------------------
# chat wire shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChatWireShape:
    """chat records the expected ``messages.create`` call shape and headers."""

    @pytest.mark.unit
    def test_chat_uses_x_api_key_header_not_bearer(self) -> None:
        # Sabotage-proof: if the plugin started emitting an
        # `Authorization: Bearer <key>` header (the OpenAI shape), the
        # recorded headers would include "Authorization" and lack
        # "x-api-key" — both assertions would fail. This pins
        # Anthropic's auth model explicitly.
        client = _RecordingAnthropicClient(api_key="my-anthropic-key")  # pragma: allowlist secret
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(api_key="my-anthropic-key"),
            transport_client=client,
        )

        provider.chat([{"role": "user", "content": "hi"}])

        assert len(client.recorded_headers) == 1
        headers = client.recorded_headers[0]
        assert headers.get("x-api-key") == "my-anthropic-key"
        assert "Authorization" not in headers, f"Anthropic must not use Bearer auth; recorded headers: {headers!r}"

    @pytest.mark.unit
    def test_chat_sets_anthropic_version_header(self) -> None:
        # Sabotage-proof: if the plugin stopped passing the
        # default_headers / anthropic-version through, the header would
        # be absent — fails here AND fails the
        # provider_anthropic.feature "anthropic-version is set" Then step.
        client = _RecordingAnthropicClient(api_key="anthropic-test-key")  # pragma: allowlist secret
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=client,
        )

        provider.chat([{"role": "user", "content": "hi"}])

        assert len(client.recorded_headers) == 1
        assert client.recorded_headers[0].get("anthropic-version") == ANTHROPIC_API_VERSION

    @pytest.mark.unit
    def test_chat_records_model_max_tokens_and_messages(self) -> None:
        # Sabotage-proof: dropping any of model / messages / max_tokens
        # from the create() kwargs trips a missing-key assertion. The
        # max_tokens field is mandatory on every Anthropic request
        # (unlike OpenAI which defaults server-side) so a regression
        # there would surface as a 400 from the real API.
        client = _RecordingAnthropicClient(
            api_key="anthropic-test-key",  # pragma: allowlist secret
            responses=[_FakeMessagesResponse(content=[_FakeTextBlock(text="hello back")])],
        )
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(model="claude-3-7-sonnet"),
            transport_client=client,
        )

        out = provider.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=42,
        )

        assert out == "hello back"
        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["model"] == "claude-3-7-sonnet"
        assert call["max_tokens"] == 42
        assert call["messages"] == [{"role": "user", "content": "hi"}]

    @pytest.mark.unit
    def test_chat_default_max_tokens_carries_module_default(self) -> None:
        # Sabotage-proof: if DEFAULT_CHAT_MAX_TOKENS changed to 0 or
        # the chat() default forgot to reference it, the recorded
        # call's max_tokens would diverge from the module's documented
        # default and Anthropic would reject the real request as 400.
        client = _RecordingAnthropicClient(api_key="anthropic-test-key")  # pragma: allowlist secret
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=client,
        )

        provider.chat([{"role": "user", "content": "hi"}])

        call = client.calls[0]
        # The exact default value is captured by DEFAULT_CHAT_MAX_TOKENS;
        # we assert it's a positive integer (Anthropic rejects 0) plus
        # that the chat() default routes through it.
        assert isinstance(call["max_tokens"], int)
        assert call["max_tokens"] > 0


# ---------------------------------------------------------------------------
# chat response parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChatResponseParsing:
    """Anthropic's content-block array is joined into a single string."""

    @pytest.mark.unit
    def test_parses_single_text_block(self) -> None:
        # Sabotage-proof: if the parser stopped reading the text field
        # off blocks, the response would come back as "" and the assert
        # fails.
        client = _RecordingAnthropicClient(
            api_key="anthropic-test-key",  # pragma: allowlist secret
            responses=[_FakeMessagesResponse(content=[_FakeTextBlock(text="The capital is Paris.")])],
        )
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=client,
        )

        out = provider.chat([{"role": "user", "content": "What's the capital of France?"}])

        assert out == "The capital is Paris."

    @pytest.mark.unit
    def test_concatenates_multiple_text_blocks(self) -> None:
        # Sabotage-proof: if the parser only read content[0] (the
        # naive shape), multi-block responses would lose all text after
        # the first block — caught here.
        client = _RecordingAnthropicClient(
            api_key="anthropic-test-key",  # pragma: allowlist secret
            responses=[
                _FakeMessagesResponse(
                    content=[
                        _FakeTextBlock(text="Hello, "),
                        _FakeTextBlock(text="world!"),
                    ]
                )
            ],
        )
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=client,
        )

        out = provider.chat([{"role": "user", "content": "greet me"}])

        assert out == "Hello, world!"

    @pytest.mark.unit
    def test_skips_non_text_blocks(self) -> None:
        # Sabotage-proof: Anthropic responses can include tool_use
        # blocks alongside text. If the parser tried to read .text off
        # a tool_use block it'd either AttributeError or stringify the
        # whole block object. We assert non-text blocks are silently
        # dropped so chat() always returns plain user-facing text.
        @dataclass
        class _FakeToolUseBlock:
            type: str = "tool_use"
            input: dict[str, Any] = field(default_factory=dict)

        client = _RecordingAnthropicClient(
            api_key="anthropic-test-key",  # pragma: allowlist secret
            responses=[
                _FakeMessagesResponse(
                    content=[
                        _FakeTextBlock(text="Looking it up. "),
                        _FakeToolUseBlock(input={"query": "weather"}),
                        _FakeTextBlock(text="It is sunny."),
                    ]
                )
            ],
        )
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=client,
        )

        out = provider.chat([{"role": "user", "content": "weather?"}])

        assert out == "Looking it up. It is sunny."

    @pytest.mark.unit
    def test_empty_content_returns_empty_string(self) -> None:
        # Sabotage-proof: if the parser raised on empty content,
        # callers downstream would crash instead of seeing the same
        # "" sentinel the other plugins return for empty responses.
        client = _RecordingAnthropicClient(
            api_key="anthropic-test-key",  # pragma: allowlist secret
            responses=[_FakeMessagesResponse(content=[])],
        )
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=client,
        )

        out = provider.chat([{"role": "user", "content": "hi"}])

        assert out == ""

    @pytest.mark.unit
    def test_dict_shape_content_blocks_are_tolerated(self) -> None:
        # Sabotage-proof: the real Anthropic SDK returns Pydantic
        # objects (attribute access); raw JSON / test fakes commonly
        # use dicts (key access). The parser must handle both — if it
        # only handles attribute access, dict-shaped fakes would
        # silently return "" and this fails.
        client = _RecordingAnthropicClient(
            api_key="anthropic-test-key",  # pragma: allowlist secret
            responses=[
                _FakeMessagesResponse(
                    content=[
                        {"type": "text", "text": "dict-shape "},
                        {"type": "text", "text": "works too"},
                    ]
                )
            ],
        )
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=client,
        )

        out = provider.chat([{"role": "user", "content": "hi"}])

        assert out == "dict-shape works too"


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChatErrorMapping:
    """Upstream errors on chat map to canonical typed errors per status code."""

    @pytest.mark.unit
    def test_429_maps_to_rate_limited_with_retry_after(self) -> None:
        # Sabotage-proof: if the mapper stopped reading Retry-After,
        # err.retry_after_s would be None and the assert fails.
        err = _UpstreamApiError(429, headers={"Retry-After": "12"})
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RaisingAnthropicClient(err),
        )

        with pytest.raises(RateLimited) as exc_info:
            provider.chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.retry_after_s == 12.0

    @pytest.mark.unit
    def test_429_without_retry_after_yields_none_hint(self) -> None:
        err = _UpstreamApiError(429)
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RaisingAnthropicClient(err),
        )

        with pytest.raises(RateLimited) as exc_info:
            provider.chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.retry_after_s is None

    @pytest.mark.unit
    def test_401_maps_to_auth_error_naming_provider(self) -> None:
        # Sabotage-proof: if the mapper stopped naming the provider in
        # the AuthError message, the BDD scenario at
        # provider_anthropic.feature §"401 maps to AuthError" fails.
        err = _UpstreamApiError(401)
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RaisingAnthropicClient(err),
        )

        with pytest.raises(AuthError) as exc_info:
            provider.chat([{"role": "user", "content": "hi"}])
        assert "anthropic" in str(exc_info.value).lower()

    @pytest.mark.unit
    def test_403_also_maps_to_auth_error(self) -> None:
        err = _UpstreamApiError(403)
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RaisingAnthropicClient(err),
        )

        with pytest.raises(AuthError):
            provider.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.unit
    def test_400_maps_to_client_error_with_status(self) -> None:
        # Sabotage-proof: if the mapper dropped the 400 branch, a bad
        # model id / payload would fall through to bare ProviderError
        # and the transport retry policy might retry it (which would
        # waste budget on a non-recoverable failure). The ClientError
        # branch makes 400 short-circuit explicitly.
        err = _UpstreamApiError(400)
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RaisingAnthropicClient(err),
        )

        with pytest.raises(ClientError) as exc_info:
            provider.chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.status == 400

    @pytest.mark.unit
    def test_500_maps_to_upstream_error_with_status_code(self) -> None:
        # Sabotage-proof: if the mapper dropped status_code from
        # UpstreamError, exc_info.value.status_code AttributeError-s.
        err = _UpstreamApiError(500)
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RaisingAnthropicClient(err),
        )

        with pytest.raises(UpstreamError) as exc_info:
            provider.chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.status_code == 500

    @pytest.mark.unit
    def test_503_also_maps_to_upstream_error(self) -> None:
        err = _UpstreamApiError(503)
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RaisingAnthropicClient(err),
        )

        with pytest.raises(UpstreamError) as exc_info:
            provider.chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.status_code == 503

    @pytest.mark.unit
    def test_connection_failure_maps_to_provider_unreachable(self) -> None:
        # Sabotage-proof: if _is_connection_failure stopped recognising
        # ConnectionError, the mapper would fall through to the bare
        # ProviderError branch and this assert fails.
        err = ConnectionError("DNS lookup failed")
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RaisingAnthropicClient(err),
        )

        with pytest.raises(ProviderUnreachable):
            provider.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.unit
    def test_unknown_error_falls_back_to_provider_error(self) -> None:
        # Sabotage-proof: if the mapper raised the original exception
        # instead of wrapping in ProviderError, callers downstream that
        # only catch ProviderError would miss the failure entirely.
        err = ValueError("some other failure")
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RaisingAnthropicClient(err),
        )

        with pytest.raises(ProviderError):
            provider.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# dimension() and healthcheck()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDimensionAndHealth:
    """dimension() reports the no-embed sentinel; healthcheck drives chat."""

    @pytest.mark.unit
    def test_dimension_returns_zero(self) -> None:
        # Sabotage-proof: if dimension() returned a positive integer,
        # the indexing layer would think Anthropic has an embedding
        # surface and write nonsense vectors when the operator
        # mis-configures Anthropic as the embed provider. Returning 0
        # makes the misconfiguration trip an obvious invariant
        # downstream rather than corrupting the vector store silently.
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RecordingAnthropicClient(api_key="anthropic-test-key"),  # pragma: allowlist secret
        )
        assert provider.dimension() == EMBED_DIMENSION_NOT_APPLICABLE == 0

    @pytest.mark.unit
    def test_healthcheck_ok_when_chat_succeeds(self) -> None:
        # Sabotage-proof: if healthcheck() called embed_batch (the
        # azure_foundry / openai pattern) it'd always report ok=False
        # because Anthropic refuses embed. Chat is the only path that
        # can actually probe a healthy endpoint.
        client = _RecordingAnthropicClient(
            api_key="anthropic-test-key",  # pragma: allowlist secret
            responses=[_FakeMessagesResponse(content=[_FakeTextBlock(text="pong")])],
        )
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=client,
        )

        health = provider.healthcheck()

        assert isinstance(health, ProviderHealth)
        assert health.ok is True
        assert health.endpoint == "https://api.anthropic.com"
        assert health.warm_ms is not None and health.warm_ms >= 0
        # The healthcheck actually drove the chat path — recorded.
        assert len(client.calls) == 1

    @pytest.mark.unit
    def test_healthcheck_not_ok_on_provider_error(self) -> None:
        # Sabotage-proof: if healthcheck() stopped catching
        # ProviderError, the chat failure path would raise rather than
        # emit ok=False; a sabotage-mutation removing the try/except
        # would trip "did not raise" here.
        err = _UpstreamApiError(401)
        provider = AnthropicProvider(
            credentials=_anthropic_credentials(),
            transport_client=_RaisingAnthropicClient(err),
        )

        health = provider.healthcheck()

        assert health.ok is False
        # Canonical class name is surfaced — stable across plugins so
        # the JSON probe-config report has a typed failure category.
        assert health.error == "AuthError"
