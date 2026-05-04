"""Hybrid search orchestrator for the kairix pipeline.

Orchestrates the full search pipeline:

  1. Classify query intent
  2. Dispatch BM25 and vector search in parallel (ThreadPoolExecutor)
  3. Fuse results with RRF
  4. Apply entity boosting via Neo4j graph
  5. Apply token budget
  6. Log retrieval event

ENTITY intent requires an available Neo4j connection. When Neo4j is
unavailable and the classified intent is ENTITY, search() returns a
SearchResult with a non-empty error field and no results. The caller
should check sr.error before using sr.results.

Falls back to BM25-only if vector search fails (non-ENTITY intents).
Logs every search event to the path set by KAIRIX_SEARCH_LOG (default: /data/kairix/logs/search.jsonl).

Never raises — returns SearchResult with empty results on any failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kairix.core.db import get_db_path, open_db
from kairix.core.search.bm25 import BM25Result
from kairix.core.search.budget import apply_budget
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.intent import QueryIntent
from kairix.core.search.rrf import (
    FusedResult,
    chunk_date_boost,
    entity_boost_neo4j,
    procedural_boost,
    temporal_date_boost,
)
from kairix.core.search.scope import Scope
from kairix.core.search.vec_index import VECTOR_DEFAULT_K, VecResult

logger = logging.getLogger(__name__)


def _get_neo4j():  # type: ignore[return]
    """Lazy import for Neo4j client to avoid hard dependency at module load."""
    from kairix.knowledge.graph.client import get_client

    return get_client()


def _get_llm():  # type: ignore[return]
    """Lazy import for LLM backend to avoid hard dependency at module load."""
    from kairix.platform.llm import get_default_backend

    return get_default_backend()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEARCH_LOG_PATH = Path(
    os.environ.get(
        "KAIRIX_SEARCH_LOG",
        str(Path.home() / ".cache" / "kairix" / "logs" / "search.jsonl"),
    )
)

# Query logging (privacy-sensitive — disabled by default)
_LOG_QUERIES: bool = os.getenv("KAIRIX_LOG_QUERIES", "0") == "1"
_QUERY_LOG_PATH: Path = Path(
    os.getenv(
        "KAIRIX_QUERY_LOG",
        str(Path.home() / ".cache" / "kairix" / "logs" / "queries.jsonl"),
    )
)

# Rotate when file exceeds this size
_QUERY_LOG_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB

# Collections — loaded from kairix.config.yaml if configured, otherwise use defaults.
# Override with KAIRIX_EXTRA_COLLECTIONS env var (comma-separated) for quick additions.
_COLLECTIONS_CONFIG = None  # loaded lazily from config


def _get_shared_collections() -> list[str]:
    """Get shared collection names from config or defaults."""
    global _COLLECTIONS_CONFIG
    if _COLLECTIONS_CONFIG is None:
        try:
            from kairix.core.search.config_loader import load_collections

            _COLLECTIONS_CONFIG = load_collections()
        except (ImportError, OSError, ValueError):
            _COLLECTIONS_CONFIG = False  # mark as "tried and failed"

    if _COLLECTIONS_CONFIG:
        names = [c.name for c in _COLLECTIONS_CONFIG.shared]
    else:
        # Fallback: search all documents (no collection scoping)
        names = []

    # Add extra collections from env var
    extra = os.environ.get("KAIRIX_EXTRA_COLLECTIONS", "")
    names.extend(c.strip() for c in extra.split(",") if c.strip())
    return names


def _get_agent_pattern() -> str:
    """Get the per-agent collection name template."""
    if _COLLECTIONS_CONFIG:
        return _COLLECTIONS_CONFIG.agent_pattern
    return "{agent}-memory"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


# Backwards-compat alias — canonical definition is in pipeline.py.
# Deprecated: import from kairix.core.search.pipeline instead.
# Will be removed no sooner than 2 releases after 2026.04.
from kairix.core.search.pipeline import SearchResult as SearchResult  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collections_for(agent: str | None, scope: Scope) -> list[str] | None:
    """Build collection list from config, agent name, and scope.

    If no collections are configured, returns an empty list (search all documents).
    """
    cols: list[str] = list(_get_shared_collections())
    if agent and "agent" in scope:
        pattern = _get_agent_pattern()
        cols.append(pattern.format(agent=agent))
    return cols or None  # None = no collection filter (search everything)


def _log_search_event(event: dict) -> None:
    """Append a search event to the JSONL log. Creates directory if needed."""
    try:
        SEARCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SEARCH_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as e:
        logger.warning("hybrid: failed to write search log — %s", e)


def _rotate_query_log(path: Path) -> None:
    """Rotate path → path.1, removing any older rotated file. Simple size-based rotation."""
    rotated = Path(str(path) + ".1")
    if rotated.exists():
        rotated.unlink()
    shutil.move(str(path), str(rotated))


def _log_query_event(event: dict) -> None:
    """Append a query event to the query JSONL log. Rotates at 10 MB. No-op if logging disabled."""
    if not _LOG_QUERIES:
        return
    try:
        _QUERY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _QUERY_LOG_PATH.exists() and _QUERY_LOG_PATH.stat().st_size >= _QUERY_LOG_MAX_BYTES:
            _rotate_query_log(_QUERY_LOG_PATH)
        with _QUERY_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as e:
        logger.warning("hybrid: failed to write query log — %s", e)


def _query_has_temporal_marker(query: str) -> bool:
    """
    Return True if the query contains an explicit temporal reference.

    Used to guard temporal chunk injection — prevents memory files being injected
    for historical/architectural queries ("what changed and why") that trigger
    TEMPORAL intent but whose gold documents are design docs, not daily logs.

    Explicit markers: ISO dates (YYYY-MM-DD / YYYY-MM) or relative terms
    (yesterday, today, last week, last month, recently, this week, this month).
    """
    import re as _re

    iso_date = _re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b")
    rel_term = _re.compile(
        r"\b(recent(?:ly)?|last\s+(?:week|month)|yesterday|today|this\s+(?:week|month))\b",
        _re.IGNORECASE,
    )
    return bool(iso_date.search(query) or rel_term.search(query))


def _enrich_chunk_dates(
    fused: list[FusedResult],
    db_path: Path,
    doc_repo: object | None = None,
) -> None:
    """
    Populate FusedResult.chunk_date from the kairix SQLite DB for TMP-7B.

    Does a single SQL query joining content_vectors to documents on hash,
    then sets r.chunk_date for any results whose path has a non-null chunk_date.

    When doc_repo (DocumentRepository) is provided, delegates to
    doc_repo.get_chunk_dates() instead of direct SQL.

    Safe when DB is unavailable — logs a warning and returns silently.
    Never raises.
    """
    if not fused:
        return

    paths = [r.path for r in fused if r.path]
    if not paths:
        return

    # Delegate to DocumentRepository when provided
    if doc_repo is not None:
        try:
            path_to_date = doc_repo.get_chunk_dates(paths)  # type: ignore[union-attr]
            for r in fused:
                cd = path_to_date.get(r.path)
                if cd:
                    r.chunk_date = cd
            return
        except Exception as e:
            logger.warning("hybrid: _enrich_chunk_dates doc_repo failed — %s", e)
            return

    try:
        db = open_db(Path(db_path))
        try:
            # Use LIKE suffix match because the DB stores absolute paths while FusedResult
            # paths may be collection-relative (e.g. "concept/builder.md" vs
            # "/data/documents/concept/builder.md"). An exact IN() match would miss all rows.
            # safe: LIKE clauses use ? binding for values
            like_clauses = " OR ".join("d.path LIKE ?" for _ in paths)
            rows = db.execute(
                f"SELECT d.path, cv.chunk_date "
                f"FROM content_vectors cv "
                f"JOIN documents d ON d.hash = cv.hash "
                f"WHERE cv.chunk_date IS NOT NULL AND ({like_clauses})",
                [f"%{p}" for p in paths],
            ).fetchall()
        finally:
            db.close()
    except (sqlite3.Error, OSError) as e:
        logger.warning("hybrid: _enrich_chunk_dates DB query failed — %s", e)
        return

    if not rows:
        logger.warning(
            "hybrid: _enrich_chunk_dates found 0 rows for %d paths — "
            "chunk_date column may not be populated. "
            "Re-run `kairix embed` to populate chunk_date (ERR-001).",
            len(paths),
        )
        return

    # Build path → chunk_date map (last non-null value wins for multi-chunk docs)
    path_to_date: dict[str, str] = {}
    for path, chunk_date in rows:
        path_to_date[path] = chunk_date

    for r in fused:
        cd = path_to_date.get(r.path)
        if cd:
            r.chunk_date = cd


# ---------------------------------------------------------------------------
# Multi-hop helper (extracted from search() — SOLID-1)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Extracted pipeline stages (I7 — independently testable)
# ---------------------------------------------------------------------------


@dataclass
class _SearchPipelineState:
    """Groups pipeline state passed between search stages (reduces parameter lists)."""

    fused: list[FusedResult]
    query: str
    intent: QueryIntent
    budget: int
    t_start: float
    bm25_count: int
    vec_count: int
    collections: list[str]
    vec_failed: bool
    fallback_used: bool
    temporal_chunks: list | None = None
    agent: str | None = None
    scope: Scope = Scope.SHARED_AGENT


def _preprocess_temporal(
    query: str,
    intent: QueryIntent,
) -> tuple[str, list, frozenset[str] | None]:
    """Rewrite a TEMPORAL query and extract temporal chunks and date-filter paths.

    Returns (active_query, temporal_chunks, date_filter_paths).
    For non-TEMPORAL intents, returns (query, [], None) unchanged.
    Never raises.
    """
    if intent != QueryIntent.TEMPORAL:
        return query, [], None

    active_query = query
    temporal_chunks: list = []
    date_filter_paths: frozenset[str] | None = None

    try:
        from datetime import date as _date

        from kairix.core.temporal.index import query_temporal_chunks
        from kairix.core.temporal.rewriter import (
            extract_time_window,
            rewrite_temporal_query,
        )

        active_query = rewrite_temporal_query(query, reference_date=_date.today())
        start, end = extract_time_window(query, reference_date=_date.today())
        temporal_chunks = query_temporal_chunks(topic=active_query, start=start, end=end, limit=10)

        # TMP-2: build date-filtered path set to restrict BM25 and vector results.
        # Only for RELATIVE temporal expressions (last week, recently, yesterday).
        # NOT for absolute date references (March 2026, 2026-03-09) — those queries
        # are ABOUT a time period, not filtered by document creation date.
        if start is not None:
            from kairix.core.embed.schema import get_date_filtered_paths
            from kairix.core.temporal.rewriter import is_relative_temporal

            if is_relative_temporal(query):
                _tmp2_db = open_db(Path(get_db_path()))
                try:
                    _paths = get_date_filtered_paths(_tmp2_db, start, end)
                    if _paths:  # empty = no dated chunks yet; do not filter
                        date_filter_paths = _paths
                        logger.debug(
                            "hybrid: TMP-2 date filter active — %d paths in [%s, %s]",
                            len(_paths),
                            start,
                            end,
                        )
                except Exception as _dfp_e:
                    logger.warning("hybrid: TMP-2 get_date_filtered_paths failed — %s", _dfp_e)
                finally:
                    _tmp2_db.close()
    except Exception as _e:
        logger.warning("hybrid: temporal rewriting failed — %s", _e)
        active_query = query

    return active_query, temporal_chunks, date_filter_paths


def _apply_entity_boost(
    fused: list[FusedResult],
    neo4j_client: object,
    cfg: RetrievalConfig,
) -> list[FusedResult]:
    """Apply entity boosting via Neo4j graph in-degree.

    Boosts entity canonical notes so they rank higher in results.
    Returns the fused list unchanged on any failure.
    """
    try:
        fused = entity_boost_neo4j(fused, neo4j_client, config=cfg.entity)
    except Exception as _eb_e:  # broad catch justified: Neo4j driver can raise arbitrary exceptions
        logger.warning("hybrid: entity_boost_neo4j failed — %s", _eb_e)
    return fused


def _check_entity_prerequisites(
    intent: QueryIntent,
    neo4j_client: object,
    query: str,
) -> SearchResult | None:
    """Return a SearchResult error if ENTITY intent lacks Neo4j, else None.

    ENTITY intent requires Neo4j. Do not silently degrade to BM25+vector
    for entity queries — the results would be misleading (no entity graph
    expansion, no alias resolution, no mention-based boosting).
    """
    if intent != QueryIntent.ENTITY or neo4j_client.available:  # type: ignore[union-attr]
        return None
    err = (
        "Entity queries require Neo4j but the graph is unavailable. "
        "Check KAIRIX_NEO4J_URI, KAIRIX_NEO4J_USER, KAIRIX_NEO4J_PASSWORD "
        "and run `kairix onboard check` for diagnostics. "
        "Install Neo4j with `bash scripts/install-neo4j.sh` if not yet set up."
    )
    logger.error("hybrid: ENTITY query rejected — Neo4j unavailable (query=%r)", query[:60])
    return SearchResult(query=query, intent=intent, error=err)


def _dispatch_parallel_search(
    active_query: str,
    collections: list[str],
    cfg: RetrievalConfig,
    date_filter_paths: frozenset[str] | None,
    intent: QueryIntent,
    query: str,
) -> tuple[list[BM25Result], list[VecResult], bool]:
    """Dispatch BM25 and vector search in parallel via ThreadPoolExecutor.

    Returns (bm25_results, vec_results, vec_failed).
    """
    from kairix.core.search.bm25 import bm25_search as _bm25_search_fn

    bm25_results: list[BM25Result] = []
    vec_results: list[VecResult] = []
    vec_failed = False

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures: dict[str, Future] = {}

        futures["bm25"] = executor.submit(
            _bm25_search_fn,
            active_query,
            collections,
            cfg.bm25_limit,
            None,
            date_filter_paths,
        )
        if not cfg.skip_vector:
            futures["vec"] = executor.submit(
                _run_vector_search,
                active_query,
                collections,
                date_filter_paths,
                cfg.vec_limit,
                intent.value,
            )

        for name, future in futures.items():
            try:
                result = future.result(timeout=30)
                if name == "bm25":
                    bm25_results = result or []
                elif name == "vec":
                    vec_results = result or []
            except Exception as e:
                logger.warning("hybrid: %s search future failed — %s", name, e)
                if name == "vec":
                    vec_failed = True

    if cfg.skip_vector:
        vec_failed = False  # not a failure — intentionally skipped
    elif not vec_results:
        vec_failed = True
        logger.info(
            "hybrid: vector search returned no results, using BM25 only (query=%r)",
            query[:60],
        )

    return bm25_results, vec_results, vec_failed


def _apply_intent_boosts(
    fused: list[FusedResult],
    intent: QueryIntent,
    active_query: str,
    neo4j_client: object,
    cfg: RetrievalConfig,
) -> list[FusedResult]:
    """Apply all post-fusion intent-specific boosts.

    Applies entity boost, procedural boost, temporal date-path boost,
    and chunk-date proximity boost as appropriate for the query intent.
    Returns the boosted fused list.
    """
    # Entity boost via Neo4j — boosts entity canonical notes by graph in-degree
    fused = _apply_entity_boost(fused, neo4j_client, cfg)

    # Procedural boosting for PROCEDURAL intent
    if intent == QueryIntent.PROCEDURAL:
        fused = procedural_boost(fused, config=cfg.procedural)

    # Temporal date-path boost for TEMPORAL intent (disabled by default via config —
    # enable for date-named file corpora via RetrievalConfig.for_daily_log_corpus())
    if intent == QueryIntent.TEMPORAL:
        fused = temporal_date_boost(fused, active_query, config=cfg.temporal)

    # Chunk-date proximity boost for TEMPORAL intent (TMP-7B)
    # Guard: skip when config requires explicit temporal marker and query lacks one.
    # Prevents recency bias on generic TEMPORAL queries ("what changed and why").
    _chunk_date_guard_ok = not cfg.temporal.chunk_date_boost_guard_explicit_only or _query_has_temporal_marker(
        active_query
    )
    if intent == QueryIntent.TEMPORAL and cfg.temporal.chunk_date_boost_enabled and _chunk_date_guard_ok:
        fused = _apply_chunk_date_boost(fused, active_query, cfg)

    return fused


def _apply_chunk_date_boost(
    fused: list[FusedResult],
    active_query: str,
    cfg: RetrievalConfig,
) -> list[FusedResult]:
    """Apply chunk-date proximity boost for TEMPORAL intent.

    Extracts the time window from the query and boosts results whose
    chunk_date is close to the window midpoint. Returns the fused list
    unchanged on any failure.
    """
    import datetime as _dt

    from kairix.core.temporal.rewriter import extract_time_window

    try:
        _start, _end = extract_time_window(active_query, reference_date=_dt.date.today())
        # Use window midpoint rather than start — avoids systematic bias toward
        # documents dated at the beginning of a multi-day query window.
        if _start and _end:
            _query_date = _start + _dt.timedelta(days=(_end - _start).days // 2)
        else:
            _query_date = _start or _dt.date.today()
        fused = chunk_date_boost(fused, _query_date, config=cfg.temporal)
    except Exception as _cbd_e:
        logger.warning("hybrid: chunk_date_boost failed — %s", _cbd_e)
    return fused


def _inject_temporal_chunks(
    fused: list[FusedResult],
    temporal_chunks: list,
    intent: QueryIntent,
    active_query: str,
) -> list[FusedResult]:
    """Merge temporal chunks into fused results for TEMPORAL intent.

    Only injects when query has an explicit temporal reference (specific date or
    relative term like "last week"). Queries like "what changed and why" trigger
    TEMPORAL intent but target architecture/decisions docs — not memory files.
    Injecting memory files at rrf_score=0.82 for those queries displaces gold docs.

    Returns updated fused list.
    """
    if not temporal_chunks or intent != QueryIntent.TEMPORAL or not _query_has_temporal_marker(active_query):
        return fused

    seen_paths = {fr.path for fr in fused}
    temporal_fused = []
    for tc in temporal_chunks[:6]:
        if tc.source_path not in seen_paths:
            heading = tc.metadata.get("section_heading") or tc.metadata.get("status") or ""
            title = (heading + " — " + tc.source_path.split("/")[-1]) if heading else tc.source_path.split("/")[-1]
            temporal_fused.append(
                FusedResult(
                    path=tc.source_path,
                    collection="temporal",
                    title=title,
                    snippet=tc.text[:500].strip(),
                    rrf_score=0.82,
                    boosted_score=0.82,
                )
            )
            seen_paths.add(tc.source_path)
    if temporal_fused:
        fused = temporal_fused + fused
        logger.debug("hybrid: merged %d temporal chunks into results", len(temporal_fused))

    return fused


def _apply_reranking(
    fused: list[FusedResult],
    active_query: str,
    intent: QueryIntent,
    cfg: RetrievalConfig,
) -> list[FusedResult]:
    """Apply cross-encoder re-ranking when eligible.

    Always on for MULTI_HOP and SEMANTIC intents, optional for others
    via config.rerank.enabled. Returns the fused list unchanged on failure.
    """
    should_rerank = (cfg.rerank.enabled or intent.value in cfg.rerank_intents) and fused
    if not should_rerank:
        return fused
    try:
        from kairix.core.search.rerank import rerank as _rerank

        fused = _rerank(
            query=active_query,
            results=fused,
            model=cfg.rerank.model,
            candidate_limit=cfg.rerank.candidate_limit,
        )
    except Exception as _rr_e:
        logger.warning("hybrid: rerank failed — %s — using unmodified order", _rr_e)
    return fused


def _build_search_result(state: _SearchPipelineState) -> SearchResult:
    """Apply token budget, compute diagnostics, build SearchResult, and log.

    This is the final assembly stage of the search pipeline — everything
    after boosting and re-ranking is done here.
    """
    budgeted = apply_budget(state.fused, budget=state.budget)

    total_tokens = sum(r.token_estimate for r in budgeted)
    tiers_used = sorted({r.tier for r in budgeted})

    t_end = time.monotonic()
    latency_ms = (t_end - state.t_start) * 1000.0

    result = SearchResult(
        query=state.query,
        intent=state.intent,
        results=budgeted,
        bm25_count=state.bm25_count,
        vec_count=state.vec_count,
        fused_count=len(state.fused),
        collections=state.collections,
        tiers_used=tiers_used,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        vec_failed=state.vec_failed,
        fallback_used=state.fallback_used,
    )

    # Attach temporal chunks for TEMPORAL intent (accessible to callers)
    if state.temporal_chunks:
        result.error = ""  # ensure no error state masks chunks
        result._temporal_chunks = state.temporal_chunks

    # Log event
    _log_fn = _log_search_event
    _log_q_fn = _log_query_event
    query_hash = hashlib.sha256(state.query.encode()).hexdigest()[:12]
    _log_fn(
        {
            "query_hash": query_hash,
            "intent": state.intent.value,
            "agent": state.agent,
            "scope": state.scope,
            "bm25_count": state.bm25_count,
            "vec_count": state.vec_count,
            "fused_count": len(state.fused),
            "collections": state.collections,
            "tiers_used": tiers_used,
            "total_tokens": total_tokens,
            "latency_ms": round(latency_ms, 1),
            "vec_failed": state.vec_failed,
            "fallback_used": state.fallback_used,
            "ts": int(time.time()),
        }
    )

    # Optional raw query log (privacy-sensitive — controlled by KAIRIX_LOG_QUERIES)
    _log_q_fn(
        {
            "ts": int(time.time()),
            "query": state.query,
            "query_hash": query_hash,
            "intent": state.intent.value,
            "agent": state.agent,
            "fused_count": len(state.fused),
            "vec_failed": state.vec_failed,
            "latency_ms": round(latency_ms, 1),
            "top_paths": [r.result.path for r in budgeted[:3]],
        }
    )

    return result


def _apply_hyde(
    query: str,
    query_vec: Any,
    embed: Any = None,
    chat: Any = None,
) -> Any:
    """Apply HyDE (Hypothetical Document Embeddings) to a query vector.

    Generates a short hypothetical answer via LLM, embeds it, and blends
    with the query embedding (50/50) for better recall on conceptual queries.

    Returns the (possibly blended) query vector. Never raises.
    """
    import numpy as np

    try:
        if embed is None or chat is None:
            from kairix._azure import chat_completion, embed_text

            embed = embed or embed_text
            chat = chat or chat_completion

        hyde_answer = chat(
            [
                {
                    "role": "user",
                    "content": f"Write a short paragraph that answers: {query}",
                }
            ],
            max_tokens=150,
        )
        if not hyde_answer:
            return query_vec

        hyde_vec = embed(hyde_answer)
        if not hyde_vec:
            return query_vec

        hyde_arr = np.array(hyde_vec, dtype=np.float32)
        hyde_norm = np.linalg.norm(hyde_arr)
        if hyde_norm <= 0:
            return query_vec

        hyde_arr /= hyde_norm
        blended = (query_vec + hyde_arr) / 2.0
        norm = np.linalg.norm(blended)
        if norm > 0:
            blended /= norm
        return blended
    except Exception as _hyde_e:
        logger.debug("hybrid: HyDE generation failed — %s — using raw query embedding", _hyde_e)
        return query_vec


def _run_vector_search(
    query: str,
    collections: list[str],
    date_filter_paths: frozenset[str] | None = None,
    k: int = VECTOR_DEFAULT_K,
    intent: str | None = None,
) -> list[VecResult]:
    """
    Embed query and run ANN vector search via usearch. Returns [] on any failure.
    Called from ThreadPoolExecutor — must not raise.

    Retries embed once on empty result to handle cold Key Vault fetches.

    When intent is "semantic" or "multi_hop", applies HyDE (Hypothetical Document
    Embeddings): generates a short hypothetical answer via LLM, embeds it, and
    blends with the query embedding for better recall on conceptual queries.
    """
    import time as _time

    import numpy as np

    try:
        from kairix._azure import chat_completion as _chat
        from kairix._azure import embed_text as _embed

        _get_index = get_vector_index

        vec = _embed(query)
        if not vec:
            logger.warning("hybrid: embed returned empty on first try — retrying once")
            _time.sleep(0.5)
            vec = _embed(query)
        if not vec:
            logger.warning("hybrid: embed returned empty after retry — skipping vector search")
            return []

        query_vec = np.array(vec, dtype=np.float32)
        norm = np.linalg.norm(query_vec)
        if norm > 0:
            query_vec /= norm

        if intent in ("semantic", "multi_hop"):
            query_vec = _apply_hyde(query, query_vec, embed=_embed, chat=_chat)

        index = _get_index()
        if index is None or len(index) == 0:
            return []

        results = index.search(query_vec, k=k, collections=collections or None)

        if date_filter_paths and results:
            results = [r for r in results if r["path"] in date_filter_paths]

        return results
    except Exception as e:
        logger.warning("hybrid: _run_vector_search failed — %s", e)
        return []


# Module-level usearch index singleton
_VECTOR_INDEX: Any = None


def get_vector_index() -> Any:
    """Lazily load the usearch vector index singleton."""
    global _VECTOR_INDEX
    if _VECTOR_INDEX is not None:
        return _VECTOR_INDEX
    try:
        from kairix.core.search.vec_index import VectorIndex
        from kairix.paths import db_path as get_db_path

        db_p = get_db_path()
        index_path = db_p.parent / "vectors.usearch"
        meta_path = db_p.parent / "vectors.meta.json"
        idx = VectorIndex(index_path=index_path, meta_path=meta_path, db_path=db_p)
        count = idx.load()
        if count > 0:
            logger.info("hybrid: loaded usearch index (%d vectors)", count)
            _VECTOR_INDEX = idx
            return idx
        logger.warning("hybrid: usearch index empty or missing at %s", index_path)
        return None
    except Exception as e:
        logger.warning("hybrid: failed to load usearch index — %s", e)
        return None
