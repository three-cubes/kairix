"""Integration: ollama plugin embed + chat + error mapping end-to-end (#provider-plugin-arch IM-11).

Boundary chain:

  caller -> OllamaProvider.embed_batch / .chat
        -> recording fake transport_client
           (the json the plugin passes mirrors what the Ollama-native
            HTTP API expects; path is /api/embeddings or /api/chat;
            no auth header is emitted)

  caller -> OllamaProvider.embed_batch
        -> raising fake transport_client (ConnectionRefusedError /
           _HttpStatusStubError)
        -> _map_transport_error -> ProviderUnreachable / ClientError /
           UpstreamError

This test ties the Ollama plugin together at the integration boundary
so the Provider Protocol contract is exercised end-to-end with the
production typed-error vocabulary. The ``transport_client=`` keyword is
the documented DI seam (see ADR § Provider Protocol contract); no env
mutation, no @patch.

F1-clean, F2-clean, F5-clean.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import (
    ClientError,
    Provider,
    ProviderUnreachable,
    UpstreamError,
)
from kairix.providers.ollama import OllamaProvider

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Recording fake mirroring the Ollama-native HTTP surface
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """Recording transport that returns a stub embedding / chat body.

    Mirrors the :class:`kairix.providers.ollama.OllamaTransport`
    Protocol: a single ``post(path, json)`` method that returns the
    decoded JSON dict the production httpx transport would produce.
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


# ---------------------------------------------------------------------------
# Raising fake — drives the canonical error mapping
# ---------------------------------------------------------------------------


class _HttpStatusStubError(Exception):
    """Stand-in for the production ``_HttpStatusError`` carrying ``status_code``."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"upstream HTTP {status_code}")


class _RaisingTransport:
    def __init__(self, err: BaseException) -> None:
        self._err = err

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        del path, json
        raise self._err


def _credentials(
    *,
    endpoint: str = "http://localhost:11434",
    model: str = "nomic-embed-text",
    dims: int = 0,
) -> Credentials:
    # Ollama is unauthenticated; api_key is intentionally empty.
    return Credentials(api_key="", endpoint=endpoint, model=model, dims=dims)


# ---------------------------------------------------------------------------
# Happy path: embed + chat
# ---------------------------------------------------------------------------


def test_embed_records_native_path_and_per_text_fanout() -> None:
    """Sabotage-proof: regress the embed loop to send all texts in one
    request → call count would be 1 instead of 3, fails. Or regress the
    path to ``/v1/embeddings`` (OpenAI shape) → path assert fails.
    """
    transport = _RecordingTransport(embed_vector=[0.7, 0.8, 0.9])
    provider = OllamaProvider(
        credentials=_credentials(model="nomic-embed-text"),
        transport_client=transport,
    )

    vectors = provider.embed_batch(["alpha", "beta", "gamma"])

    assert vectors == [[0.7, 0.8, 0.9], [0.7, 0.8, 0.9], [0.7, 0.8, 0.9]]
    assert len(transport.calls) == 3
    paths = {call["path"] for call in transport.calls}
    assert paths == {"/api/embeddings"}
    prompts = [call["json"]["prompt"] for call in transport.calls]
    assert prompts == ["alpha", "beta", "gamma"]


def test_chat_records_native_chat_path_with_stream_false() -> None:
    """Sabotage-proof: drop ``stream=False`` from the chat body →
    body has no ``stream`` key, fails. Or regress the path to
    ``/api/generate`` → path assert fails.
    """
    transport = _RecordingTransport(chat_content="hi from ollama")
    provider = OllamaProvider(
        credentials=_credentials(model="llama3:8b"),
        transport_client=transport,
    )

    out = provider.chat([{"role": "user", "content": "ping"}], max_tokens=222)

    assert out == "hi from ollama"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["path"] == "/api/chat"
    body = call["json"]
    assert body["stream"] is False
    assert body["model"] == "llama3:8b"
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    assert body["options"]["num_predict"] == 222


def test_runtime_protocol_isinstance() -> None:
    """Sabotage-proof: removing any of name / embed_batch / chat /
    dimension / healthcheck from ``OllamaProvider`` breaks
    isinstance(provider, Provider) at runtime.
    """
    provider = OllamaProvider(
        credentials=_credentials(),
        transport_client=_RecordingTransport(),
    )
    assert isinstance(provider, Provider)


# ---------------------------------------------------------------------------
# Error mapping: connection refused / 404 / 5xx
# ---------------------------------------------------------------------------


def test_connection_refused_maps_to_provider_unreachable() -> None:
    """Sabotage-proof: drop the connection-failure early-return in
    ``_map_transport_error`` → bare ``ProviderError`` raised, the
    isinstance check fails. Verified.
    """
    transport = _RaisingTransport(ConnectionRefusedError("Connection refused"))
    provider = OllamaProvider(
        credentials=_credentials(endpoint="http://localhost:11434"),
        transport_client=transport,
    )

    with pytest.raises(ProviderUnreachable) as exc_info:
        provider.embed_batch(["x"])
    message = str(exc_info.value)
    assert "ollama" in message.lower()
    assert "localhost:11434" in message


def test_404_maps_to_client_error_naming_model() -> None:
    """Sabotage-proof: drop the 404 branch in ``_map_transport_error``
    → ClientError still raised via the generic 4xx branch, but the
    fix-hint message ``ollama pull`` would be absent. Verified.
    """
    transport = _RaisingTransport(_HttpStatusStubError(404))
    provider = OllamaProvider(
        credentials=_credentials(model="not-pulled"),
        transport_client=transport,
    )

    with pytest.raises(ClientError) as exc_info:
        provider.embed_batch(["x"])
    assert "not-pulled" in str(exc_info.value)
    assert "ollama pull" in str(exc_info.value)


def test_500_maps_to_upstream_error_with_status_code() -> None:
    """Sabotage-proof: drop status_code from UpstreamError → AttributeError
    on the assertion below.
    """
    transport = _RaisingTransport(_HttpStatusStubError(503))
    provider = OllamaProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(UpstreamError) as exc_info:
        provider.embed_batch(["x"])
    assert exc_info.value.status_code == 503


def test_dimension_adapts_to_first_observed_embed_response() -> None:
    """Sabotage-proof: if embed_batch didn't update ``_embed_dimension``
    after the first response, ``dimension()`` would stay at the
    configured ``dims=384`` even though the model returned a 5-dim
    vector. Verified by commenting out the assignment.
    """
    transport = _RecordingTransport(embed_vector=[1.0, 2.0, 3.0, 4.0, 5.0])
    provider = OllamaProvider(
        credentials=_credentials(dims=384),
        transport_client=transport,
    )

    provider.embed_batch(["x"])

    assert provider.dimension() == 5
