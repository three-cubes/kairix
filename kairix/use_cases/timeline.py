"""Timeline use case — date-aware retrieval shared by CLI and MCP.

Closes #163 (CLI/MCP timeline divergence) and lays the Phase-1 template
for #168 (CLI/MCP feature parity). The use case:

  1. Resolves a time window — either explicit ``since``/``until`` from
     the caller, or extracted from the query when both are None.
  2. Rewrites the query temporally (so vector/BM25 search sees expanded
     date phrases like "April 2026").
  3. **Primary backend:** queries the structured temporal-chunks index
     for board-card / memory-section hits in the window.
  4. **Fall-through:** if the temporal-chunks backend returns nothing
     (or no time window was detectable), runs the search pipeline on
     the rewritten query so callers always get *some* signal.

CLI and MCP both call ``run_timeline``; their adapters translate argv /
JSON in and the ``TimelineResult`` dataclass out. Adapters never own
business logic — see ``docs/architecture/cli-mcp-feature-parity.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from kairix.core.search.scope import Scope
from kairix.use_cases import _timeline_defaults as _defaults

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimelineHit:
    """A single timeline hit — uniform shape across both backends.

    The temporal-chunks backend populates ``date`` and ``chunk_type``;
    the search-pipeline fallback leaves them empty. Both populate
    ``path``, ``title``, ``snippet``, ``score``.
    """

    path: str
    title: str
    snippet: str
    score: float
    date: str = ""
    chunk_type: str = ""


@dataclass(frozen=True)
class TimelineResult:
    """Outcome of one ``run_timeline`` invocation.

    Attributes:
        original_query: The caller's query, unchanged.
        rewritten_query: Query after temporal rewriting (== original
            when no temporal expression was found).
        is_temporal: True when a time window was extracted (or supplied).
        fell_back: True when the search-pipeline fallback produced
            ``results`` (because temporal-chunks returned empty).
        time_window: ``{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}``;
            empty strings when a bound is open. ``{}`` when no window
            was detectable.
        results: Up to ``limit`` ``TimelineHit``s, best-first.
        error: Empty string on success; structured ``"<Class>: <msg>"``
            on failure (mirrors the wrap_tool_errors envelope).
    """

    original_query: str
    rewritten_query: str
    is_temporal: bool
    fell_back: bool
    time_window: dict[str, str]
    results: list[TimelineHit] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class TimelineDeps:
    """Injectable dependencies for ``run_timeline``.

    Production callers leave every field None and the use case fills in
    real implementations on first access (see ``_timeline_defaults``).
    Tests construct a ``TimelineDeps(...)`` with light-weight stand-ins
    to drive the orchestration end-to-end without touching the real
    document store, search pipeline, or query rewriter.
    """

    extract_window_fn: Callable[[str, date | None], tuple[date | None, date | None]] | None = None
    rewrite_query_fn: Callable[[str, date | None], str] | None = None
    query_chunks_fn: Callable[..., list[Any]] | None = None
    search_fn: Callable[..., Any] | None = None


def _format_window(start: date | None, end: date | None) -> dict[str, str]:
    if start is None and end is None:
        return {}
    return {
        "start": start.isoformat() if start else "",
        "end": end.isoformat() if end else "",
    }


def _chunk_to_hit(chunk: Any) -> TimelineHit:
    """Project a TemporalChunk into the uniform TimelineHit shape."""
    text = getattr(chunk, "text", "") or ""
    chunk_date = getattr(chunk, "date", None)
    metadata = getattr(chunk, "metadata", {}) or {}
    title = metadata.get("section_heading") or metadata.get("card_id") or metadata.get("title") or ""
    return TimelineHit(
        path=str(getattr(chunk, "source_path", "")),
        title=str(title),
        snippet=text[:300],
        score=float(metadata.get("score", 0.0)),
        date=chunk_date.isoformat() if chunk_date else "",
        chunk_type=str(getattr(chunk, "chunk_type", "")),
    )


def _search_to_hits(search_result: Any, limit: int) -> list[TimelineHit]:
    """Project a SearchResult's BudgetedResult list into TimelineHits."""
    out: list[TimelineHit] = []
    for budgeted in getattr(search_result, "results", [])[:limit]:
        inner = getattr(budgeted, "result", None)
        if inner is None:
            continue
        snippet = getattr(budgeted, "content", "") or getattr(inner, "snippet", "")
        out.append(
            TimelineHit(
                path=str(getattr(inner, "path", "")),
                title=str(getattr(inner, "title", "")),
                snippet=snippet[:300],
                score=float(getattr(inner, "boosted_score", getattr(inner, "score", 0.0))),
            )
        )
    return out


