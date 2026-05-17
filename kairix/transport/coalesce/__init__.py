"""EmbedCoalescer — universal coalescing for concurrent endpoint calls.

In-process request coalescer that folds N concurrent single-text embed
calls into one batched HTTP request, so N agents asking N different
questions in the same window pay one round-trip latency total instead
of N.

This is the canonical home for the coalescer (Wave 2 / IM-2 of the
provider-plugin-architecture migration). See
``docs/architecture/provider-plugin-architecture.md`` for the
three-layer split that places universal endpoint concerns under
``kairix/transport/`` and away from the domain layer.
"""

from kairix.transport.coalesce.embed_coalescer import (
    DEFAULT_COALESCE_WINDOW_MS,
    DEFAULT_MAX_BATCH_SIZE,
    CoalescerStats,
    EmbedCoalescer,
    get_embed_coalescer,
    reset_embed_coalescer,
)

__all__ = [
    "DEFAULT_COALESCE_WINDOW_MS",
    "DEFAULT_MAX_BATCH_SIZE",
    "CoalescerStats",
    "EmbedCoalescer",
    "get_embed_coalescer",
    "reset_embed_coalescer",
]
