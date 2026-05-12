"""Dependency container for the embedding pipeline.

Production code calls run_embed() without deps — defaults are wired to
real services via ``default_factory`` references on each field. Tests
construct ``EmbedDependencies`` with explicit fake callables and never
reach the production defaults.

Each field is a typed (non-Optional) ``Callable``; production wiring
lives in ``_deps_defaults`` so mypy can narrow the dataclass shape and
no caller needs ``assert deps.x is not None`` lines (the
``Optional[Callable]`` pattern flagged in #204).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from kairix.core.embed._deps_defaults import (
    default_embed_batch,
    default_get_azure_config,
    default_get_document_root,
    default_migrate_content_vectors,
    default_open_usearch_index,
    default_preflight_check,
)


@dataclass
class EmbedDependencies:
    """Injectable dependencies for ``run_embed``.

    Each field defaults via ``default_factory`` to a thin wrapper around
    the real production implementation (see
    ``kairix.core.embed._deps_defaults``). Tests pass fakes explicitly:

    .. code-block:: python

        deps = EmbedDependencies(
            get_azure_config=lambda: ("k", "e", "d"),
            preflight_check=lambda *_: 1536,
            embed_batch=lambda texts, *a, **kw: [[0.0] * 1536 for _ in texts],
            open_usearch_index=lambda: None,
            migrate_content_vectors=lambda _db: None,
            get_document_root=lambda: None,
        )

    All fields are non-Optional callables — callers do not need to guard
    against ``None`` (closes the ``narrow on Optional`` regression in
    #204).
    """

    get_azure_config: Callable[[], tuple[str, str, str]] = field(default_factory=lambda: default_get_azure_config)
    preflight_check: Callable[[str, str, str], int] = field(default_factory=lambda: default_preflight_check)
    embed_batch: Callable[..., list[list[float]]] = field(default_factory=lambda: default_embed_batch)
    open_usearch_index: Callable[[], object | None] = field(default_factory=lambda: default_open_usearch_index)
    migrate_content_vectors: Callable[[sqlite3.Connection], None] = field(
        default_factory=lambda: default_migrate_content_vectors
    )
    get_document_root: Callable[[], str | None] = field(default_factory=lambda: default_get_document_root)
