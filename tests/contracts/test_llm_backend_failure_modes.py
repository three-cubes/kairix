"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`LLMBackend`.

``LLMBackend`` documents a "never raises" contract: on provider failure
``chat`` returns ``""`` and ``embed`` returns ``[]``. Consumers
short-circuit on those shapes — e.g. :class:`LLMFactExtractor` must emit
no facts (rather than crash the ingest) when the chat reply is empty.

One body per method, two implementations (F43 limb 2):

* the real :class:`AzureOpenAIBackend` (the default backend) wired through
  :class:`LLMBackendDeps` to the production provider adapters
  (``default_chat_callable`` / :class:`ProviderEmbeddingService`) over a
  real :class:`OllamaProvider` whose transport refuses the connection;
* the canonical :class:`tests.fakes.FakeLLMBackend` configured with the
  contract's failure shapes.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

import pytest

from kairix.core.facts import LLMFactExtractor
from kairix.credentials import Credentials
from kairix.platform.llm.backends import AzureOpenAIBackend, LLMBackendDeps, default_chat_callable
from kairix.providers.ollama import OllamaProvider
from kairix.transport.cache.embed_cache import EmbedCache
from kairix.transport.embed_service import ProviderEmbeddingService
from tests.fakes import FakeLLMBackend, FakeOllamaTransport

pytestmark = pytest.mark.contract


def _unreachable_provider() -> OllamaProvider:
    return OllamaProvider(
        Credentials(api_key="", endpoint="http://ollama.invalid:11434", model="llama3", dims=768),
        transport_client=FakeOllamaTransport(raises=ConnectionRefusedError("F68-sidecar-stopped")),
    )


def _real_backend() -> AzureOpenAIBackend:
    provider = _unreachable_provider()
    cache = EmbedCache()
    embed_service = ProviderEmbeddingService(
        provider,
        existing_coalescer_fn=lambda: None,
        coalescer_factory=lambda **_kwargs: None,
        get_embed_cache_fn=lambda: cache,
    )
    return AzureOpenAIBackend(
        LLMBackendDeps(
            chat=partial(default_chat_callable, provider_resolver=lambda: provider),
            embed=embed_service.embed,
        )
    )


_FACTORIES: list[Callable[[], Any]] = [
    _real_backend,
    lambda: FakeLLMBackend(chat_responses=[], embed_empty=True),
]
_IDS = ["real-default-backend", "fake"]

_TURNS = [
    {"id": "t1", "role": "user", "content": "We moved the deploy window to Thursdays."},
    {"id": "t2", "role": "assistant", "content": "Noted — Thursday deploys from now on."},
]


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_chat_returns_empty_when_provider_unreachable_fact_extractor_emits_nothing(
    factory: Callable[[], Any],
) -> None:
    """The provider is down: ``chat`` returns ``""`` (never raises) and the
    fact extractor consuming it returns no facts instead of crashing.

    Sabotage proof: in ``kairix/transport/embed_service.py::ProviderChatBackend.chat``
    replace the ``except Exception`` body's ``return ""`` with ``raise``.
    Re-run: the real case fails with ``ProviderUnreachable`` escaping the
    backend. Restored.
    """
    backend = factory()

    assert backend.chat([{"role": "user", "content": "ping"}], max_tokens=16) == ""
    assert LLMFactExtractor(llm=backend).extract(turns=_TURNS) == []


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_embed_returns_empty_vector_when_provider_unreachable(factory: Callable[[], Any]) -> None:
    """The provider is down: ``embed`` returns ``[]`` (never raises), and a
    retry is not served a poisoned cache entry.

    Sabotage proof: in ``ProviderEmbeddingService.embed`` replace the
    direct-dispatch ``except Exception`` body's ``return []`` with
    ``raise``. Re-run: the real case fails with ``ProviderUnreachable``.
    Restored.
    """
    backend = factory()

    assert backend.embed("thursday deploy window") == []
    assert backend.embed("thursday deploy window") == []
