"""In-process request coalescer for transport.

See docs/architecture/provider-plugin-architecture.md. Re-exports the
current ``EmbedCoalescer`` from ``kairix.core.embed.embed_coalescer``.
Wave 2 (IM-2) will flip the canonical path; this shim keeps imports
working during the migration so consumers can opportunistically switch
to the ``kairix.transport.coalesce`` namespace ahead of the move.
"""

from kairix.core.embed.embed_coalescer import (
    EmbedCoalescer,
    get_embed_coalescer,
    reset_embed_coalescer,
)

__all__ = ["EmbedCoalescer", "get_embed_coalescer", "reset_embed_coalescer"]
