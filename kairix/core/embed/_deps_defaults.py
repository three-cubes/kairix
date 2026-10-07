"""Lazy production-default callables for ``EmbedDependencies``.

Each function is the production wiring for one ``EmbedDependencies``
field. They live in a sibling module — *not* in ``deps.py`` — for
two reasons:

  - The lazy ``from kairix.core.embed.embed import ...`` imports inside
    each default break the ``embed.py → deps.py → embed.py`` cycle that
    would otherwise form if the imports were top-level on ``deps.py``.
    Putting the laziness in standalone wrappers (rather than inside a
    dataclass ``__post_init__``) keeps ``deps.py`` itself import-cheap
    and lets ``EmbedDependencies`` use ``default_factory`` (which mypy
    can narrow), removing the ``Optional[Callable]`` self-resolving
    pattern flagged in #204.

  - Production wiring lives in one place; unit tests inject fakes at
    the dataclass boundary and never reach this module. The defaults
    are exercised in aggregate by the integration suite.

All defaults are addressed by their fully-qualified name in
``deps.py`` so the dataclass shape is purely typed callables.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


def default_get_azure_config() -> tuple[str, str, str]:
    """Production default for ``EmbedDependencies.get_azure_config``."""
    from kairix.core.embed.embed import get_azure_config_from_credentials

    return get_azure_config_from_credentials()


def default_preflight_check(api_key: str, endpoint: str, deployment: str, **kwargs: Any) -> int:
    """Production default for ``EmbedDependencies.preflight_check``.

    ``**kwargs`` pass through to :func:`kairix.core.embed.embed.preflight_check`
    (e.g. its ``client=`` seam), mirroring :func:`default_embed_batch`.
    """
    from kairix.core.embed.embed import preflight_check

    return preflight_check(api_key, endpoint, deployment, **kwargs)


def default_embed_batch(
    texts: list[str],
    api_key: str,
    endpoint: str,
    deployment: str,
    dims: int = 3072,
    **kwargs: Any,
) -> list[list[float]]:
    """Production default for ``EmbedDependencies.embed_batch``."""
    from kairix.core.embed.embed import embed_batch

    return embed_batch(texts, api_key, endpoint, deployment, dims, **kwargs)


def default_open_usearch_index() -> Any:
    """Production default for ``EmbedDependencies.open_usearch_index``."""
    from kairix.core.embed.embed import open_default_usearch_index

    return open_default_usearch_index()


def default_migrate_content_vectors(db: sqlite3.Connection) -> None:
    """Production default for ``EmbedDependencies.migrate_content_vectors``."""
    from kairix.core.embed.schema import migrate_content_vectors

    migrate_content_vectors(db)


def default_open_embedding_cache() -> Any:
    """Production default for ``EmbedDependencies.open_embedding_cache``.

    Resolves the path under ``KAIRIX_DOCUMENT_ROOT/.kairix/cache/`` via
    the F4 paths boundary and returns an open
    :class:`kairix.core.embed.embedding_cache.EmbeddingCache`. Returning
    ``None`` on path-layer failure keeps the embed pipeline running
    without the cache safety net — the operator still gets vectors,
    just without restart-resilience for this cycle.

    Test-isolation guard: pytest sets ``PYTEST_CURRENT_TEST`` for every
    test it runs. When that env var is present we refuse to construct
    a cache against the production path resolution, since test fixtures
    that haven't explicitly injected a cache via
    ``EmbedDependencies(open_embedding_cache=...)`` would otherwise
    leak cache files into the developer's real document root and bleed
    state between unrelated tests. Test code that genuinely wants the
    cache path exercised passes a ``tmp_path``-backed
    :class:`EmbeddingCache` through the constructor seam.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        from kairix.core.embed.embedding_cache import EmbeddingCache
        from kairix.paths import embedding_cache_path

        return EmbeddingCache(embedding_cache_path())
    except Exception as e:
        logger.warning("default_open_embedding_cache: cache unavailable — %s", e)
        return None


def default_get_reflib_index_mode() -> str:
    """Production default for ``EmbedDependencies.get_reflib_index_mode``.

    Resolves the ``reference_library.index`` mode (#475) from
    kairix.config.yaml. A missing or unreadable file yields the eager
    default (today's behaviour); a present-but-invalid value raises
    ``ConfigValidationError`` loudly inside ``load_reference_library`` —
    an operator who asked for ``skip`` must not silently get a full
    reference-library re-embed.
    """
    from kairix.core.search.config_loader import load_reference_library

    return load_reference_library().index


def default_get_document_root() -> str | None:
    """Production default for ``EmbedDependencies.get_document_root``.

    Resolution failures are tolerated — the embed pipeline only uses the
    document root for chunk-date heuristics. Returning ``None`` lets the
    pipeline run without crashing when the kairix paths layer is
    unavailable (e.g. a test process with no kairix.config.yaml on disk).
    """
    try:
        from kairix.paths import document_root

        return str(document_root())
    except Exception as e:
        logger.warning("default_get_document_root: paths layer unavailable — %s", e)
        return None
