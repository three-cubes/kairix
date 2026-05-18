"""Integration: AzureFoundryProvider uses LLM credentials for chat, embed credentials for embeddings.

Pins the production-incident regression from v2026.5.19a1: the plugin
factory passed embed credentials (endpoint+model) to the provider and
``chat()`` therefore POSTed to the embed endpoint with the embed model
name (``text-embedding-3-large``), which Azure Foundry rejects with
``400 The requested operation is unsupported.``.

The fix splits credentials: ``embed_batch()`` keeps using the embed
endpoint+model; ``chat()`` uses the LLM endpoint+model. Sabotage-proof:
revert the ``llm_credentials`` plumbing in ``provider.chat`` (or
``make_provider``) and either:

- the chat-side assertion below fires (wrong endpoint / wrong model
  name on the wire), or
- the call falls back to the embed endpoint and a real Foundry
  deployment would 400 — captured here by asserting the wire kwargs
  carry the LLM model name, not the embed model name.

Lives at the integration tier because it exercises the production
``make_provider`` factory through its public ``credentials_resolver=``
seam, plus the wire shape of both ``chat`` and ``embed_batch`` against
a recording transport — F1-clean (DI through documented kwargs, no
monkeypatch).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers.azure_foundry import AzureFoundryProvider, make_provider

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Recording transport — minimal SDK-shaped surface covering chat + embed
# ---------------------------------------------------------------------------


@dataclass
class _FakeChatMessage:
    content: str | None


@dataclass
class _FakeChatChoice:
    message: _FakeChatMessage


@dataclass
class _FakeChatResponse:
    choices: list[_FakeChatChoice]


class _RecordingChatCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeChatResponse:
        self.calls.append(dict(kwargs))
        return _FakeChatResponse(choices=[_FakeChatChoice(message=_FakeChatMessage(content="ok"))])


class _RecordingChat:
    def __init__(self) -> None:
        self.completions = _RecordingChatCompletions()


@dataclass
class _FakeEmbeddingData:
    embedding: list[float]


@dataclass
class _FakeEmbeddingResponse:
    data: list[_FakeEmbeddingData]


class _RecordingEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeEmbeddingResponse:
        self.calls.append(dict(kwargs))
        # Return a single fake vector per input
        n = len(kwargs.get("input", []))
        return _FakeEmbeddingResponse(data=[_FakeEmbeddingData(embedding=[0.0] * 4) for _ in range(n)])


class _RecordingTransportClient:
    def __init__(self) -> None:
        self.chat = _RecordingChat()
        self.embeddings = _RecordingEmbeddings()


# ---------------------------------------------------------------------------
# Distinct LLM vs embed credentials — the production setup that broke a9
# ---------------------------------------------------------------------------

_EMBED_CREDS = Credentials(
    api_key="embed-test-key",  # pragma: allowlist secret
    endpoint="https://example-resource.services.ai.azure.com",
    model="text-embedding-3-large",
    dims=1536,
)
_LLM_CREDS = Credentials(
    api_key="llm-test-key",  # pragma: allowlist secret
    endpoint="https://example-resource.services.ai.azure.com/api/projects/proj/openai/v1",
    model="gpt-5.4-mini",
    dims=1536,
)


def test_chat_uses_llm_model_not_embed_model_when_credentials_are_split() -> None:
    """``provider.chat()`` carries the LLM model name on the wire,
    even when the embed credentials use a different model.

    Sabotage-proof: change ``self._llm_credentials`` → ``self._credentials``
    in ``provider.chat`` and the wire ``model=`` becomes the embed model
    name, failing the equality below.
    """
    transport = _RecordingTransportClient()
    provider = AzureFoundryProvider(
        credentials=_EMBED_CREDS,
        transport_client=transport,
        llm_credentials=_LLM_CREDS,
    )

    provider.chat([{"role": "user", "content": "hi"}], max_tokens=50)

    call = transport.chat.completions.calls[0]
    assert call["model"] == "gpt-5.4-mini", (
        f"chat() must use the LLM model, not the embed model. Wire model: {call['model']!r}. "
        f"Embed model on creds: {_EMBED_CREDS.model!r}, LLM model on creds: {_LLM_CREDS.model!r}."
    )
    # gpt-5-prefixed model is reasoning-class → max_completion_tokens on wire
    assert "max_completion_tokens" in call, (
        f"gpt-5.4-mini is reasoning-class — wire must carry max_completion_tokens. Got kwargs: {sorted(call.keys())}"
    )
    assert call["max_completion_tokens"] == 50


def test_embed_batch_uses_embed_model_when_credentials_are_split() -> None:
    """``provider.embed_batch()`` continues to use the embed model.

    Sabotage-proof: change ``self._credentials`` → ``self._llm_credentials``
    in ``embed_batch`` and the wire ``model=`` becomes the LLM model
    name, failing the equality below.
    """
    transport = _RecordingTransportClient()
    provider = AzureFoundryProvider(
        credentials=_EMBED_CREDS,
        transport_client=transport,
        llm_credentials=_LLM_CREDS,
    )

    provider.embed_batch(["hello world"])

    call = transport.embeddings.calls[0]
    assert call["model"] == "text-embedding-3-large", (
        f"embed_batch() must use the embed model. Wire model: {call['model']!r}."
    )


def test_make_provider_passes_both_credential_bundles_to_provider() -> None:
    """The entry-point factory resolves both ``embed`` and ``llm`` purposes
    and threads both Credentials bundles into the AzureFoundryProvider.

    Sabotage-proof: revert ``make_provider`` to call only
    ``credentials_resolver("embed")`` and the assertion that the
    resolver was asked for the ``llm`` purpose fails.
    """
    seen_purposes: list[str] = []

    def _resolver(purpose: str) -> Credentials:
        seen_purposes.append(purpose)
        if purpose == "embed":
            return _EMBED_CREDS
        if purpose == "llm":
            return _LLM_CREDS
        raise ValueError(f"unexpected purpose: {purpose}")

    provider = make_provider(credentials_resolver=_resolver)

    assert "embed" in seen_purposes, "make_provider must resolve embed credentials"
    assert "llm" in seen_purposes, "make_provider must resolve llm credentials so chat() can use the LLM endpoint+model"
    # The provider now holds both; chat() should carry the LLM model on the wire.
    assert isinstance(provider, AzureFoundryProvider)
    assert provider._llm_credentials.model == "gpt-5.4-mini"
    assert provider._credentials.model == "text-embedding-3-large"


def test_chat_falls_back_to_embed_credentials_when_llm_credentials_omitted() -> None:
    """Back-compat: a single-endpoint install (or an existing test that
    only passes ``credentials=``) still works — ``chat()`` falls back to
    the embed credentials.

    Sabotage-proof: remove the ``or credentials`` fallback in
    ``AzureFoundryProvider.__init__`` and constructing the provider
    without ``llm_credentials`` raises ``AttributeError`` on the first
    chat() call.
    """
    transport = _RecordingTransportClient()
    legacy_creds = Credentials(
        api_key="legacy-test-key",  # pragma: allowlist secret
        endpoint="https://legacy.services.ai.azure.com",
        model="gpt-4o-mini",
        dims=1536,
    )
    provider = AzureFoundryProvider(credentials=legacy_creds, transport_client=transport)

    provider.chat([{"role": "user", "content": "hi"}], max_tokens=42)

    call = transport.chat.completions.calls[0]
    assert call["model"] == "gpt-4o-mini"
    assert call["max_tokens"] == 42  # gpt-4o-mini is chat-class, not reasoning
