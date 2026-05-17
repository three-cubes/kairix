"""kairix.transport.cache — universal embed response cache.

Canonical home of :class:`EmbedCache`. See
docs/architecture/provider-plugin-architecture.md for the three-layer
split (core / transport / providers); the embed cache sits in the
transport layer in front of every provider's embed call so a single
implementation services all providers.

Public surface re-exported here so callers import from the package,
not the implementation module.
"""

from kairix.transport.cache.embed_cache import EmbedCache, get_embed_cache, reset_embed_cache

__all__ = ["EmbedCache", "get_embed_cache", "reset_embed_cache"]
