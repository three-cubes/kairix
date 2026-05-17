"""Universal endpoint response cache.

See docs/architecture/provider-plugin-architecture.md. Re-exports the
current ``EmbedCache`` from ``kairix.core.embed.embed_cache``. Wave 2
(IM-3) will flip the canonical path; this shim keeps imports working
during the migration so consumers can opportunistically switch to the
``kairix.transport.cache`` namespace ahead of the move.
"""

from kairix.core.embed.embed_cache import EmbedCache, get_embed_cache

__all__ = ["EmbedCache", "get_embed_cache"]
