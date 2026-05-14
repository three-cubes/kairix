"""
Shared retrieval interface for eval tooling.

Consolidates the _retrieve() implementations from runner.py, generate.py,
and hybrid_sweep.py into a single module. All eval code should import
retrieve() from here rather than maintaining local wrappers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """Unified result from any retrieval backend."""

    paths: list[str]
    snippets: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def _default_pipeline_builder(*, config: Any) -> Any:
    """Production pipeline factory — wraps ``build_search_pipeline``.

    The lazy local-import avoids a module-import-time circular when
    callers reach ``kairix.quality.eval.retrieval`` before
    ``kairix.core.factory`` finishes loading. The wrapper exists so
    ``RetrievalDeps.pipeline_builder`` has a stable, typed default.
    """
    from kairix.core.factory import build_search_pipeline

    return build_search_pipeline(config=config)


@dataclass
class RetrievalDeps:
    """Injectable dependencies for ``retrieve`` and ``_retrieve_hybrid``.

    Replaces the F6-violating ``search_fn=None`` / ``pipeline_builder=None``
    test-only kwargs with a typed dataclass. Production code calls
    ``retrieve(...)`` without ``deps`` and the default factory wires the
    real ``build_search_pipeline``. Tests construct
    ``RetrievalDeps(searcher=fake)`` (to bypass pipeline construction) or
    ``RetrievalDeps(pipeline_builder=spy)`` (to observe the resolved
    ``RetrievalConfig`` that flows through).

    ``searcher`` stays Optional because the two seams are mutually
    exclusive: a pre-bound searcher means "skip pipeline construction"
    and ``None`` means "build the pipeline via ``pipeline_builder``."
    The name doesn't end in ``_fn`` so it stays clear of F6 even with
    the ``None`` default.

    ``pipeline_builder`` is non-Optional with a ``default_factory`` (per
    CLAUDE.md F6 guidance) so mypy sees the production callable directly —
    no ``assert deps.x is not None`` ladder is needed inside ``retrieve``.
    """

    pipeline_builder: Callable[..., Any] = field(default_factory=lambda: _default_pipeline_builder)
    searcher: Callable[..., Any] | None = None


def retrieve(
    query: str,
    system: str = "hybrid",
    agent: str | None = None,
    limit: int = 10,
    db_path: str | None = None,
    collection: str | None = None,
    collections: list[str] | None = None,
    fusion_override: str | None = None,
    config: Any | None = None,
    deps: RetrievalDeps | None = None,
) -> RetrievalResult:
    """
    Run retrieval and return a RetrievalResult.

    Args:
        query:            Search query text.
        system:           Backend: 'hybrid', 'bm25', 'mock', or 'mock-reflib'.
        agent:            Agent name for collection scoping (hybrid/bm25).
        limit:            Max results (bm25/mock backends).
        db_path:          Optional path to a specific database.
        collection:       Single collection name (converted to collections list).
        collections:      Explicit collections list (takes precedence over collection).
        fusion_override:  Override fusion strategy (hybrid only).
        config:           Pre-built RetrievalConfig (hybrid only; overrides fusion_override).
        deps:             Injectable dependencies. Tests construct
                          ``RetrievalDeps(searcher=fake)`` or
                          ``RetrievalDeps(pipeline_builder=spy)``; production omits the
                          kwarg and the default factory wires
                          ``build_search_pipeline``.

    Returns:
        RetrievalResult with paths, snippets, and metadata.

    Raises:
        ValueError: Unknown system name.
    """
    _ = db_path  # caller-symmetry param; resolved via kairix.paths internally
    deps = deps if deps is not None else RetrievalDeps()
    if system == "hybrid":
        return _retrieve_hybrid(
            query=query,
            agent=agent,
            collection=collection,
            collections=collections,
            fusion_override=fusion_override,
            config=config,
            deps=deps,
        )
    elif system == "bm25":
        return _retrieve_bm25(query=query, agent=agent, limit=limit)
    elif system == "mock":
        from kairix.quality.benchmark.mock_retrieval import mock_retrieve

        paths, snippets, meta = mock_retrieve(query=query, limit=limit)
        return RetrievalResult(paths=paths, snippets=snippets, meta=meta)
    elif system == "mock-reflib":
        from kairix.quality.benchmark.mock_reflib_retrieval import mock_reflib_retrieve

        paths, snippets, meta = mock_reflib_retrieve(query=query, limit=limit)
        return RetrievalResult(paths=paths, snippets=snippets, meta=meta)
    else:
        raise ValueError(f"Unknown system: {system!r}. Use 'hybrid', 'bm25', 'mock', or 'mock-reflib'.")


def _retrieve_hybrid(
    query: str,
    agent: str | None = None,
    collection: str | None = None,
    collections: list[str] | None = None,
    fusion_override: str | None = None,
    config: Any | None = None,
    deps: RetrievalDeps | None = None,
) -> RetrievalResult:
    """Hybrid search backend.

    Resolves the effective ``RetrievalConfig`` before building the pipeline:
    explicit ``config=`` wins; otherwise per-collection overrides for a
    single-collection scope are merged on top of the YAML global config;
    ``fusion_override`` is then layered on top. This closes #112 for the
    eval/benchmark path so ``--collection reference-library`` actually
    receives reflib's tuned overrides.
    """
    deps = deps if deps is not None else RetrievalDeps()
    # Resolve config BEFORE building the pipeline. The historical bug
    # (config reassigned after pipeline already built) is closed by doing
    # all resolution up front.
    if config is None:
        from kairix.core.search.config_loader import resolve_retrieval_config

        config = resolve_retrieval_config(collection=collection, collections=collections)

    if fusion_override:
        from dataclasses import replace

        config = replace(config, fusion_strategy=fusion_override)

    searcher = deps.searcher
    if searcher is None:
        _pipeline = deps.pipeline_builder(config=config)
        searcher = _pipeline.search

    # Build explicit collections list when --collection is set
    effective_collections = collections or ([collection] if collection else None)

    search_kwargs: dict[str, Any] = {
        "query": query,
        "budget": 500_000,
        "collections": effective_collections,
    }
    if agent:
        search_kwargs["agent"] = agent
        search_kwargs["scope"] = "shared+agent"

    sr = searcher(**search_kwargs)
    paths = [b.result.path for b in sr.results]
    snippets = [b.content[:500] for b in sr.results]
    meta = {
        "intent": sr.intent.value,
        "bm25_count": sr.bm25_count,
        "vec_count": sr.vec_count,
        "fused_count": sr.fused_count,
        "vec_failed": sr.vec_failed,
        "latency_ms": round(sr.latency_ms, 1),
    }
    return RetrievalResult(paths=paths, snippets=snippets, meta=meta)


def _retrieve_bm25(
    query: str,
    agent: str | None = None,
    limit: int = 10,
) -> RetrievalResult:
    """BM25-only backend."""
    from kairix.core.search.bm25 import bm25_search

    results = bm25_search(query=query, agent=agent, limit=limit)
    paths = [r["file"] for r in results]
    snippets = [r.get("snippet") or "" for r in results]
    return RetrievalResult(paths=paths, snippets=snippets, meta={"system": "bm25"})
