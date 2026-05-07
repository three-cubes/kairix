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

#143 Phase 2b — ``GoldBuilder`` class with ``LLMJudge`` + ``Retriever``
constructor injection. Module-level functions are kept as deprecated
wrappers for backwards compatibility; Phase 4 removes the ``*_fn=`` kwargs.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from kairix.quality.eval.judge import (
    JUDGE_DEPLOYMENT,
    JudgeResult,
    calibrate,
    fetch_llm_credentials,
)

if TYPE_CHECKING:
    from kairix.core.protocols import LLMJudge as LLMJudgeProtocol
    from kairix.core.protocols import Retriever as RetrieverProtocol

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


def _validate_weights(weights: tuple[float, float, float]) -> None:
    """Raise ValueError if any BM25 weight is non-finite or non-positive.

    SQLite's bm25() does not accept bound parameters for weight args, so the
    values get interpolated. ``float()`` prevents SQL injection but does not
    guard ``nan`` / ``inf`` (both are valid Python floats and produce
    undefined SQLite behaviour). Reject explicitly to fail fast — see #143
    Phase 0b.
    """
    w_fp, w_title, w_doc = weights
    for label, w in (("filepath", w_fp), ("title", w_title), ("doc", w_doc)):
        if not math.isfinite(w) or w <= 0:
            raise ValueError(
                f"gold_builder: BM25 weight {label}={w!r} must be finite and positive; "
                f"weights tuple = (filepath, title, doc)"
            )


def _vector_search(
    query: str,
    collections: list[str] | None = None,
    limit: int = 10,
) -> list[dict[str, str]]:
    """Run vector search via usearch. Returns list of {path, title, snippet, collection} dicts.

    Module-level helper retained for the deprecated ``pool_candidates``
    wrapper — Phase 4 removes this once all callers route through
    ``GoldBuilder._retriever``.
    """
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


# ---------------------------------------------------------------------------
# GoldBuilder — class with LLMJudge + Retriever constructor injection
# (#143 Phase 2b)
#
# Wraps the free functions ``pool_candidates``, ``grade_candidates``,
# ``build_independent_gold`` in a class that takes ``LLMJudge`` and
# ``Retriever`` via the constructor. Tests construct
# ``GoldBuilder(llm_judge=FakeLLMJudge(...), retriever=FakeRetriever(...))``
# rather than substituting via the legacy ``*_fn=`` kwargs.
#
# The free functions are preserved for backwards compatibility — they
# instantiate a ``GoldBuilder`` with default deps and delegate. The
# ``*_fn=None`` kwargs stay on those wrappers; Phase 4 removes them once
# all callers route through the class.
# ---------------------------------------------------------------------------