def run_timeline(
    query: str,
    *,
    anchor_date: date | None = None,
    agent: str | None = None,
    scope: Scope = Scope.SHARED_AGENT,
    since: date | None = None,
    until: date | None = None,
    chunk_types: list[str] | None = None,
    limit: int = 10,
    deps: TimelineDeps | None = None,
) -> TimelineResult:
    """Run the timeline use case and return a structured result.

    Never raises — failures populate ``TimelineResult.error`` and return
    an otherwise-empty result. Callers (CLI/MCP) surface the error verbatim.

    Args:
        query: User's natural-language query (may contain temporal
            expressions like "last week", "April 2026").
        anchor_date: Reference date for relative expressions. None →
            today, evaluated by the rewriter.
        agent: Agent name for collection scoping (search fallback only).
        scope: Multi-agent scope (search fallback only).
        since: Explicit lower bound; overrides query-extracted start.
        until: Explicit upper bound; overrides query-extracted end.
        chunk_types: Filter for the temporal-chunks backend
            (e.g. ``["board_card"]``); None → both types.
        limit: Maximum number of hits.
        deps: Injectable dependencies; production callers leave None.
    """
    d = deps or TimelineDeps()
    extract = d.extract_window_fn or _defaults.default_extract_window
    rewrite = d.rewrite_query_fn or _defaults.default_rewrite_query
    query_chunks = d.query_chunks_fn or _defaults.default_query_chunks
    search = d.search_fn or _defaults.default_search

    try:
        # 1. Resolve time window — explicit args win; otherwise extract from query.
        start: date | None = since
        end: date | None = until
        if start is None and end is None:
            try:
                start, end = extract(query, anchor_date)
            except Exception:
                logger.debug("extract_window failed", exc_info=True)
                start, end = None, None

        time_window = _format_window(start, end)
        is_temporal = bool(time_window)

        # 2. Rewrite the query when temporal — rewriter expands "last week"
        # into a date range so downstream search backends see the expansion.
        rewritten = query
        if is_temporal:
            try:
                rewritten = rewrite(query, anchor_date)
            except Exception:
                logger.debug("rewrite_query failed", exc_info=True)
                rewritten = query

        # 3. Primary backend: structured temporal-chunks index.
        chunk_hits: list[TimelineHit] = []
        if is_temporal:
            try:
                chunks = query_chunks(rewritten, start, end, chunk_types, limit)
                chunk_hits = [_chunk_to_hit(c) for c in chunks]
            except Exception:
                logger.warning("temporal-chunks query failed", exc_info=True)

        if chunk_hits:
            return TimelineResult(
                original_query=query,
                rewritten_query=rewritten,
                is_temporal=True,
                fell_back=False,
                time_window=time_window,
                results=chunk_hits,
            )

        # 4. Fall-through: search pipeline on rewritten query. We always
        # try this when temporal-chunks came back empty, so MCP callers
        # (and CLI users with no data in the temporal index) still get a
        # signal. fell_back=True signals "primary backend was empty".
        search_hits: list[TimelineHit] = []
        try:
            sr = search(rewritten, 3000, agent, scope)
            search_hits = _search_to_hits(sr, limit)
        except Exception:
            logger.warning("search fallback failed", exc_info=True)

        return TimelineResult(
            original_query=query,
            rewritten_query=rewritten,
            is_temporal=is_temporal,
            fell_back=True,
            time_window=time_window,
            results=search_hits,
        )
    except Exception as exc:
        logger.warning("run_timeline failed: %s", exc, exc_info=True)
        return TimelineResult(
            original_query=query,
            rewritten_query=query,
            is_temporal=False,
            fell_back=True,
            time_window={},
            results=[],
            error=f"{type(exc).__name__}: {exc}",
        )
