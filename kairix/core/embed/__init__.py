"""Public surface for the kairix embed package.

This package owns the worker-ingestion embed pipeline (chunking, schema,
batch run loop). Single-text embedding for the search pipeline is
handled by :class:`kairix.transport.embed_service.ProviderEmbeddingService`,
which adapts a configured :class:`kairix.providers.Provider` plugin to
the :class:`kairix.core.protocols.EmbeddingService` Protocol.

The legacy ``embed_text`` re-export was removed in v2026.5.17 along
with ``kairix._azure``. Callers needing a single-text embed should
resolve a provider via :func:`kairix.providers.get_provider` and wrap
it in ``ProviderEmbeddingService``.
"""

__all__: list[str] = []