class GoldBuilder:
    """LLMJudge + Retriever-injected gold suite builder.

    Constructor takes:
      - ``llm_judge``: ``LLMJudge`` protocol implementation (production:
        ``kairix.quality.eval.judge.LLMJudge`` wrapping ``AzureChatBackend``;
        tests: ``FakeLLMJudge``).
      - ``retriever``: ``Retriever`` protocol implementation. Used for the
        ``vector`` system path; BM25 weighted variants stay on the private
        ``_bm25_search_with_weights`` method (raw SQL — Phase 5 follow-up
        lifts this onto ``DocumentRepository``).

    Both are optional; when omitted, production defaults are constructed
    lazily on first use (``LLMJudge(chat_backend=AzureChatBackend())`` and
    a ``_DefaultGoldRetriever`` shim).
    """

    def __init__(
        self,
        *,
        llm_judge: LLMJudgeProtocol | None = None,
        retriever: RetrieverProtocol | None = None,
    ) -> None:
        self._llm_judge = llm_judge
        self._retriever = retriever

    # ------------------------------------------------------------------
    # Internal default-dependency construction (lazy — production only)
    # ------------------------------------------------------------------

    def _get_llm_judge(self) -> LLMJudgeProtocol:
        """Return the configured LLMJudge or construct a production default."""
        if self._llm_judge is None:
            from kairix._azure import AzureChatBackend
            from kairix.quality.eval.judge import LLMJudge as ProductionLLMJudge

            self._llm_judge = ProductionLLMJudge(chat_backend=AzureChatBackend())
        return self._llm_judge

    def _get_retriever(self) -> RetrieverProtocol:
        """Return the configured Retriever or construct a production default."""
        if self._retriever is None:
            self._retriever = _DefaultGoldRetriever()
        return self._retriever

    # ------------------------------------------------------------------
    # BM25 weighted search (private — raw SQL kept here pending Phase 5
    # lift onto ``DocumentRepository.search_fts_weighted``)
    # ------------------------------------------------------------------

    def _bm25_search_with_weights(
        self,
        query: str,
        weights: tuple[float, float, float],
        collections: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, str]]:
        """Run BM25 search with specific column weights.

        Returns list of {path, title, snippet, collection} dicts.

        TODO(#143 Phase 5): lift this onto ``DocumentRepository`` as a
        ``search_fts_weighted`` method so gold_builder no longer reaches
        past the protocol into raw SQL. Inappropriate intimacy retained
        for Phase 2b to keep this PR's blast radius small.
        """
        from kairix.core.db import get_db_path, open_db
        from kairix.core.search.tokenizer import tokenize_fts_query

        # Validate weights up-front (fail fast on nan / inf / non-positive).
        _validate_weights(weights)

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

        # contextlib.closing guarantees the connection closes on every exit
        # path, including exceptions raised inside the result loop.
        from contextlib import closing

        with closing(db) as conn:
            conn.row_factory = sqlite3.Row
            w_fp, w_title, w_doc = weights
            try:
                if collections:
                    placeholders = ",".join("?" * len(collections))
                    # safe: float() cast on bm25 weights (validated finite/positive above);
                    # no ? binding available for bm25 args
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
                    # safe: float() cast on bm25 weights (validated finite/positive above);
                    # no ? binding available for bm25 args
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

    # ------------------------------------------------------------------
    # Vector retrieval — routes through the injected Retriever protocol
    # ------------------------------------------------------------------

    def _vector_retrieve(
        self,
        query: str,
        collections: list[str] | None,
        limit: int,
    ) -> list[dict[str, str]]:
        """Run vector retrieval via the injected ``Retriever``.

        The retriever's ``retrieve`` returns a result whose shape varies by
        implementation (production: ``RetrievalResult`` with ``paths`` /
        ``snippets`` / ``meta``; tests: ``SimpleNamespace`` with
        ``results=[]``). Adapt both to the {path, title, snippet, collection}
        dict list ``pool`` consumes.
        """
        retriever = self._get_retriever()
        try:
            result = retriever.retrieve(query, collections=collections)
        except Exception as e:
            logger.warning("gold_builder: retriever.retrieve raised — %s", e)
            return []

        # RetrievalResult shape: paths / snippets / meta lists
        paths = getattr(result, "paths", None)
        if paths is not None:
            snippets = getattr(result, "snippets", []) or []
            return [
                {
                    "path": p,
                    "title": "",
                    "snippet": (snippets[i] if i < len(snippets) else "")[:300],
                    "collection": "",
                }
                for i, p in enumerate(paths[:limit])
            ]

        # FakeRetriever shape: results list of dicts (or other arbitrary objects).
        results = getattr(result, "results", None) or []
        out: list[dict[str, str]] = []
        for r in results[:limit]:
            if isinstance(r, dict):
                out.append(
                    {
                        "path": r.get("path", ""),
                        "title": r.get("title", ""),
                        "snippet": (r.get("snippet", "") or "")[:300],
                        "collection": r.get("collection", ""),
                    }
                )
        return out

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def pool(
        self,
        query: str,
        systems: list[str],
        collections: list[str] | None = None,
        limit_per_system: int = 10,
    ) -> list[PooledCandidate]:
        """Pool top-k results from multiple retrieval systems for a query.

        Deduplicates by path. Records which systems retrieved each document.
        BM25 variants use ``_bm25_search_with_weights``; the ``vector``
        system routes through the injected ``Retriever``.
        """
        candidates: dict[str, PooledCandidate] = {}

        for system in systems:
            if system == "vector":
                results = self._vector_retrieve(query, collections, limit_per_system)
            elif system in _WEIGHT_PRESETS:
                results = self._bm25_search_with_weights(query, _WEIGHT_PRESETS[system], collections, limit_per_system)
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

    def grade(
        self,
        query: str,
        candidates: list[PooledCandidate],
        *,
        runs: int = 1,
        api_key: str = "",
        endpoint: str = "",
    ) -> list[PooledCandidate]:
        """Grade each candidate via the injected LLMJudge.

        Runs the judge ``runs`` times and uses majority vote for the final
        grade. ``api_key`` / ``endpoint`` are forwarded to judge
        implementations that resolve credentials per-call (e.g. the
        production ``LLMJudge`` wrapping ``AzureChatBackend``).
        """
        if not candidates:
            return []

        # Build (doc_key, snippet) pairs — use path_title() for unique
        # keys so two files with the same stem (e.g. readme.md) get
        # independent grades.
        judge_candidates = [(path_title(c.path), c.snippet[:150]) for c in candidates]

        judge = self._get_llm_judge()
        graded_candidates: list[PooledCandidate] = list(candidates)

        for _run in range(runs):
            # The protocol's ``grade()`` signature is
            # ``grade(query, candidates, *, runs=1)`` — credential plumbing
            # is implementation-specific. The production ``LLMJudge``
            # accepts ``api_key`` / ``endpoint`` kwargs; the FakeLLMJudge
            # ignores them. Forward conditionally so we conform to the
            # protocol while still supplying production credentials.
            # Cast to Any so mypy doesn't reject the optional kwargs that
            # only the production implementation accepts.
            judge_any = cast(Any, judge)
            try:
                result = judge_any.grade(
                    query,
                    judge_candidates,
                    runs=1,
                    api_key=api_key,
                    endpoint=endpoint,
                )
            except TypeError:
                # FakeLLMJudge / minimal implementations without credential kwargs.
                result = judge.grade(query, judge_candidates, runs=1)

            grades_dict: dict[str, int] = getattr(result, "grades", {}) or {}
            for c in graded_candidates:
                doc_key = path_title(c.path)
                c.grade_votes.append(int(grades_dict.get(doc_key, 0)))

        # Majority vote
        for c in graded_candidates:
            if c.grade_votes:
                c.grade = max(set(c.grade_votes), key=c.grade_votes.count)

        return graded_candidates

    def build_independent_gold(
        self,
        suite_path: Path,
        output_path: Path,
        systems: list[str] | None = None,
        judge_runs: int = 2,
        calibrate_first: bool = True,
        limit_per_system: int = 10,
        credentials: tuple[str, str, str] | None = None,
    ) -> GoldBuildReport:
        """Build an independent gold suite using TREC-style pooling + LLM judge.

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
            credentials:       Optional (api_key, endpoint, deployment) tuple.
                               When omitted, fetched via ``fetch_llm_credentials``.

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
            api_key, endpoint, _deployment = credentials
        else:
            api_key, endpoint, _deployment = fetch_llm_credentials()
        if not api_key or not endpoint:
            logger.error("gold_builder: no API credentials — cannot run judge")
            return GoldBuildReport()

        # Calibrate via the injected judge
        judge = self._get_llm_judge()
        if calibrate_first:
            logger.info("gold_builder: running calibration...")
            # Cast to Any so the production ``calibrate(api_key=, endpoint=)``
            # call type-checks even though the protocol signature is
            # ``calibrate() -> bool``.
            judge_any = cast(Any, judge)
            try:
                judge_any.calibrate(api_key=api_key, endpoint=endpoint)
            except TypeError:
                # FakeLLMJudge.calibrate() takes no kwargs
                judge.calibrate()
            logger.info("gold_builder: calibration passed")

        report = GoldBuildReport()

        for i, case in enumerate(cases):
            query = case.get("query", "")
            if not query:
                continue

            logger.info("gold_builder: [%d/%d] %s", i + 1, len(cases), query[:60])

            # Pool candidates
            candidates = self.pool(query, systems, limit_per_system=limit_per_system)
            report.total_candidates_pooled += len(candidates)

            if not candidates:
                logger.warning("gold_builder: no candidates for query %r", query[:60])
                continue

            # Grade via the injected LLMJudge
            candidates = self.grade(
                query,
                candidates,
                runs=judge_runs,
                api_key=api_key,
                endpoint=endpoint,
            )
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


class _DefaultGoldRetriever:
    """Production Retriever for the gold builder — delegates to module-level
    ``_vector_search``.

    Conforms to ``kairix.core.protocols.Retriever``. Wraps the legacy
    ``_vector_search`` helper as an injectable object so production callers
    can route through the protocol while tests substitute a ``FakeRetriever``
    via ``GoldBuilder(retriever=...)``.

    Returns a ``SimpleNamespace`` with ``results=[...]`` and
    ``vec_failed=False`` matching the FakeRetriever default shape so the
    pool-time adapter in ``GoldBuilder._vector_retrieve`` handles both
    surfaces uniformly.
    """

    def retrieve(
        self,
        query: str,
        *,
        collections: list[str] | None = None,
        cfg: Any = None,
    ) -> Any:
        from types import SimpleNamespace

        results = _vector_search(query, collections, limit=cfg if isinstance(cfg, int) else 10)
        return SimpleNamespace(results=results, vec_failed=False)


# ---------------------------------------------------------------------------
# Backwards-compat module-level functions (DEPRECATED — Phase 4 removes the
# *_fn= kwargs).
#
# These wrappers preserve the existing public surface while routing through
# the new ``GoldBuilder`` class with default deps. The ``*_fn`` kwargs stay
# for callers who substitute via legacy injection; new code should use
# ``GoldBuilder`` with constructor-injected ``LLMJudge`` / ``Retriever``.
# ---------------------------------------------------------------------------


def pool_candidates(
    query: str,
    systems: list[str],
    collections: list[str] | None = None,
    limit_per_system: int = 10,
    search_fns: dict[str, Callable[..., list[dict[str, str]]]] | None = None,
) -> list[PooledCandidate]:
    """DEPRECATED — use ``GoldBuilder(...).pool(...)`` instead.

    Pool top-k results from multiple retrieval systems for a single query.

    Deduplicates by path. Records which systems retrieved each document.

    Args:
        search_fns: DEPRECATED (Phase 4 removes). Optional mapping of system
                    name to search callable. Each callable receives
                    (query, collections, limit) and returns list of
                    {path, title, snippet, collection} dicts. Production
                    defaults to internal BM25/vector functions.
    """
    # Backwards-compat fast path — when ``search_fns`` is supplied, honour
    # the legacy injection semantics directly (tests still rely on this).
    _fns = search_fns or {}
    if _fns:
        candidates: dict[str, PooledCandidate] = {}
        for system in systems:
            if system in _fns:
                results = _fns[system](query, collections, limit_per_system)
            elif system == "vector":
                results = _vector_search(query, collections, limit_per_system)
            elif system in _WEIGHT_PRESETS:
                # Use a builder so the validate-weights guard still fires.
                results = GoldBuilder()._bm25_search_with_weights(
                    query, _WEIGHT_PRESETS[system], collections, limit_per_system
                )
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

    return GoldBuilder().pool(query, systems, collections, limit_per_system)


def grade_candidates(
    query: str,
    candidates: list[PooledCandidate],
    api_key: str,
    endpoint: str,
    deployment: str = JUDGE_DEPLOYMENT,
    judge_runs: int = 2,
    judge_fn: Callable[..., JudgeResult] | None = None,
) -> list[PooledCandidate]:
    """DEPRECATED — use ``GoldBuilder(...).grade(...)`` instead.

    Grade each candidate using the LLM judge.

    Runs judge_runs times and uses majority vote for final grade.

    Args:
        judge_fn: DEPRECATED (Phase 4 removes). Legacy callable substitute
                  for ``judge_batch``. New code should construct
                  ``GoldBuilder(llm_judge=LLMJudge(chat_backend=...))``.
    """
    if not candidates:
        return []

    if judge_fn is None:
        return GoldBuilder().grade(query, candidates, runs=judge_runs, api_key=api_key, endpoint=endpoint)

    # Backwards-compat path — preserve legacy ``judge_fn`` semantics so any
    # caller still using the kwarg keeps working until Phase 4.
    judge_candidates = [(path_title(c.path), c.snippet[:150]) for c in candidates]
    graded_candidates: list[PooledCandidate] = list(candidates)

    for _run in range(judge_runs):
        result: JudgeResult = judge_fn(
            query=query,
            candidates=judge_candidates,
            api_key=api_key,
            endpoint=endpoint,
            deployment=deployment,
            shuffle=True,
        )
        for c in graded_candidates:
            doc_key = path_title(c.path)
            c.grade_votes.append(result.grades.get(doc_key, 0))

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
    """DEPRECATED — use ``GoldBuilder(...).build_independent_gold(...)`` instead.

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
        search_fns:    DEPRECATED (Phase 4 removes). Legacy per-system search
                       callable substitution.
        calibrate_fn:  DEPRECATED (Phase 4 removes). Legacy calibration
                       callable substitution.
        grade_fn:      DEPRECATED (Phase 4 removes). Legacy grade callable
                       substitution.

    Returns:
        GoldBuildReport with statistics.
    """
    # When no legacy substitution kwargs are supplied, route through the
    # class — production callers and most tests land here.
    if search_fns is None and calibrate_fn is None and grade_fn is None:
        return GoldBuilder().build_independent_gold(
            suite_path=suite_path,
            output_path=output_path,
            systems=systems,
            judge_runs=judge_runs,
            calibrate_first=calibrate_first,
            limit_per_system=limit_per_system,
            credentials=credentials,
        )

    # Legacy injection path — preserved verbatim so existing tests / callers
    # that still pass *_fn kwargs continue to work until Phase 4.
    if systems is None:
        systems = ["bm25-equal", "bm25-filepath", "bm25-title", "vector"]

    with open(suite_path) as f:
        suite_data = yaml.safe_load(f)

    cases = suite_data.get("cases", [])
    if not cases:
        logger.error("gold_builder: no cases found in suite %s", suite_path)
        return GoldBuildReport()

    if credentials is not None:
        api_key, endpoint, deployment = credentials
    else:
        api_key, endpoint, deployment = fetch_llm_credentials()
    if not api_key or not endpoint:
        logger.error("gold_builder: no API credentials — cannot run judge")
        return GoldBuildReport()

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

        candidates = pool_candidates(query, systems, limit_per_system=limit_per_system, search_fns=search_fns)
        report.total_candidates_pooled += len(candidates)

        if not candidates:
            logger.warning("gold_builder: no candidates for query %r", query[:60])
            continue

        _grade = grade_fn or grade_candidates
        candidates = _grade(query, candidates, api_key, endpoint, deployment, judge_runs)
        report.total_judge_calls += len(candidates) * judge_runs

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

        case["gold_titles"] = gold_titles
        case["score_method"] = "ndcg"
        if "gold_paths" in case:
            case["legacy_gold_paths"] = case.pop("gold_paths")

        report.queries_processed += 1

    suite_data.setdefault("meta", {})["gold_method"] = "trec-pooling-llm-judge"
    suite_data["meta"]["gold_systems"] = systems
    suite_data["meta"]["judge_runs"] = judge_runs
    suite_data["meta"]["n_cases"] = report.queries_processed

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        yaml.dump(suite_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    report.avg_candidates_per_query = (
        report.total_candidates_pooled / report.queries_processed if report.queries_processed > 0 else 0
    )

    logger.info("gold_builder: %s", report)
    return report


# Re-export the module-level _bm25_search_with_weights as a thin wrapper that
# constructs a one-off GoldBuilder. Some legacy callers may import it
# directly; Phase 4 removes the wrapper.
def _bm25_search_with_weights(
    query: str,
    weights: tuple[float, float, float],
    collections: list[str] | None = None,
    limit: int = 10,
) -> list[dict[str, str]]:
    """DEPRECATED — call ``GoldBuilder()._bm25_search_with_weights(...)``."""
    return GoldBuilder()._bm25_search_with_weights(query, weights, collections, limit)
