"""Provider → ``EmbeddingService`` adapter for the kairix transport layer.

Adapts a :class:`kairix.providers.Provider` (the plugin Protocol) to the
:class:`kairix.core.protocols.EmbeddingService` Protocol that the search
pipeline consumes. Lives in ``kairix.transport`` because it threads two
transport-layer concerns — the per-text ``EmbedCache`` and the
process-shared ``EmbedCoalescer`` — between the provider plugin and the
domain layer.

Why this module exists
----------------------

In v2026.5.17 the plugin layer (``kairix/providers/<name>/``) is the
only embed/chat path. ``kairix/core/search/backends.py`` previously
hosted ``AzureEmbeddingService`` which delegated to the now-deprecated
``kairix._azure.embed_text``. The replacement contract is
provider-agnostic — every plugin satisfies ``Provider.embed_batch`` —
so the adapter sits at the transport boundary and owns the cache +
coalescer routing.

F26 (``kairix/core/**`` may not import ``kairix/providers/**`` or
``kairix/transport/**``) makes this the only legal home for an adapter
that imports both layers.

Construction
------------

Production callers build the adapter in ``kairix.core.factory`` (and
similar boundary modules) via::

    from kairix.providers import get_provider
    from kairix.paths import provider_name
    from kairix.transport.embed_service import ProviderEmbeddingService

    name = provider_name()
    if name is None:
        raise ValueError("kairix.config.yaml is missing the required 'provider:' field")
    provider = get_provider(name)
    embedding_service = ProviderEmbeddingService(provider)

Tests construct ``ProviderEmbeddingService(FakeProvider(...))`` from
``tests/fakes.py`` — no env-var mutation, no patching.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kairix.providers import Provider

logger = logging.getLogger(__name__)


class ProviderEmbeddingService:
    """Adapt a :class:`kairix.providers.Provider` to the ``EmbeddingService`` Protocol.

    Single-text embeds (``embed``) route through the process-shared
    :class:`kairix.transport.cache.EmbedCache` and (when wired) the
    :class:`kairix.transport.coalesce.EmbedCoalescer` so concurrent
    callers asking the same / nearby questions pay one provider
    round-trip total. Batched embeds (``embed_batch``) bypass the cache
    because the coalescer's call shape is per-text — bulk callers
    (worker ingestion, suite builders) own their own batching strategy.

    Failure contract — matches the legacy
    :class:`kairix.core.search.backends.AzureEmbeddingService`:

      - ``embed(text)`` returns ``[]`` on plugin error (so the search
        pipeline can short-circuit rather than abort the surrounding
        request).
      - ``embed_batch(texts)`` returns ``[[]]`` per text on plugin
        error (so the caller can attribute partial failure per text).

    The plugin itself raises canonical typed errors (see
    ``kairix.providers._errors``); this adapter swallows them on the
    single-text path so the protocol's "never raises" contract holds.

    Construction:

      ``provider``: the resolved plugin. Production callers build it
      via ``kairix.providers.get_provider(provider_name())``; tests
      pass a ``FakeProvider`` from ``tests/fakes.py``.
    """

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def embed(self, text: str) -> list[float]:
        """Embed a single text string. Returns ``[]`` on failure.

        Routes through the process-shared cache and (when wired) the
        request coalescer so concurrent callers asking the same /
        nearby questions pay one round-trip total. Matches the hot-path
        wiring previously owned by ``kairix._azure.embed_text``.
        """
        if not text or not text.strip():
            return []

        from kairix.transport.cache import get_embed_cache
        from kairix.transport.coalesce import embed_coalescer as embed_coalescer_mod
        from kairix.transport.coalesce import get_embed_coalescer

        cache = get_embed_cache()
        cached = cache.get(text)
        if cached is not None:
            return cached

        # Coalescer routing: if a singleton is already installed (test
        # pre-installation or a previous production warm-up), use it.
        # Otherwise lazily build one with this provider's batch dispatcher
        # so concurrent agents fold into one provider round-trip.
        existing = embed_coalescer_mod._EMBED_COALESCER
        if existing is not None:
            result = existing.embed(text)
            if result:
                cache.put(text, result)
            return result

        coalescer = get_embed_coalescer(embed_batch=self._provider.embed_batch)
        if coalescer is not None:
            result = coalescer.embed(text)
            if result:
                cache.put(text, result)
            return result

        # No coalescer available (window=0 or sequential/test path).
        # Dispatch directly through the plugin; honour the
        # "never raises" protocol contract by swallowing transport errors.
        try:
            vectors = self._provider.embed_batch([text])
        except Exception as exc:
            logger.warning("ProviderEmbeddingService.embed: provider raised — %s", exc)
            return []
        if not vectors or not vectors[0]:
            return []
        embedding = list(vectors[0])
        cache.put(text, embedding)
        return embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in one provider round-trip.

        Bypasses the per-text cache + coalescer — bulk callers (worker
        ingestion, suite builders) own their own batching and don't
        benefit from coalescing within a batch. Returns ``[]`` for
        every text on plugin failure so callers can short-circuit
        per-text without aborting the surrounding batch.
        """
        if not texts:
            return []
        try:
            return self._provider.embed_batch(list(texts))
        except Exception as exc:
            logger.warning("ProviderEmbeddingService.embed_batch: provider raised — %s", exc)
            return [[] for _ in texts]


class ProviderChatBackend:
    """Adapt a :class:`kairix.providers.Provider` to the ``LLMBackend`` Protocol.

    Implements the ``chat(messages, max_tokens=800) -> str`` shape that
    :mod:`kairix.platform.llm` consumes (production: query-planner
    decompose calls, briefing synthesiser, summaries generation). The
    plugin's :meth:`Provider.chat` already speaks the same shape, so
    this adapter is mostly a "never raises" wrapper: it returns ``""``
    on plugin error rather than propagating the exception, honouring
    the ``LLMBackend`` contract that callers can short-circuit on an
    empty reply.

    Wiring of the eval module's :class:`~kairix.core.protocols.ChatBackend`
    surface (``complete(prompt, *, api_key, endpoint, deployment,
    system, temperature, timeout_s) -> str``) is a follow-up — every
    eval-side caller (LLMJudge / QueryGenerator / ProductionLLMJudge)
    needs to migrate from the deprecated
    :class:`kairix._azure.AzureChatBackend` adapter at the same time,
    so the change is sequenced into the same wave that deletes
    ``kairix/_azure.py``.

    Construction:

      ``provider``: the resolved plugin. Production callers build it
      via ``kairix.providers.get_provider(provider_name())``; tests
      pass a ``FakeProvider`` from ``tests/fakes.py``.
    """

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    def chat(self, messages: list[dict[str, object]], max_tokens: int = 800) -> str:
        """Run a chat completion through the configured plugin.

        Translates the ``LLMBackend`` ``chat(messages, max_tokens)``
        protocol shape into the plugin's :meth:`Provider.chat` call.
        Returns ``""`` on any plugin error so callers (synthesiser,
        query planner, summaries) can short-circuit on the empty reply
        without aborting the surrounding workflow.
        """
        try:
            return self._provider.chat(list(messages), max_tokens=max_tokens)
        except Exception as exc:
            logger.warning("ProviderChatBackend.chat: provider raised — %s", exc)
            return ""


__all__ = ["ProviderChatBackend", "ProviderEmbeddingService"]
