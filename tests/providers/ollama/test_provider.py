"""Unit tests for :class:`kairix.providers.ollama.OllamaProvider`.

Coverage matrix:

- Protocol conformance — ``isinstance(provider, Provider)`` (runtime-checkable).
- ``embed_batch`` records the expected wire shape against a recording
  fake transport: path is ``/api/embeddings`` (NOT ``/v1/embeddings``,
  NOT ``/openai/v1/embeddings``), body carries the configured model and
  the per-text prompt, and N input texts produce exactly N recorded
  POST calls (loop-batched).
- ``embed_batch`` records no ``Authorization`` / no ``api-key`` header
  — Ollama is unauthenticated and the absence is load-bearing.
- ``chat`` records the expected wire shape (path ``/api/chat``, body
  has ``model`` / ``messages`` / ``stream=False`` / ``options.num_predict``).
- Error mapping — connection-refused maps to ``ProviderUnreachable``;
  404 maps to ``ClientError`` carrying the model name; 5xx maps to
  ``UpstreamError`` with status_code.
- ``dimension()`` reports the configured / discovered dim and adapts
  to the first observed embed response (Ollama embed dim depends on
  the deployed model — 768 for nomic-embed-text, 384 for all-MiniLM).
- ``healthcheck()`` returns ``ok=True`` on success and surfaces the
  canonical error class name on failure.

Test seams:

- Recording transport client (``_RecordingTransport``) — captures every
  ``post(path, json)`` call so the test can assert wire-shape; no
  monkey-patching, no @patch, no env mutation.
- Error-raising transport client (``_RaisingTransport``) — drives the
  error-mapping path with stand-in upstream errors.

Sabotage-proofs are noted at the test definition for every test —
mutate the impl, confirm the test fails, restore.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import (
    ClientError,
    Provider,
    ProviderError,
    ProviderHealth,
    ProviderUnreachable,
    UpstreamError,
)
from kairix.providers.ollama import (
    DEFAULT_EMBED_DIMENSION,
    PROVIDER_NAME,
    OllamaProvider,
)

# ---------------------------------------------------------------------------
# Test seams — recording and raising HTTP transports
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """Records every ``post(path, json)`` call and returns a configured body.

    Mirrors the :class:`kairix.providers.ollama.OllamaTransport` Protocol:
    a single ``post`` method that returns a decoded JSON dict. The plugin
    never asks the transport for auth headers — confirming that absence
    is one of the load-bearing assertions for this plugin, so the
    recording fake intentionally exposes no header field at all (any
    attempt by the plugin to attach auth would have to mutate the recorded
    request shape, which would surface elsewhere).
    """

    def __init__(
        self,
        *,
        embed_vector: list[float] | None = None,
        chat_content: str = "hello back",
    ) -> None:
        self._embed_vector = embed_vector if embed_vector is not None else [0.1, 0.2, 0.3]
        self._chat_content = chat_content
        self.calls: list[dict[str, Any]] = []

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"path": path, "json": dict(json)})
        if path.endswith("/embeddings"):
            return {"embedding": list(self._embed_vector)}
        if path.endswith("/chat"):
            return {"message": {"role": "assistant", "content": self._chat_content}}
        return {}


class _VariableEmbedTransport:
    """Recording transport that returns a different vector per call.

    Used to verify the dimension-adaptation path: when the first embed
    response carries a 7-dim vector, ``dimension()`` must report 7 even
    though credentials were configured with a different dim.
    """

    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = list(vectors)
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"path": path, "json": dict(json)})
        vec = self._vectors[self._index % len(self._vectors)]
        self._index += 1
        return {"embedding": list(vec)}


class _HttpStatusStubError(Exception):
    """Stand-in for the production ``_HttpStatusError`` carrying ``status_code``."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"upstream HTTP {status_code}")


class _RaisingTransport:
    """Transport that always raises a configured exception."""

    def __init__(self, err: BaseException) -> None:
        self._err = err
        self.calls: list[dict[str, Any]] = []

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"path": path, "json": dict(json)})
        raise self._err


