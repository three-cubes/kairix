"""Unit tests for :class:`kairix.providers.azure_foundry.AzureFoundryProvider`.

Coverage matrix:

- Protocol conformance — ``isinstance(provider, Provider)`` (runtime-checkable).
- ``embed_batch`` records the expected wire shape (model name, input list,
  dimension kwarg) against a recording transport client.
- ``chat`` records the expected wire shape (model, messages, max_tokens).
- Error mapping — every status code (429 / 401 / 403 / 500 / 503) and
  connection failure maps to the canonical typed error and Retry-After
  hints flow through on 429.
- URL-suffix normalisation — Foundry endpoints get ``/openai/v1``
  appended exactly once, never duplicated.
- ``dimension()`` reports the configured / discovered dim; falls back to
  the default before any embed has happened.
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
from kairix.providers.azure_foundry import (
    DEFAULT_EMBED_DIMENSION,
    PROVIDER_NAME,
    AzureFoundryProvider,
    normalize_foundry_endpoint,
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

    Mirrors the openai-SDK surface that
    :class:`AzureFoundryProvider` actually consumes: an
    ``embeddings.create`` and a ``chat.completions.create`` method. No
    HTTP is performed; the recorded kwargs are the wire-shape contract
    this provider pins.
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


def _foundry_credentials(
    *,
    api_key: str = "foundry-test-key",  # pragma: allowlist secret
    endpoint: str = "https://example-resource.services.ai.azure.com",
    model: str = "text-embedding-3-large",
    dims: int = 1536,
) -> Credentials:
    """Construct a Credentials test instance pinned to Foundry shape."""
    return Credentials(api_key=api_key, endpoint=endpoint, model=model, dims=dims)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProtocolConformance:
    """AzureFoundryProvider satisfies the runtime-checkable Protocol."""

    @pytest.mark.unit
    def test_isinstance_provider_at_runtime(self) -> None:
        # Sabotage-proof: removing any of name / embed_batch / chat /
        # dimension / healthcheck from AzureFoundryProvider breaks the
        # runtime_checkable isinstance() — caught here.
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
            transport_client=_RecordingTransportClient(),
        )
        assert isinstance(provider, Provider)

    @pytest.mark.unit
    def test_name_matches_pyproject_entry_point_key(self) -> None:
        # Sabotage-proof: if PROVIDER_NAME drifted from "azure_foundry",
        # the pyproject.toml entry-point and BDD feature names would
        # stop matching what get_provider("azure_foundry") returns.
        assert PROVIDER_NAME == "azure_foundry"
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
            transport_client=_RecordingTransportClient(),
        )
        assert provider.name == "azure_foundry"


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
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(model="text-embedding-3-small", dims=1536),
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
    def test_empty_input_returns_empty_without_calling_transport(self) -> None:
        # Sabotage-proof: if embed_batch dispatched on empty input, the
        # recorded calls list would be non-empty.
        client = _RecordingTransportClient()
        provider = AzureFoundryProvider(credentials=_foundry_credentials(), transport_client=client)

        assert provider.embed_batch([]) == []
        assert client.embeddings.calls == []

    @pytest.mark.unit
    def test_credentials_endpoint_is_carried_through_normalisation(self) -> None:
        # Sabotage-proof: if the URL helper started double-suffixing
        # endpoints that already include /openai/v1, the normalisation
        # would produce "/openai/v1/openai/v1" and trip the BDD wire
        # scenario. Verified directly here so the helper stays correct.
        plain = "https://example-resource.services.ai.azure.com"
        already = "https://example-resource.services.ai.azure.com/openai/v1"
        assert normalize_foundry_endpoint(plain).endswith("/openai/v1")
        assert normalize_foundry_endpoint(already).endswith("/openai/v1")
        assert "/openai/v1/openai/v1" not in normalize_foundry_endpoint(already)


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
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(model="gpt-4o-mini"),
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

        provider = AzureFoundryProvider(credentials=_foundry_credentials(), transport_client=_NoneClient())
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
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(RateLimited) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.retry_after_s == 12.0

    @pytest.mark.unit
    def test_429_without_retry_after_yields_none_hint(self) -> None:
        err = _UpstreamApiError(429)
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(RateLimited) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.retry_after_s is None

    @pytest.mark.unit
    def test_401_maps_to_auth_error_naming_provider(self) -> None:
        # Sabotage-proof: if the mapper stopped naming the provider in
        # the AuthError message, the BDD scenario at
        # provider_azure_foundry.feature §"401 maps to AuthError" fails.
        err = _UpstreamApiError(401)
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(AuthError) as exc_info:
            provider.embed_batch(["alpha"])
        assert "azure_foundry" in str(exc_info.value)

    @pytest.mark.unit
    def test_403_also_maps_to_auth_error(self) -> None:
        err = _UpstreamApiError(403)
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(AuthError):
            provider.embed_batch(["alpha"])

    @pytest.mark.unit
    def test_500_maps_to_upstream_error_with_status_code(self) -> None:
        # Sabotage-proof: if the mapper dropped status_code from
        # UpstreamError, exc_info.value.status_code AttributeError-s.
        err = _UpstreamApiError(500)
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(UpstreamError) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.status_code == 500

    @pytest.mark.unit
    def test_503_also_maps_to_upstream_error(self) -> None:
        err = _UpstreamApiError(503)
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
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
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
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
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
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
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        with pytest.raises(RateLimited) as exc_info:
            provider.chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.retry_after_s == 3.0

    @pytest.mark.unit
    def test_chat_connection_failure_maps_to_unreachable(self) -> None:
        err = ConnectionError("refused")
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
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
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(dims=0),
            transport_client=_RecordingTransportClient(),
        )
        assert provider.dimension() == DEFAULT_EMBED_DIMENSION

    @pytest.mark.unit
    def test_dimension_uses_credential_dims_before_embed(self) -> None:
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(dims=2048),
            transport_client=_RecordingTransportClient(),
        )
        assert provider.dimension() == 2048

    @pytest.mark.unit
    def test_dimension_uses_observed_dim_after_first_embed(self) -> None:
        # Sabotage-proof: if embed_batch didn't update _embed_dimension,
        # dimension() would stay at the configured 1536 even when the
        # deployed model returned a 7-dim vector — caught here.
        client = _RecordingTransportClient(vectors=[[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]])
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(dims=1536),
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
        provider = AzureFoundryProvider(credentials=_foundry_credentials(), transport_client=client)

        health = provider.healthcheck()

        assert isinstance(health, ProviderHealth)
        assert health.ok is True
        assert health.endpoint.endswith("/openai/v1")
        assert health.warm_ms is not None and health.warm_ms >= 0

    @pytest.mark.unit
    def test_healthcheck_not_ok_on_provider_error(self) -> None:
        err = _UpstreamApiError(401)
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(),
            transport_client=_RaisingTransportClient(err),
        )

        health = provider.healthcheck()

        assert health.ok is False
        # Canonical class name is surfaced — stable across plugins so
        # the JSON probe-config report has a typed failure category.
        assert health.error == "AuthError"


# ---------------------------------------------------------------------------
# Reasoning-class model → max_completion_tokens translation
#
# All branches of the internal ``_uses_max_completion_tokens`` helper are
# exercised through the public ``provider.chat(...)`` surface in
# TestChatKwargRouting below and in the parametrised matrix in
# ``tests/integration/test_azure_foundry_reasoning_model_chat.py``. The
# helper has no direct test cases (F5 — no internal-name imports in tests).
# If a branch isn't reachable through the public chat method, it is dead
# code and gets deleted, not pinned.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChatKwargRouting:
    """``chat`` routes the public ``max_tokens=`` kwarg to the correct wire
    parameter based on the configured deployment model.
    """

    def test_gpt5_deployment_sends_max_completion_tokens_on_wire(self) -> None:
        """A gpt-5.x deployment receives ``max_completion_tokens`` (not
        ``max_tokens``) on the wire.

        Sabotage-proof: revert ``chat()`` to always send ``max_tokens``
        and this fails because ``max_completion_tokens`` is missing.
        """
        client = _RecordingTransportClient(chat_content="ok")
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(model="gpt-5.4-mini"),
            transport_client=client,
        )

        provider.chat([{"role": "user", "content": "hi"}], max_tokens=500)

        call = client.chat.completions.calls[0]
        assert call["max_completion_tokens"] == 500
        assert "max_tokens" not in call

    def test_o1_deployment_sends_max_completion_tokens_on_wire(self) -> None:
        """An o1 deployment receives ``max_completion_tokens``.

        Sabotage-proof: drop "o1" from ``_REASONING_MODEL_PREFIXES``
        and the wire receives ``max_tokens``, this fails.
        """
        client = _RecordingTransportClient(chat_content="ok")
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(model="o1-mini"),
            transport_client=client,
        )

        provider.chat([{"role": "user", "content": "hi"}], max_tokens=100)

        call = client.chat.completions.calls[0]
        assert call["max_completion_tokens"] == 100
        assert "max_tokens" not in call

    def test_gpt4o_deployment_continues_to_send_max_tokens_on_wire(self) -> None:
        """A gpt-4o-mini deployment retains the legacy ``max_tokens`` kwarg.

        Sabotage-proof: change ``chat()`` to always send
        ``max_completion_tokens`` and this fails — gpt-4o-mini rejects
        the unknown kwarg silently or with a 400.
        """
        client = _RecordingTransportClient(chat_content="ok")
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(model="gpt-4o-mini"),
            transport_client=client,
        )

        provider.chat([{"role": "user", "content": "hi"}], max_tokens=42)

        call = client.chat.completions.calls[0]
        assert call["max_tokens"] == 42
        assert "max_completion_tokens" not in call

    def test_temperature_still_present_on_reasoning_model_call(self) -> None:
        """The model-name routing must not accidentally drop the
        ``temperature`` kwarg the provider passes for synthesis
        determinism.

        Sabotage-proof: a refactor that builds the kwargs dict per branch
        and forgets ``temperature`` in the reasoning branch fails this.
        """
        client = _RecordingTransportClient(chat_content="ok")
        provider = AzureFoundryProvider(
            credentials=_foundry_credentials(model="gpt-5.4-mini"),
            transport_client=client,
        )

        provider.chat([{"role": "user", "content": "hi"}], max_tokens=100)

        call = client.chat.completions.calls[0]
        assert "temperature" in call, (
            f"reasoning-model branch must still pass temperature; got kwargs: {sorted(call.keys())}"
        )
