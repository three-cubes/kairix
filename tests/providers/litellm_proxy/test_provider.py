"""Unit tests for :class:`kairix.providers.litellm_proxy.LiteLLMProxyProvider`.

Coverage matrix:

- Protocol conformance — ``isinstance(provider, Provider)`` (runtime-checkable).
- ``embed_batch`` records the expected wire shape (model name, input
  list, dimension kwarg) against a recording transport client. Model
  ids may carry a LiteLLM upstream prefix (``azure/foundry-deploy``,
  ``bedrock/titan-embed-v2``, ``openai/text-embedding-3-large``) — the
  plugin passes the string through verbatim.
- ``chat`` records the expected wire shape (model, messages, max_tokens).
- Error mapping — every status code (429 / 401 / 403 / 500 / 503) and
  connection failure maps to the canonical typed error and Retry-After
  hints flow through on 429.
- Endpoint passthrough — the LiteLLM proxy plugin does NOT append any
  suffix to the configured base URL (distinct from azure_foundry).
- ``dimension()`` reports the configured / discovered dim; falls back
  to the default before any embed has happened.
- ``healthcheck()`` returns ``ok=True`` on success and surfaces the
  typed error class name on failure.

Test seams:

- Recording transport client (``_RecordingTransportClient``) — captures
  every ``embeddings.create`` / ``chat.completions.create`` call so the
  test can assert wire-shape; no monkey-patching, no @patch.
- Error-raising transport client (``_RaisingTransportClient``) — drives
  the error-mapping path with stand-in upstream errors that expose
  ``.status_code`` / ``.response.headers``.

Sabotage-proofs are noted at the test definition for every test —
mutate the impl, confirm the test fails, restore.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import (
    AuthError,
    Provider,
    ProviderError,
    ProviderHealth,
    ProviderUnreachable,
    RateLimited,
    UpstreamError,
)
from kairix.providers.litellm_proxy import (
    DEFAULT_EMBED_DIMENSION,
    PROVIDER_NAME,
    LiteLLMProxyProvider,
)

# ---------------------------------------------------------------------------
# Test seams (fake transport_client surfaces)
# ---------------------------------------------------------------------------


@dataclass
class _FakeEmbeddingItem:
    """Stand-in for ``openai.types.CreateEmbeddingResponse.data[i]``."""

    embedding: list[float]


@dataclass
class _FakeEmbeddingResponse:
    data: list[_FakeEmbeddingItem]


@dataclass
class _FakeChatMessage:
    content: str | None


@dataclass
class _FakeChatChoice:
    message: _FakeChatMessage


@dataclass
class _FakeChatResponse:
    choices: list[_FakeChatChoice]


class _RecordingEmbeddings:
    """Records every ``embeddings.create(**kwargs)`` call."""

    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeEmbeddingResponse:
        self.calls.append(dict(kwargs))
        return _FakeEmbeddingResponse(data=[_FakeEmbeddingItem(embedding=list(v)) for v in self._vectors])


class _RecordingChatCompletions:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeChatResponse:
        self.calls.append(dict(kwargs))
        return _FakeChatResponse(choices=[_FakeChatChoice(message=_FakeChatMessage(content=self._content))])


class _RecordingChat:
    def __init__(self, content: str) -> None:
        self.completions = _RecordingChatCompletions(content)


class _RecordingTransportClient:
    """Test-seam transport client recording embed and chat call kwargs.

    Mirrors the openai-SDK surface that :class:`LiteLLMProxyProvider`
    actually consumes: an ``embeddings.create`` and a
    ``chat.completions.create`` method. No HTTP is performed; the
    recorded kwargs are the wire-shape contract this provider pins.
    """

    def __init__(
        self,
        *,
        vectors: list[list[float]] | None = None,
        chat_content: str = "hello",
    ) -> None:
        self.embeddings = _RecordingEmbeddings(vectors or [[0.1, 0.2, 0.3]])
        self.chat = _RecordingChat(chat_content)


class _FakeHttpResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class _UpstreamApiError(Exception):
    """Stand-in for an openai-SDK ``APIStatusError``-shaped exception.

    Exposes ``.status_code`` and ``.response.headers`` — the two
    attributes the provider's error mapper reads.
    """

    def __init__(self, status_code: int, *, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.response = _FakeHttpResponse(status_code, headers)
        super().__init__(f"upstream HTTP {status_code}")


class _RaisingEmbeddings:
    def __init__(self, err: BaseException) -> None:
        self._err = err
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeEmbeddingResponse:
        self.calls.append(dict(kwargs))
        raise self._err


class _RaisingChatCompletions:
    def __init__(self, err: BaseException) -> None:
        self._err = err
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeChatResponse:
        self.calls.append(dict(kwargs))
        raise self._err


class _RaisingChat:
    def __init__(self, err: BaseException) -> None:
        self.completions = _RaisingChatCompletions(err)


class _RaisingTransportClient:
    """Transport client that always raises a configured upstream error."""

    def __init__(self, err: BaseException) -> None:
        self.embeddings = _RaisingEmbeddings(err)
        self.chat = _RaisingChat(err)


def _litellm_credentials(
    *,
    api_key: str = "sk-litellm-test-vk",  # pragma: allowlist secret
    endpoint: str = "http://localhost:4000/v1",
    model: str = "text-embedding-3-large",
    dims: int = 1536,
) -> Credentials:
    """Construct a Credentials test instance pinned to LiteLLM-proxy shape."""
    return Credentials(api_key=api_key, endpoint=endpoint, model=model, dims=dims)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProtocolConformance:
    """LiteLLMProxyProvider satisfies the runtime-checkable Provider Protocol."""

    @pytest.mark.unit
    def test_isinstance_provider_at_runtime(self) -> None:
        # Sabotage-proof: removing any of name / embed_batch / chat /
        # dimension / healthcheck from LiteLLMProxyProvider breaks the
        # runtime_checkable isinstance() — caught here.
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RecordingTransportClient(),
        )
        assert isinstance(provider, Provider)

    @pytest.mark.unit
    def test_name_matches_pyproject_entry_point_key(self) -> None:
        # Sabotage-proof: if PROVIDER_NAME drifted from "litellm_proxy",
        # the pyproject.toml entry-point and BDD feature names would
        # stop matching what get_provider("litellm_proxy") returns.
        assert PROVIDER_NAME == "litellm_proxy"
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RecordingTransportClient(),
        )
        assert provider.name == "litellm_proxy"


# ---------------------------------------------------------------------------
# embed_batch wire shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmbedBatchWireShape:
    """embed_batch records the expected request shape on the transport client."""

    @pytest.mark.unit
    def test_records_model_input_and_dimensions(self) -> None:
        # Sabotage-proof: if embed_batch dropped the model= kwarg, the
        # recorded call wouldn't contain "model" and the test fails.
        client = _RecordingTransportClient(vectors=[[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(model="text-embedding-3-small", dims=1536),
            transport_client=client,
        )

        out = provider.embed_batch(["alpha", "beta"])

        assert out == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        assert len(client.embeddings.calls) == 1
        call = client.embeddings.calls[0]
        assert call["model"] == "text-embedding-3-small"
        assert call["input"] == ["alpha", "beta"]
        assert call["dimensions"] == 1536

    @pytest.mark.unit
    def test_prefixed_model_name_flows_through_verbatim(self) -> None:
        # Sabotage-proof: if the plugin started stripping LiteLLM's
        # upstream prefix (``azure/``, ``bedrock/``, ``openai/``) from
        # the model name, the proxy would fail to route the request to
        # the configured upstream. This test pins the passthrough.
        client = _RecordingTransportClient()
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(model="bedrock/titan-embed-v2"),
            transport_client=client,
        )

        provider.embed_batch(["alpha"])

        assert client.embeddings.calls[0]["model"] == "bedrock/titan-embed-v2"

    @pytest.mark.unit
    def test_empty_input_returns_empty_without_calling_transport(self) -> None:
        # Sabotage-proof: if embed_batch dispatched on empty input, the
        # recorded calls list would be non-empty.
        client = _RecordingTransportClient()
        provider = LiteLLMProxyProvider(credentials=_litellm_credentials(), transport_client=client)

        assert provider.embed_batch([]) == []
        assert client.embeddings.calls == []

    @pytest.mark.unit
    def test_endpoint_is_passed_through_unchanged_no_suffix_appended(self) -> None:
        # Sabotage-proof: if the litellm_proxy plugin started appending
        # ``/openai/v1`` (the Foundry-specific suffix) to the configured
        # proxy URL, healthcheck() would surface that mangled URL and
        # operators with custom proxy deployments would hit 404s. This
        # test pins the endpoint passthrough.
        plain = "http://proxy.internal:8000/v1"
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(endpoint=plain),
            transport_client=_RecordingTransportClient(),
        )
        health = provider.healthcheck()
        assert health.endpoint == plain
        assert "/openai/v1/openai/v1" not in health.endpoint


# ---------------------------------------------------------------------------
# chat wire shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChatWireShape:
    """chat records the expected ``chat.completions.create`` call shape."""

    @pytest.mark.unit
    def test_records_model_messages_and_max_tokens(self) -> None:
        # Sabotage-proof: dropping any of model / messages / max_tokens
        # from the create() kwargs trips a missing-key assertion.
        client = _RecordingTransportClient(chat_content="hi back")
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(model="gpt-4o-mini"),
            transport_client=client,
        )

        out = provider.chat(
            [{"role": "user", "content": "hi"}],
            max_tokens=42,
        )

        assert out == "hi back"
        assert len(client.chat.completions.calls) == 1
        call = client.chat.completions.calls[0]
        assert call["model"] == "gpt-4o-mini"
        assert call["messages"] == [{"role": "user", "content": "hi"}]
        assert call["max_tokens"] == 42

    @pytest.mark.unit
    def test_chat_with_none_content_returns_empty_string(self) -> None:
        # Sabotage-proof: if chat() didn't coerce None content to "",
        # the test fails because out would be None and the str-equality
        # comparison would raise / fail.

        class _NoneContentCompletions:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def create(self, **kwargs: Any) -> _FakeChatResponse:
                self.calls.append(dict(kwargs))
                return _FakeChatResponse(choices=[_FakeChatChoice(message=_FakeChatMessage(content=None))])

        class _NoneClient:
            def __init__(self) -> None:
                self.embeddings = _RecordingEmbeddings([[0.0]])

                class _Chat:
                    def __init__(self) -> None:
                        self.completions = _NoneContentCompletions()

                self.chat = _Chat()

        provider = LiteLLMProxyProvider(credentials=_litellm_credentials(), transport_client=_NoneClient())
        assert provider.chat([{"role": "user", "content": "hi"}]) == ""


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmbedErrorMapping:
    """Upstream errors map to canonical typed errors per status code."""

    @pytest.mark.unit
    def test_429_maps_to_rate_limited_with_retry_after(self) -> None:
        # Sabotage-proof: if the mapper stopped reading Retry-After,
        # err.retry_after_s would be None and the assert fails.
        err = _UpstreamApiError(429, headers={"Retry-After": "12"})
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(RateLimited) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.retry_after_s == 12.0

    @pytest.mark.unit
    def test_429_without_retry_after_yields_none_hint(self) -> None:
        err = _UpstreamApiError(429)
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(RateLimited) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.retry_after_s is None

    @pytest.mark.unit
    def test_401_maps_to_auth_error_naming_provider(self) -> None:
        # Sabotage-proof: if the mapper stopped naming the provider in
        # the AuthError message, the BDD scenario at
        # provider_litellm_proxy.feature §"401 maps to AuthError" fails.
        err = _UpstreamApiError(401)
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(AuthError) as exc_info:
            provider.embed_batch(["alpha"])
        assert "litellm_proxy" in str(exc_info.value)

    @pytest.mark.unit
    def test_403_also_maps_to_auth_error(self) -> None:
        err = _UpstreamApiError(403)
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(AuthError):
            provider.embed_batch(["alpha"])

    @pytest.mark.unit
    def test_500_maps_to_upstream_error_with_status_code(self) -> None:
        # Sabotage-proof: if the mapper dropped status_code from
        # UpstreamError, exc_info.value.status_code AttributeError-s.
        err = _UpstreamApiError(500)
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(UpstreamError) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.status_code == 500

    @pytest.mark.unit
    def test_503_also_maps_to_upstream_error(self) -> None:
        err = _UpstreamApiError(503)
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(UpstreamError) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.status_code == 503

    @pytest.mark.unit
    def test_connection_failure_maps_to_provider_unreachable(self) -> None:
        # Sabotage-proof: if _is_connection_failure stopped recognising
        # ConnectionError, the mapper would fall through to the bare
        # ProviderError branch and this assert fails.
        err = ConnectionError("DNS lookup failed")
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(ProviderUnreachable):
            provider.embed_batch(["alpha"])

    @pytest.mark.unit
    def test_unknown_error_falls_back_to_provider_error(self) -> None:
        # Sabotage-proof: if the mapper raised the original exception
        # instead of wrapping in ProviderError, callers downstream that
        # only catch ProviderError would miss the failure entirely.
        err = ValueError("some other failure")
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(ProviderError):
            provider.embed_batch(["alpha"])


@pytest.mark.unit
class TestChatErrorMapping:
    """Chat path uses the same error mapper as embed."""

    @pytest.mark.unit
    def test_chat_429_maps_to_rate_limited(self) -> None:
        # Sabotage-proof: if the chat path used a different mapper, this
        # would surface the raw _UpstreamApiError instead of RateLimited.
        err = _UpstreamApiError(429, headers={"Retry-After": "3"})
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(RateLimited) as exc_info:
            provider.chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.retry_after_s == 3.0

    @pytest.mark.unit
    def test_chat_connection_failure_maps_to_unreachable(self) -> None:
        err = ConnectionError("refused")
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(ProviderUnreachable):
            provider.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# dimension() and healthcheck()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDimensionAndHealth:
    """dimension() and healthcheck() honour the configured credentials."""

    @pytest.mark.unit
    def test_dimension_defaults_when_credentials_have_no_dims(self) -> None:
        # Sabotage-proof: if dimension() returned 0 when dims=0 was
        # configured, the SearchPipeline's vector backend would index
        # zero-length vectors. DEFAULT_EMBED_DIMENSION must back-stop.
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(dims=0),
            transport_client=_RecordingTransportClient(),
        )
        assert provider.dimension() == DEFAULT_EMBED_DIMENSION

    @pytest.mark.unit
    def test_dimension_uses_credential_dims_before_embed(self) -> None:
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(dims=2048),
            transport_client=_RecordingTransportClient(),
        )
        assert provider.dimension() == 2048

    @pytest.mark.unit
    def test_dimension_uses_observed_dim_after_first_embed(self) -> None:
        # Sabotage-proof: if embed_batch didn't update _embed_dimension,
        # dimension() would stay at the configured 1536 even when the
        # deployed model returned a 7-dim vector — caught here.
        client = _RecordingTransportClient(vectors=[[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]])
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(dims=1536),
            transport_client=client,
        )

        provider.embed_batch(["alpha"])

        assert provider.dimension() == 7

    @pytest.mark.unit
    def test_healthcheck_ok_when_embed_succeeds(self) -> None:
        # Sabotage-proof: if healthcheck() stopped catching ProviderError
        # the embed_batch failure path would raise rather than emit
        # ok=False; a sabotage-mutation removing the try/except would
        # trip "did not raise" here.
        client = _RecordingTransportClient(vectors=[[0.1, 0.2, 0.3]])
        provider = LiteLLMProxyProvider(credentials=_litellm_credentials(), transport_client=client)

        health = provider.healthcheck()

        assert isinstance(health, ProviderHealth)
        assert health.ok is True
        assert health.endpoint == "http://localhost:4000/v1"
        assert health.warm_ms is not None and health.warm_ms >= 0

    @pytest.mark.unit
    def test_healthcheck_not_ok_on_provider_error(self) -> None:
        err = _UpstreamApiError(401)
        provider = LiteLLMProxyProvider(
            credentials=_litellm_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        health = provider.healthcheck()

        assert health.ok is False
        # Canonical class name is surfaced — stable across plugins so
        # the JSON probe-config report has a typed failure category.
        assert health.error == "AuthError"