def _ollama_credentials(
    *,
    api_key: str = "",  # Ollama is unauthenticated; empty by design.
    endpoint: str = "http://localhost:11434",
    model: str = "nomic-embed-text",
    dims: int = 0,
) -> Credentials:
    """Construct a Credentials test instance pinned to Ollama shape."""
    return Credentials(api_key=api_key, endpoint=endpoint, model=model, dims=dims)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProtocolConformance:
    """OllamaProvider satisfies the runtime-checkable Provider Protocol."""

    @pytest.mark.unit
    def test_isinstance_provider_at_runtime(self) -> None:
        # Sabotage-proof: removing any of name / embed_batch / chat /
        # dimension / healthcheck from OllamaProvider breaks the
        # runtime_checkable isinstance() — caught here. Verified by
        # commenting out `def dimension` in provider.py → this fails.
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_RecordingTransport(),
        )
        assert isinstance(provider, Provider)

    @pytest.mark.unit
    def test_name_matches_pyproject_entry_point_key(self) -> None:
        # Sabotage-proof: drift in PROVIDER_NAME would desync the
        # pyproject.toml entry-point key from what get_provider("ollama")
        # returns. Verified by mutating PROVIDER_NAME = "ollama2" →
        # this fails.
        assert PROVIDER_NAME == "ollama"
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_RecordingTransport(),
        )
        assert provider.name == "ollama"


# ---------------------------------------------------------------------------
# embed_batch wire shape — the load-bearing scenario for this plugin
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmbedBatchWireShape:
    """embed_batch records the Ollama-native /api/embeddings wire shape."""

    @pytest.mark.unit
    def test_records_native_embeddings_path_not_openai_path(self) -> None:
        # Sabotage-proof: regress the path constant to "/v1/embeddings"
        # (the OpenAI shape) → assertion fails because path no longer
        # equals "/api/embeddings". Verified.
        transport = _RecordingTransport()
        provider = OllamaProvider(
            credentials=_ollama_credentials(model="nomic-embed-text"),
            transport_client=transport,
        )

        provider.embed_batch(["alpha"])

        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call["path"] == "/api/embeddings"
        assert "/v1/" not in call["path"]
        assert "/openai/" not in call["path"]

    @pytest.mark.unit
    def test_records_model_and_prompt_in_body(self) -> None:
        # Sabotage-proof: drop the model= key from the request body in
        # embed_batch → recorded body has no "model" key, the assert
        # fails. Verified.
        transport = _RecordingTransport()
        provider = OllamaProvider(
            credentials=_ollama_credentials(model="llama3:8b"),
            transport_client=transport,
        )

        provider.embed_batch(["the quick brown fox"])

        body = transport.calls[0]["json"]
        assert body["model"] == "llama3:8b"
        assert body["prompt"] == "the quick brown fox"

    @pytest.mark.unit
    def test_n_texts_fan_out_to_n_requests_preserving_order(self) -> None:
        # Sabotage-proof: change the embed loop to send all texts in one
        # request (Ollama doesn't support that shape) → recorded calls
        # count would be 1, this fails. Equally, returning vectors in a
        # different order would fail the per-call prompt match.
        transport = _RecordingTransport(embed_vector=[1.0, 1.0, 1.0])
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=transport,
        )

        vectors = provider.embed_batch(["alpha", "beta", "gamma"])

        assert len(vectors) == 3
        assert len(transport.calls) == 3
        prompts_in_order = [call["json"]["prompt"] for call in transport.calls]
        assert prompts_in_order == ["alpha", "beta", "gamma"]

    @pytest.mark.unit
    def test_no_auth_header_construction_in_recorded_request_body(self) -> None:
        # The plugin's contract is "never emit an Authorization or
        # api-key header" — the transport seam exposes no header surface,
        # and the plugin must not attempt to attach one via the body
        # either. We assert that the request body contains exactly the
        # documented keys: model + prompt.
        #
        # Sabotage-proof: if the plugin started passing `headers={...}`
        # into transport.post (or smuggling auth into the body), the
        # body would contain unexpected extra keys, this fails.
        transport = _RecordingTransport()
        provider = OllamaProvider(
            credentials=_ollama_credentials(api_key="should-be-ignored"),  # pragma: allowlist secret
            transport_client=transport,
        )

        provider.embed_batch(["x"])

        body = transport.calls[0]["json"]
        assert set(body.keys()) == {"model", "prompt"}

    @pytest.mark.unit
    def test_empty_input_returns_empty_without_calling_transport(self) -> None:
        # Sabotage-proof: if embed_batch dispatched on empty input, the
        # recorded calls list would be non-empty. Verified.
        transport = _RecordingTransport()
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=transport,
        )

        assert provider.embed_batch([]) == []
        assert transport.calls == []


