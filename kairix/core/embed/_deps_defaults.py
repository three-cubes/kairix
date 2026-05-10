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
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


def default_get_azure_config() -> tuple[str, str, str]:
    """Production default for ``EmbedDependencies.get_azure_config``."""
    from kairix.core.embed.embed import _get_azure_config

    return _get_azure_config()


def default_preflight_check(api_key: str, endpoint: str, deployment: str) -> int:
    """Production default for ``EmbedDependencies.preflight_check``."""
    from kairix.core.embed.embed import preflight_check

    return preflight_check(api_key, endpoint, deployment)


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
    from kairix.core.embed.embed import _open_usearch_index

    return _open_usearch_index()


def default_migrate_content_vectors(db: sqlite3.Connection) -> None:
    """Production default for ``EmbedDependencies.migrate_content_vectors``."""
    from kairix.core.embed.schema import migrate_content_vectors

    migrate_content_vectors(db)


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
