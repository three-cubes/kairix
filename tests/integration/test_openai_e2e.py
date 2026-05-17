"""Integration: openai plugin embed + chat + error mapping end-to-end (#provider-plugin-arch IM-7).

Boundary chain:

  caller -> OpenAIProvider.embed_batch / .chat
        -> recording fake transport_client
           (the kwargs the plugin passes mirror what the openai-SDK
            ``OpenAI`` client would put on the wire; auth header is
            ``Authorization: Bearer <api_key>`` by SDK contract)

  caller -> OpenAIProvider.embed_batch
        -> raising fake transport_client (APIStatusError shape)
        -> _map_transport_error -> RateLimited / AuthError / UpstreamError

This test ties the OpenAI plugin together at the integration boundary
so the Provider Protocol contract is exercised end-to-end. The
``transport_client=`` keyword is the documented DI seam (see ADR §
Provider Protocol contract); no env mutation, no @patch.

F1-clean, F2-clean, F5-clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import (
    AuthError,
    Provider,
    ProviderUnreachable,
    RateLimited,
    UpstreamError,
)
from kairix.providers.openai import OpenAIProvider

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Recording fake mirroring the openai-SDK ``OpenAI`` surface
# ---------------------------------------------------------------------------


@dataclass
class _EmbedItem:
    embedding: list[float]


@dataclass
class _EmbedResponse:
    data: list[_EmbedItem]


@dataclass
class _ChatMessage:
    content: str | None


@dataclass
class _ChatChoice:
    message: _ChatMessage


@dataclass
class _ChatResponse:
    choices: list[_ChatChoice]


class _RecordingEmbeddings:
    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _EmbedResponse:
        self.calls.append(dict(kwargs))
        return _EmbedResponse(data=[_EmbedItem(embedding=list(v)) for v in self._vectors])


class _RecordingChatCompletions:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _ChatResponse:
        self.calls.append(dict(kwargs))
        return _ChatResponse(choices=[_ChatChoice(message=_ChatMessage(content=self._content))])


class _RecordingChat:
    def __init__(self, content: str) -> None:
        self.completions = _RecordingChatCompletions(content)


class _RecordingTransport:
    def __init__(
        self,
        *,
        vectors: list[list[float]] | None = None,
        chat_content: str = "hello back",
    ) -> None:
        self.embeddings = _RecordingEmbeddings(vectors or [[0.1, 0.2, 0.3]])
        self.chat = _RecordingChat(chat_content)


# ---------------------------------------------------------------------------
# Raising fake — drives the canonical error mapping
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class _UpstreamApiError(Exception):
    def __init__(self, status_code: int, *, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.response = _FakeHttpResponse(status_code, headers)
        super().__init__(f"upstream HTTP {status_code}")


class _RaisingEmbeddings:
    def __init__(self, err: BaseException) -> None:
        self._err = err

    def create(self, **kwargs: Any) -> _EmbedResponse:
        del kwargs
        raise self._err


class _RaisingChat:
    def __init__(self, err: BaseException) -> None:
        self.completions = _RaisingEmbeddings(err)


class _RaisingTransport:
    def __init__(self, err: BaseException) -> None:
        self.embeddings = _RaisingEmbeddings(err)
        self.chat = _RaisingChat(err)


def _credentials(
    *,
    api_key: str = "sk-int-openai",  # pragma: allowlist secret
    endpoint: str = "https://api.openai.com/v1",
    model: str = "text-embedding-3-large",
    dims: int = 1536,
) -> Credentials:
    return Credentials(api_key=api_key, endpoint=endpoint, model=model, dims=dims)


# ---------------------------------------------------------------------------
# Happy path: embed + chat
# ---------------------------------------------------------------------------


def test_embed_round_trips_vectors_and_records_wire_shape() -> None:
    """Sabotage-proof: drop the ``input=`` kwarg from
    ``OpenAIProvider.embed_batch`` → recorded call has no ``input``,
    fails.
    """
    transport = _RecordingTransport(vectors=[[0.7, 0.8, 0.9], [1.0, 1.1, 1.2]])
    provider = OpenAIProvider(
        credentials=_credentials(model="text-embedding-3-large"),
        transport_client=transport,
    )

    vectors = provider.embed_batch(["alpha", "beta"])

    assert vectors == [[0.7, 0.8, 0.9], [1.0, 1.1, 1.2]]
    assert len(transport.embeddings.calls) == 1
    call = transport.embeddings.calls[0]
    assert call["model"] == "text-embedding-3-large"
    assert call["input"] == ["alpha", "beta"]


def test_chat_round_trips_content_and_records_wire_shape() -> None:
    """Sabotage-proof: drop the ``messages=`` kwarg from
    ``OpenAIProvider.chat`` → recorded call missing messages, fails.
    """
    transport = _RecordingTransport(chat_content="hi from openai")
    provider = OpenAIProvider(credentials=_credentials(), transport_client=transport)

    out = provider.chat([{"role": "user", "content": "ping"}], max_tokens=222)

    assert out == "hi from openai"
    assert len(transport.chat.completions.calls) == 1
    call = transport.chat.completions.calls[0]
    assert call["max_tokens"] == 222
    assert call["messages"] == [{"role": "user", "content": "ping"}]


def test_runtime_protocol_isinstance() -> None:
    """Sabotage-proof: removing any of name / embed_batch / chat /
    dimension / healthcheck from ``OpenAIProvider`` breaks
    isinstance(provider, Provider) at runtime.
    """
    provider = OpenAIProvider(
        credentials=_credentials(),
        transport_client=_RecordingTransport(),
    )
    assert isinstance(provider, Provider)


# ---------------------------------------------------------------------------
# Error mapping: 429 / 401 / 500 / connection failure
# ---------------------------------------------------------------------------


def test_429_maps_to_rate_limited_with_retry_after() -> None:
    """Sabotage-proof: drop the 429 branch in ``_map_transport_error``
    → bare ``ProviderError`` raised, the isinstance check fails.
    """
    transport = _RaisingTransport(_UpstreamApiError(429, headers={"Retry-After": "23"}))
    provider = OpenAIProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(RateLimited) as exc_info:
        provider.embed_batch(["x"])
    assert exc_info.value.retry_after_s == 23.0


def test_401_maps_to_auth_error() -> None:
    """Sabotage-proof: drop the 401 branch → bare ``ProviderError``."""
    transport = _RaisingTransport(_UpstreamApiError(401))
    provider = OpenAIProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(AuthError) as exc_info:
        provider.embed_batch(["x"])
    assert "openai" in str(exc_info.value)


def test_500_maps_to_upstream_error() -> None:
    """Sabotage-proof: drop the 5xx branch → bare ``ProviderError``."""
    transport = _RaisingTransport(_UpstreamApiError(502))
    provider = OpenAIProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(UpstreamError) as exc_info:
        provider.embed_batch(["x"])
    assert exc_info.value.status_code == 502


def test_connection_failure_maps_to_provider_unreachable() -> None:
    """Sabotage-proof: drop the connection-failure branch → bare
    ``ProviderError`` raised, fails the isinstance check.
    """
    transport = _RaisingTransport(ConnectionError("connection refused"))
    provider = OpenAIProvider(credentials=_credentials(), transport_client=transport)

    with pytest.raises(ProviderUnreachable) as exc_info:
        provider.embed_batch(["x"])
    assert "openai" in str(exc_info.value)