# ---------------------------------------------------------------------------
# chat wire shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChatWireShape:
    """chat records the /api/chat wire shape with stream=False."""

    @pytest.mark.unit
    def test_records_chat_path_and_body(self) -> None:
        # Sabotage-proof: change the chat URL to /api/generate or
        # /v1/chat/completions → the path assertion fails. Verified.
        transport = _RecordingTransport(chat_content="hi from ollama")
        provider = OllamaProvider(
            credentials=_ollama_credentials(model="llama3:8b"),
            transport_client=transport,
        )

        out = provider.chat([{"role": "user", "content": "ping"}], max_tokens=42)

        assert out == "hi from ollama"
        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call["path"] == "/api/chat"
        body = call["json"]
        assert body["model"] == "llama3:8b"
        assert body["messages"] == [{"role": "user", "content": "ping"}]
        assert body["stream"] is False
        assert body["options"]["num_predict"] == 42

    @pytest.mark.unit
    def test_chat_with_empty_message_returns_empty_string(self) -> None:
        # Sabotage-proof: if chat() returned None when content is missing
        # the str-equality with "" would fail. Verified by mutating the
        # final coalesce branch.
        class _EmptyContent:
            def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
                del path, json
                return {"message": {"role": "assistant", "content": None}}

        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_EmptyContent(),
        )
        assert provider.chat([{"role": "user", "content": "hi"}]) == ""


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmbedErrorMapping:
    """Upstream errors map to the canonical typed-error vocabulary."""

    @pytest.mark.unit
    def test_connection_refused_maps_to_provider_unreachable(self) -> None:
        # The load-bearing failure mode: operator's sidecar isn't running.
        # Sabotage-proof: remove the _is_connection_failure early-return
        # in _map_transport_error → connection failures fall through to
        # bare ProviderError, this isinstance check fails. Verified.
        err = ConnectionRefusedError("Connection refused")
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_RaisingTransport(err),
        )

        with pytest.raises(ProviderUnreachable) as exc_info:
            provider.embed_batch(["alpha"])
        # Surface should name the provider and the endpoint so operators
        # can identify which local URL is unreachable.
        message = str(exc_info.value)
        assert "ollama" in message.lower()
        assert "localhost:11434" in message

    @pytest.mark.unit
    def test_generic_connection_error_also_maps_to_unreachable(self) -> None:
        # Sabotage-proof: drop the ConnectionError isinstance check →
        # falls through to bare ProviderError, fails here.
        err = ConnectionError("DNS lookup failed")
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_RaisingTransport(err),
        )

        with pytest.raises(ProviderUnreachable):
            provider.embed_batch(["alpha"])

    @pytest.mark.unit
    def test_404_maps_to_client_error_naming_model(self) -> None:
        # Sabotage-proof: drop the 404 branch → falls through to the 4xx
        # generic ClientError, but the message no longer carries the
        # ``ollama pull`` fix-hint. Verified by mutating the branch.
        err = _HttpStatusStubError(404)
        provider = OllamaProvider(
            credentials=_ollama_credentials(model="missing-model"),
            transport_client=_RaisingTransport(err),
        )

        with pytest.raises(ClientError) as exc_info:
            provider.embed_batch(["alpha"])
        message = str(exc_info.value)
        assert "missing-model" in message
        assert "ollama pull" in message
        assert exc_info.value.status == 404

    @pytest.mark.unit
    def test_400_maps_to_client_error_generic(self) -> None:
        # Sabotage-proof: drop the generic 4xx branch → falls through to
        # bare ProviderError, this isinstance fails.
        err = _HttpStatusStubError(400)
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_RaisingTransport(err),
        )

        with pytest.raises(ClientError) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.status == 400

    @pytest.mark.unit
    def test_500_maps_to_upstream_error_with_status_code(self) -> None:
        # Sabotage-proof: if the mapper dropped status_code from
        # UpstreamError, exc_info.value.status_code AttributeError-s.
        err = _HttpStatusStubError(500)
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_RaisingTransport(err),
        )

        with pytest.raises(UpstreamError) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.status_code == 500

    @pytest.mark.unit
    def test_503_also_maps_to_upstream_error(self) -> None:
        err = _HttpStatusStubError(503)
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_RaisingTransport(err),
        )

        with pytest.raises(UpstreamError) as exc_info:
            provider.embed_batch(["alpha"])
        assert exc_info.value.status_code == 503

    @pytest.mark.unit
    def test_unknown_error_falls_back_to_provider_error(self) -> None:
        # Sabotage-proof: if the mapper let the raw exception escape,
        # the ProviderError isinstance fails. Callers downstream that
        # only catch ProviderError would miss the failure entirely.
        err = ValueError("some other failure")
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_RaisingTransport(err),
        )

        with pytest.raises(ProviderError):
            provider.embed_batch(["alpha"])


