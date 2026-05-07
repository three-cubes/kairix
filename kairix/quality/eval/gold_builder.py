"""
TREC-style independent gold suite builder.

Pools candidates from multiple retrieval systems, deduplicates, and grades
each (query, document) pair with the LLM judge to produce system-independent
relevance judgments.

Usage::

    kairix eval build-gold \\
        --suite suites/v2-real-world.yaml \\
        --output suites/v2-independent-gold.yaml \\
        --systems bm25-equal,bm25-filepath,bm25-title,vector

Methodology: TREC pooling (Voorhees & Harman, 2005) adapted for LLM judges.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kairix.quality.eval.judge import (
    JudgeResult,
    calibrate,
    fetch_llm_credentials,
    judge_batch,
)

logger = logging.getLogger(__name__)

# BM25 weight presets for pooling — column order: filepath, title, doc
_WEIGHT_PRESETS: dict[str, tuple[float, float, float]] = {
    "bm25-equal": (1.0, 1.0, 1.0),
    "bm25-filepath": (10.0, 1.0, 1.0),
    "bm25-title": (1.0, 5.0, 1.0),
    "bm25-fp-title": (5.0, 3.0, 1.0),
}


def path_title(path: str) -> str:
    """Build a path-based gold title from a document path.

    Uses all path segments after the first (collection root) without the
    ``.md`` extension, so two different documents never produce the same
    title — even when filenames are generic (e.g. ``readme.md``).

    Example: "reference-library/engineering/adr-examples/readme.md"
           → "engineering/adr-examples/readme"

    For short paths (1-2 segments) the full path (minus extension) is
    returned as-is.
    """
    parts = Path(path).with_suffix("").parts
    # Drop the first segment (collection root) to keep the rest unique.
    # For short paths (<=2 segments) return everything.
    if len(parts) > 2:
        return "/".join(parts[1:])
    return "/".join(parts)


@dataclass
class PooledCandidate:
    """A document candidate from the retrieval pool."""

    path: str
    title: str
    snippet: str
    collection: str
    sources: list[str] = field(default_factory=list)  # which systems retrieved it
    grade: int = 0  # LLM judge grade (0/1/2)
    grade_votes: list[int] = field(default_factory=list)  # grades from multiple runs


@dataclass
class GoldBuildReport:
    """Summary of gold suite building."""

    queries_processed: int = 0
    total_candidates_pooled: int = 0
    total_judge_calls: int = 0
    avg_candidates_per_query: float = 0.0
    grade_distribution: dict[int, int] = field(default_factory=lambda: {0: 0, 1: 0, 2: 0})


def _bm25_search_with_weights(
    query: str,
    weights: tuple[float, float, float],
    collections: list[str] | None = None,
    limit: int = 10,
) -> list[dict[str, str]]:
    """
    Run BM25 search with specific column weights.

    Returns list of {path, title, snippet, collection} dicts.
    """
    from kairix.core.db import get_db_path, open_db
    from kairix.core.search.tokenizer import tokenize_fts_query

    # Build FTS5 query (bare style — gold builder pools with implicit AND)
    fts_query = tokenize_fts_query(query, style="bare")
    if not fts_query:
        return []

    try:
        db_path = get_db_path()
        db = open_db(Path(db_path))
    except Exception as e:
        logger.warning("gold_builder: cannot open DB — %s", e)
        return []

    # contextlib.closing guarantees the connection closes on every exit path,
    # including exceptions raised inside the result loop. Previously the
    # connection was leaked when fetchall() succeeded but row processing then
    # raised on a malformed row, since the only db.close() calls were on the
    # FTS-failure path and after the result loop completed normally (#143 Phase 0).
    from contextlib import closing

    with closing(db) as conn:
        conn.row_factory = sqlite3.Row
        w_fp, w_title, w_doc = weights
        # SQLite's bm25() function does not support bound parameters for the
        # weight arguments — they have to be interpolated. float() prevents
        # SQL string injection but doesn't guard nan or inf, both of which
        # are valid Python floats and produce undefined SQLite behaviour.
        # Reject them explicitly so a misconfigured weight tuple fails fast
        # with a clear message rather than silently corrupting BM25 scores.
        for label, w in (("filepath", w_fp), ("title", w_title), ("doc", w_doc)):
            if not math.isfinite(w) or w <= 0:
                raise ValueError(
                    f"gold_builder: BM25 weight {label}={w!r} must be finite and positive; "
                    f"weights tuple = (filepath, title, doc)"
                )
        try:
            if collections:
                placeholders = ",".join("?" * len(collections))
                # safe: float() cast on bm25 weights, no ? binding available for bm25 args
                sql = f"""
                    SELECT d.collection, d.path, d.title, c.doc,
                           bm25(documents_fts, {float(w_fp)}, {float(w_title)}, {float(w_doc)}) AS score
                    FROM documents_fts
                    JOIN documents d ON d.id = documents_fts.rowid
                    JOIN content c ON c.hash = d.hash
                    WHERE documents_fts MATCH ?
                      AND d.collection IN ({placeholders})
                      AND d.active = 1
                    ORDER BY score ASC
                    LIMIT ?
                """
                params: list[Any] = [fts_query, *collections, limit]
            else:
                # safe: float() cast on bm25 weights, no ? binding available for bm25 args
                sql = f"""
                    SELECT d.collection, d.path, d.title, c.doc,
                           bm25(documents_fts, {float(w_fp)}, {float(w_title)}, {float(w_doc)}) AS score
                    FROM documents_fts
                    JOIN documents d ON d.id = documents_fts.rowid
                    JOIN content c ON c.hash = d.hash
                    WHERE documents_fts MATCH ?
                      AND d.active = 1
                    ORDER BY score ASC
                    LIMIT ?
                """
                params = [fts_query, limit]

            rows = conn.execute(sql, params).fetchall()
        except Exception as e:
            logger.warning("gold_builder: FTS query failed — %s", e)
            return []

        results = []
        for row in rows:
            doc_text = row["doc"] or ""
            if doc_text.startswith("---"):
                parts = doc_text.split("---", 2)
                snippet = parts[2].strip()[:300] if len(parts) >= 3 else doc_text[:300]
            else:
                snippet = doc_text[:300]
            results.append(
                {
                    "path": str(row["path"]),
                    "title": str(row["title"] or ""),
                    "snippet": snippet,
                    "collection": str(row["collection"]),
                }
            )

        return results


def _vector_search(
    query: str,
    collections: list[str] | None = None,
    limit: int = 10,
) -> list[dict[str, str]]:
    """Run vector search via usearch. Returns list of {path, title, snippet, collection} dicts."""
    try:
        import numpy as np

        from kairix._azure import embed_text
        from kairix.core.search.hybrid import get_vector_index

        vec = embed_text(query)
        if not vec:
            return []

        query_vec = np.array(vec, dtype=np.float32)
        norm = np.linalg.norm(query_vec)
        if norm > 0:
            query_vec /= norm

        index = get_vector_index()
        if index is None:
            return []

        results = index.search(query_vec, k=limit, collections=collections)
        return [
            {
                "path": r["path"],
                "title": r["title"],
                "snippet": r["snippet"][:300],
                "collection": r["collection"],
            }
            for r in results
        ]
    except Exception as e:
        logger.warning("gold_builder: vector search failed — %s", e)
        return []


def pool_candidates(
    query: str,
    systems: list[str],
    collections: list[str] | None = None,
    limit_per_system: int = 10,
    search_fns: dict[str, Callable[..., list[dict[str, str]]]] | None = None,
) -> list[PooledCandidate]:
    """
    Pool top-k results from multiple retrieval systems for a single query.

    Deduplicates by path. Records which systems retrieved each document.

    Args:
        search_fns: Optional mapping of system name to search callable.
                    Each callable receives (query, collections, limit) and
                    returns list of {path, title, snippet, collection} dicts.
                    Production defaults to internal BM25/vector functions.
    """
    _fns = search_fns or {}
    candidates: dict[str, PooledCandidate] = {}

    for system in systems:
        if system in _fns:
            results = _fns[system](query, collections, limit_per_system)
        elif system == "vector":
            results = _vector_search(query, collections, limit_per_system)
        elif system in _WEIGHT_PRESETS:
            results = _bm25_search_with_weights(query, _WEIGHT_PRESETS[system], collections, limit_per_system)
        else:
            logger.warning("gold_builder: unknown system %r — skipping", system)
            continue

        for r in results:
            path = r["path"]
            if path not in candidates:
                candidates[path] = PooledCandidate(
                    path=path,
                    title=r["title"],
                    snippet=r["snippet"],
                    collection=r["collection"],
                )
            candidates[path].sources.append(system)

    return list(candidates.values())


def grade_candidates(
    query: str,
    candidates: list[PooledCandidate],
    api_key: str,
    endpoint: str,
    deployment: str = "gpt-4o-mini",
    judge_runs: int = 2,
    judge_fn: Callable[..., JudgeResult] | None = None,
) -> list[PooledCandidate]:
    """
    Grade each candidate using the LLM judge.

    Runs judge_runs times and uses majority vote for final grade.
    """
    if not candidates:
        return []

    # Build (doc_key, snippet) pairs for judge — use path_title() for
    # unique keys so two files with the same stem (e.g. readme.md) get
    # independent grades.
    judge_candidates = []
    for c in candidates:
        doc_key = path_title(c.path)
        judge_candidates.append((doc_key, c.snippet[:150]))

    _judge = judge_fn or judge_batch

    graded_candidates: list[PooledCandidate] = list(candidates)

    for _run in range(judge_runs):
        result: JudgeResult = _judge(
            query=query,
            candidates=judge_candidates,
            api_key=api_key,
            endpoint=endpoint,
            deployment=deployment,
            shuffle=True,
        )

        for c in graded_candidates:
            doc_key = path_title(c.path)
            grade = result.grades.get(doc_key, 0)
            c.grade_votes.append(grade)

    # Majority vote
    for c in graded_candidates:
        if c.grade_votes:
            c.grade = max(set(c.grade_votes), key=c.grade_votes.count)

    return graded_candidates


def build_independent_gold(
    suite_path: Path,
    output_path: Path,
    systems: list[str] | None = None,
    judge_runs: int = 2,
    calibrate_first: bool = True,
    limit_per_system: int = 10,
    credentials: tuple[str, str, str] | None = None,
    search_fns: dict[str, Callable[..., list[dict[str, str]]]] | None = None,
    calibrate_fn: Callable[[str, str, str], bool] | None = None,
    grade_fn: Callable[..., list[PooledCandidate]] | None = None,
) -> GoldBuildReport:
    """
    Build an independent gold suite using TREC-style pooling + LLM judge.

    1. Load queries from existing suite
    2. For each query, pool candidates from multiple retrieval systems
    3. Grade each candidate with LLM judge (majority vote)
    4. Output enriched suite with system-independent gold_titles

    Args:
        suite_path:        Path to input suite YAML (queries + categories).
        output_path:       Path to write enriched suite YAML.
        systems:           List of retrieval system names to pool from.
        judge_runs:        Number of judge runs per query (default: 2).
        calibrate_first:   Run calibration anchors before judging (default: True).
        limit_per_system:  Top-k results per system per query (default: 10).

    Returns:
        GoldBuildReport with statistics.
    """
    if systems is None:
        systems = ["bm25-equal", "bm25-filepath", "bm25-title", "vector"]

    # Load suite
    with open(suite_path) as f:
        suite_data = yaml.safe_load(f)

    cases = suite_data.get("cases", [])
    if not cases:
        logger.error("gold_builder: no cases found in suite %s", suite_path)
        return GoldBuildReport()

    # Fetch credentials
    if credentials is not None:
        api_key, endpoint, deployment = credentials
    else:
        api_key, endpoint, deployment = fetch_llm_credentials()
    if not api_key or not endpoint:
        logger.error("gold_builder: no API credentials — cannot run judge")
        return GoldBuildReport()

    # Calibrate judge
    _calibrate = calibrate_fn or calibrate
    if calibrate_first:
        logger.info("gold_builder: running calibration...")
        _calibrate(api_key, endpoint, deployment)
        logger.info("gold_builder: calibration passed")

    report = GoldBuildReport()

    for i, case in enumerate(cases):
        query = case.get("query", "")
        if not query:
            continue

        logger.info("gold_builder: [%d/%d] %s", i + 1, len(cases), query[:60])

        # Pool candidates
        candidates = pool_candidates(query, systems, limit_per_system=limit_per_system, search_fns=search_fns)
        report.total_candidates_pooled += len(candidates)

        if not candidates:
            logger.warning("gold_builder: no candidates for query %r", query[:60])
            continue

        # Grade with LLM judge
        _grade = grade_fn or grade_candidates
        candidates = _grade(query, candidates, api_key, endpoint, deployment, judge_runs)
        report.total_judge_calls += len(candidates) * judge_runs

        # Build gold_titles from graded candidates (grade >= 1)
        gold_titles = []
        for c in sorted(candidates, key=lambda x: x.grade, reverse=True):
            report.grade_distribution[c.grade] = report.grade_distribution.get(c.grade, 0) + 1
            if c.grade >= 1:
                gold_titles.append(
                    {
                        "title": path_title(c.path),
                        "relevance": c.grade,
                    }
                )

        # Update case with independent gold
        case["gold_titles"] = gold_titles
        case["score_method"] = "ndcg"
        # Preserve original gold_paths for comparison but mark as legacy
        if "gold_paths" in case:
            case["legacy_gold_paths"] = case.pop("gold_paths")

        report.queries_processed += 1

    # Update metadata
    suite_data.setdefault("meta", {})["gold_method"] = "trec-pooling-llm-judge"
    suite_data["meta"]["gold_systems"] = systems
    suite_data["meta"]["judge_runs"] = judge_runs
    suite_data["meta"]["n_cases"] = report.queries_processed

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        yaml.dump(suite_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    report.avg_candidates_per_query = (
        report.total_candidates_pooled / report.queries_processed if report.queries_processed > 0 else 0
    )

    logger.info("gold_builder: %s", report)
    return report