@pytest.mark.unit
class TestChatErrorMapping:
    """Chat path uses the same error mapper as embed."""

    @pytest.mark.unit
    def test_chat_connection_failure_maps_to_unreachable(self) -> None:
        # Sabotage-proof: if chat used a different mapper (e.g. failed
        # to translate ConnectionError), this would surface the raw
        # ConnectionError instead of the typed ProviderUnreachable.
        err = ConnectionError("refused")
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_RaisingTransport(err),
        )

        with pytest.raises(ProviderUnreachable):
            provider.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.unit
    def test_chat_404_maps_to_client_error(self) -> None:
        # Sabotage-proof: drop the 4xx branch on the chat path → raw
        # exception escapes, isinstance fails.
        err = _HttpStatusStubError(404)
        provider = OllamaProvider(
            credentials=_ollama_credentials(model="missing-llm"),
            transport_client=_RaisingTransport(err),
        )

        with pytest.raises(ClientError):
            provider.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# dimension() and healthcheck()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDimensionAndHealth:
    """dimension() adapts to Ollama's variable embed-dim-per-model behaviour."""

    @pytest.mark.unit
    def test_dimension_defaults_when_credentials_have_no_dims(self) -> None:
        # Sabotage-proof: if dimension() returned 0 when no dims is
        # configured and no embed has happened, the vector backend
        # downstream would index zero-length vectors. The 768 default
        # must back-stop until the first embed updates it.
        provider = OllamaProvider(
            credentials=_ollama_credentials(dims=0),
            transport_client=_RecordingTransport(),
        )
        assert provider.dimension() == DEFAULT_EMBED_DIMENSION
        assert provider.dimension() == 768  # nomic-embed-text is the modal Ollama embed model

    @pytest.mark.unit
    def test_dimension_uses_credential_dims_before_embed(self) -> None:
        provider = OllamaProvider(
            credentials=_ollama_credentials(dims=384),
            transport_client=_RecordingTransport(),
        )
        # Pre-embed dimension respects configured dims (e.g. operator
        # using all-MiniLM rather than nomic-embed-text).
        assert provider.dimension() == 384

    @pytest.mark.unit
    def test_dimension_adapts_to_first_observed_embed_response(self) -> None:
        # The load-bearing Ollama-specific behaviour: dim depends on
        # the deployed model and may not match what credentials say.
        # Sabotage-proof: if embed_batch didn't update _embed_dimension,
        # dimension() would stay at the configured 384 forever even
        # when the model returned 5-dim vectors. Verified by commenting
        # out the assignment in embed_batch.
        transport = _VariableEmbedTransport(vectors=[[1.0, 2.0, 3.0, 4.0, 5.0]])
        provider = OllamaProvider(
            credentials=_ollama_credentials(dims=384),
            transport_client=transport,
        )

        provider.embed_batch(["alpha"])

        assert provider.dimension() == 5

    @pytest.mark.unit
    def test_healthcheck_ok_when_embed_succeeds(self) -> None:
        # Sabotage-proof: if healthcheck() stopped catching ProviderError
        # the embed_batch failure path would raise rather than emit
        # ok=False; mutating the try/except to bare-pass would also fail
        # the warm_ms assertion (it would be None).
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_RecordingTransport(),
        )

        health = provider.healthcheck()

        assert isinstance(health, ProviderHealth)
        assert health.ok is True
        assert health.endpoint == "http://localhost:11434"
        assert health.warm_ms is not None and health.warm_ms >= 0

    @pytest.mark.unit
    def test_healthcheck_not_ok_on_connection_refused(self) -> None:
        # Sabotage-proof: if healthcheck() didn't emit the typed-class
        # name on failure, downstream JSON serialisation in
        # probe-config would lose the failure category.
        err = ConnectionRefusedError("Connection refused")
        provider = OllamaProvider(
            credentials=_ollama_credentials(),
            transport_client=_RaisingTransport(err),
        )

        health = provider.healthcheck()

        assert health.ok is False
        assert health.error == "ProviderUnreachable"
        # Endpoint is normalised (trailing slashes stripped) but the
        # host:port survives — the operator sees which URL was tried.
        assert health.endpoint == "http://localhost:11434"

    @pytest.mark.unit
    def test_healthcheck_endpoint_strips_trailing_slash(self) -> None:
        # Sabotage-proof: if _normalize_endpoint stopped stripping,
        # the recorded endpoint would carry the trailing slash and
        # subsequent URL construction would double up to
        # "http://localhost:11434//api/embeddings".
        provider = OllamaProvider(
            credentials=_ollama_credentials(endpoint="http://localhost:11434/"),
            transport_client=_RecordingTransport(),
        )

        health = provider.healthcheck()

        assert health.endpoint == "http://localhost:11434"
