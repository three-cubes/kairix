"""
Benchmark runner for kairix retrieval quality evaluation.

Runs a BenchmarkSuite against a configured retrieval system and produces
per-category and weighted-total scores.

Score methods:
  exact - gold_path present in top-5 retrieved paths (case-insensitive substring)
  fuzzy - gold_path present in top-10 (relaxed, for approximate matching)
  llm   - gpt-4o-mini rates retrieved content relevance 0.0-1.0
  ndcg  - true NDCG@10 with graded relevance (0/1/2); also computes Hit@5 and MRR@10
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kairix.quality.benchmark.suite import BenchmarkSuite
from kairix.quality.eval.constants import (
    CATEGORY_ALIASES,
    CATEGORY_WEIGHTS,
    PHASE_GATES,
)
from kairix.quality.eval.metrics import (
    hit_at_k_graded,
    match_gold_to_path,
    ndcg_graded,
    reciprocal_rank_graded,
)

if TYPE_CHECKING:
    from kairix.core.protocols import ChatBackend


@runtime_checkable
class ContentClassifier(Protocol):
    """Two-step classifier surface used by the benchmark runner.

    Production: ``ContentClassifier`` wraps ``kairix.core.classify.rules.classify_content``
    and ``kairix.core.classify.judge.classify_with_llm``. Tests pass a
    ``FakeContentClassifier`` from ``tests/fakes.py`` instead of substituting
    individual ``classify_fn`` / ``classify_llm_fn`` callables.
    """

    def classify_rules(self, query: str, agent: str) -> Any: ...

    def classify_with_llm(self, query: str, agent: str) -> Any: ...


class _DefaultContentClassifier:
    """Production ``ContentClassifier`` — delegates to the real classify modules."""

    def classify_rules(
        self, query: str, agent: str
    ) -> Any:  # pragma: no cover — needs the real classify_content; deferred to integration coverage
        from kairix.core.classify.rules import classify_content

        return classify_content(query, agent=agent)

    def classify_with_llm(self, query: str, agent: str) -> Any:  # pragma: no cover — same as above
        from kairix.core.classify.judge import classify_with_llm

        return classify_with_llm(query, agent=agent)


# Re-export so existing `from kairix.quality.benchmark.runner import CATEGORY_WEIGHTS` keeps working

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCORE_TIERS = [
    (0.80, "Phase 4 target — fully-tuned with synthesis"),
    (0.75, "Production quality — Phase 3 gate"),
    (0.68, "Phase 2 gate — temporal + tiered context working"),
    (0.62, "Phase 1 gate — hybrid search + entity graph"),
    (0.51, "Typical BM25 on well-curated vault"),
    (0.35, "BM25 on Phase 1 query suite"),
    (0.00, "Below BM25 baseline — something is broken"),
]

CATEGORY_FLOOR = 0.50  # per-category minimum for gate pass

# How many top results to inspect for exact/fuzzy matching
EXACT_MATCH_TOPK = 5
FUZZY_MATCH_TOPK = 10

# match_gold_to_path re-exported from metrics for external callers
__all__ = ["CATEGORY_ALIASES", "CATEGORY_WEIGHTS", "PHASE_GATES", "match_gold_to_path"]


def title_in_retrieved(gold_title: str, retrieved_paths: list[str], top_k: int) -> bool:
    """True if any of the top-k retrieved paths resolves to the gold title."""
    return any(match_gold_to_path(gold_title, p) for p in retrieved_paths[:top_k])


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkResult:
    meta: dict[str, Any]
    summary: dict[str, Any]  # weighted_total, category_scores, gate dict
    diagnostics: dict[str, Any]
    cases: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Score helpers
# ---------------------------------------------------------------------------


def exact_match(paths: list[str], gold: str) -> float:
    """1.0 if gold path is a case-insensitive substring of any top-K result paths."""
    if not gold:
        return 0.0
    gold_lower = gold.lower().replace("\\", "/")
    # Also match just the filename portion
    gold_parts = gold_lower.split("/")
    for path in paths[:EXACT_MATCH_TOPK]:
        path_lower = path.lower().replace("\\", "/")
        if gold_lower in path_lower or path_lower in gold_lower:
            return 1.0
        # Match on last N path components
        for n in range(len(gold_parts), 0, -1):
            suffix = "/".join(gold_parts[-n:])
            if suffix and suffix in path_lower:
                return 1.0
    return 0.0


def classification_score(
    query: str,
    expected_type: str,
    classifier: ContentClassifier | None = None,
) -> float:
    """Score a classification case by running kairix classify and comparing type.

    Returns 1.0 if the classifier's result.type matches ``expected_type``, 0.0 otherwise.
    Two-step: rules first; if the rules return ``unknown``, fall back to the LLM
    classifier. Returns 0.0 on any exception.

    Tests pass a ``FakeContentClassifier`` from tests/fakes.py to control both
    steps; production uses ``_DefaultContentClassifier`` which delegates to the
    real classify modules.
    """
    try:
        if classifier is None:
            classifier = _DefaultContentClassifier()
        result = classifier.classify_rules(query, agent="shared")
        if result.type == "unknown":
            result = classifier.classify_with_llm(query, agent="shared")
        return 1.0 if result.type == expected_type else 0.0
    except Exception:
        return 0.0


def fuzzy_match(paths: list[str], gold: str) -> float:
    """1.0 if gold path is in any top-10 result paths."""
    if not gold:
        return 0.0
    gold_lower = gold.lower().replace("\\", "/")
    gold_parts = gold_lower.split("/")
    for path in paths[:FUZZY_MATCH_TOPK]:
        path_lower = path.lower().replace("\\", "/")
        if gold_lower in path_lower or path_lower in gold_lower:
            return 1.0
        for n in range(len(gold_parts), 0, -1):
            suffix = "/".join(gold_parts[-n:])
            if suffix and suffix in path_lower:
                return 1.0
    return 0.0


def llm_judge(
    query: str,
    paths: list[str],
    snippets: list[str],
    chat_backend: ChatBackend | None = None,
) -> float:
    """Score 0.0-1.0 using gpt-4o-mini as a relevance judge.

    Args:
        query:        The search query to judge.
        paths:        Retrieved document paths.
        snippets:     Retrieved document snippets.
        chat_backend: ``ChatBackend`` protocol implementation. Defaults to
                      ``AzureChatBackend`` constructed lazily.

    Returns 0.0 on any failure (API error, parse error, timeout).
    """
    try:
        if not paths:
            return 0.0
        if (
            chat_backend is None
        ):  # pragma: no cover — production-only lazy AzureChatBackend construction; tests inject FakeChatBackend
            from kairix._azure import AzureChatBackend

            chat_backend = AzureChatBackend()

        # Match the original run-benchmark-hybrid.py scorer — paths only, 6-point scale.
        # This ensures scores are comparable across runs.
        snippets_text = "\n".join(f"- {p}" for p in paths[:5])
        prompt = (
            f"You are evaluating memory retrieval quality for an AI agent system.\n\n"
            f"Query: {query}\n\n"
            f"Retrieved documents (paths):\n{snippets_text}\n\n"
            "Score the retrieval quality from 0.0 to 1.0:\n"
            "- 1.0: Retrieved documents directly and completely answer the query\n"
            "- 0.8: Retrieved documents mostly answer the query with minor gaps\n"
            "- 0.6: Retrieved documents partially answer the query\n"
            "- 0.4: Retrieved documents are tangentially related\n"
            "- 0.2: Retrieved documents have minimal relevance\n"
            "- 0.0: Retrieved documents are irrelevant or empty\n\n"
            "Reply with ONLY a number between 0.0 and 1.0."
        )

        content = chat_backend.complete(
            prompt,
            api_key="",
            endpoint="",
            deployment="gpt-4o-mini",
        )
        score = float(content)
        return max(0.0, min(1.0, score))

    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Retrieval — delegates to shared retrieval module
# ---------------------------------------------------------------------------


def retrieve(
    query: str,
    system: str,
    agent: str,
    limit: int = 10,
    db_path: str | None = None,
    collection: str | None = None,
    fusion_override: str | None = None,
    search_fn: Callable | None = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """
    Run retrieval and return (paths, snippets, metadata).

    Args:
        search_fn: Injectable search function for testing.
                   Defaults to the production hybrid search.
    """
    from kairix.quality.eval.retrieval import retrieve

    result = retrieve(
        query=query,
        system=system,
        agent=agent,
        limit=limit,
        db_path=db_path,
        collection=collection,
        fusion_override=fusion_override,
        search_fn=search_fn,
    )
    return result.paths, result.snippets, result.meta


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------


def score_tier(score: float) -> str:
    for threshold, label in SCORE_TIERS:
        if score >= threshold:
            return label
    return SCORE_TIERS[-1][1]


def _category_diagnosis(category: str, score: float) -> str:
    """Return a brief diagnosis for a low-scoring category."""
    if score >= CATEGORY_FLOOR:
        return "✅ above floor"
    diagnoses = {
        "recall": "❌ semantic matching not finding exact docs — check vector index freshness",
        "temporal": "❌ temporal weakness is likely an ingestion problem — date-aware chunking needed (Phase 2)",
        "entity": "❌ entity graph may be empty — seed entities via kairix entity suggest/validate",
        "conceptual": "❌ abstract queries not resolving — check intent classifier routing",
        "multi_hop": "❌ multi-hop requires connected retrieval — Phase 3 planning layer",
        "procedural": "❌ procedural docs not surfacing — check collection scope",
        "classification": "❌ classification rules not matching — check rules.py patterns",
    }
    # The dict-default branch is unreachable through ``format_interpretation``,
    # which iterates only ``CATEGORY_WEIGHTS`` keys (all present in ``diagnoses``).
    return diagnoses.get(category, f"❌ score {score:.3f} below floor {CATEGORY_FLOOR}")  # pragma: no cover


def format_interpretation(result: BenchmarkResult) -> str:
    """Return a human-readable interpretation section."""
    lines: list[str] = []
    wt = result.summary["weighted_total"]
    tier = score_tier(wt)

    lines.append("=" * 60)
    lines.append("BENCHMARK RESULTS")
    lines.append("=" * 60)
    lines.append(f"Weighted total: {wt:.3f}  [{tier}]")
    ndcg = result.summary.get("ndcg_at_10")
    hit5 = result.summary.get("hit_rate_at_5")
    mrr = result.summary.get("mrr_at_10")
    if ndcg is not None:
        lines.append(f"NDCG@10:       {ndcg:.3f}  (Hit@5: {hit5:.3f}  MRR@10: {mrr:.3f})")
    lines.append("")
    lines.append("Category breakdown:")
    cat_scores = result.summary["category_scores"]
    for cat, weight in CATEGORY_WEIGHTS.items():
        score = cat_scores.get(cat, 0.0)
        n = result.diagnostics.get("category_counts", {}).get(cat, 0)
        diagnosis = _category_diagnosis(cat, score)
        lines.append(f"  {cat:12} {score:.3f}  (weight {weight:.0%}, n={n})  {diagnosis}")
    lines.append("")

    # Gate check
    for gate_name, gate_threshold in PHASE_GATES.items():
        status = "PASS ✅" if wt >= gate_threshold else f"FAIL ❌ (need +{gate_threshold - wt:.3f})"
        lines.append(f"  {gate_name.upper()} gate (≥{gate_threshold}): {status}")
    lines.append("")

    # Per-category floor check
    floors_failed = [cat for cat, score in cat_scores.items() if score < CATEGORY_FLOOR]
    if floors_failed:
        lines.append(f"Categories below floor ({CATEGORY_FLOOR}):")
        for cat in floors_failed:
            lines.append(f"  {cat}: {cat_scores[cat]:.3f}")
    else:
        lines.append("All categories above floor ✅")

    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Extracted helpers for run_benchmark (reduce cognitive complexity)
# ---------------------------------------------------------------------------


def score_case(
    case: Any,
    paths: list[str],
    snippets: list[str],
    retrieval_meta: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    """Dispatch to the correct score method for a single benchmark case.

    Returns (score, ndcg_detail) where ndcg_detail is non-empty only for NDCG cases.
    """
    if case.score_method == "classification":
        return classification_score(case.query, case.expected_type or ""), {}

    if case.score_method == "exact":
        if case.gold_title:
            score = 1.0 if title_in_retrieved(case.gold_title, paths, EXACT_MATCH_TOPK) else 0.0
        else:
            score = exact_match(paths, case.gold_path or "")
        return score, {}

    if case.score_method == "fuzzy":
        if case.gold_title:
            score = 1.0 if title_in_retrieved(case.gold_title, paths, FUZZY_MATCH_TOPK) else 0.0
        else:
            score = fuzzy_match(paths, case.gold_path or "")
        return score, {}

    if case.score_method == "ndcg":
        effective_gold = (
            case.gold_titles
            or case.gold_paths
            or ([{"path": case.gold_path, "relevance": 2}] if case.gold_path else [])
        )
        score = ndcg_graded(paths, effective_gold, k=10)
        ndcg_detail = {
            "hit_at_5": hit_at_k_graded(paths, effective_gold, k=5),
            "rr": reciprocal_rank_graded(paths, effective_gold, k=10),
        }
        return score, ndcg_detail

    # llm fallback
    return llm_judge(query=case.query, paths=paths, snippets=snippets), {}


def retrieve_case(
    case: Any,
    system: str,
    agent: str | None,
    db_path: str | None,
    collection: str | None,
    fusion_override: str | None,
    retrieve_fn: Callable[..., tuple[list[str], list[str], dict[str, Any]]] | None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Wrap retrieval with error handling; classification cases skip retrieval."""
    if case.score_method == "classification":
        return [], [], {"scored_by": "classification"}
    try:
        _retrieve = retrieve_fn or retrieve
        return _retrieve(
            query=case.query,
            system=system,
            agent=case.agent or agent,
            db_path=db_path,
            collection=collection,
            fusion_override=fusion_override,
        )
    except Exception as exc:
        return [], [], {"error": str(exc)}


def aggregate_scores_by_category(
    category_scores: dict[str, list[float]],
) -> dict[str, float]:
    """Compute per-category averages from accumulated score lists."""
    return {cat: round(sum(scores) / len(scores), 4) if scores else 0.0 for cat, scores in category_scores.items()}


def compute_weighted_total(
    per_category_avg: dict[str, float],
    suite_version: str,
) -> float:
    """Apply category weights (with Phase 3 classification adjustment) and return weighted total.

    The result is in the closed interval [0, 1] — both ``score_tier`` and
    ``PHASE_GATES`` assume this. The Phase 3 adjustment moves 0.10 from
    ``temporal`` to ``classification`` so the weights conserve. (A previous
    revision used 0.15 for classification, which broke the [0, 1] range and
    let perfect-scoring v1.1 suites report 1.05; surfaced by contract test.)
    """
    effective_weights = dict(CATEGORY_WEIGHTS)
    if suite_version >= "1.1" and per_category_avg.get("classification", 0.0) > 0:
        # Conservation: temporal donates 0.10 to classification.
        effective_weights["classification"] = 0.10
        effective_weights["temporal"] = 0.10
    return round(
        sum(per_category_avg.get(cat, 0.0) * w for cat, w in effective_weights.items()),
        4,
    )


def aggregate_ndcg_metrics(
    case_results: list[dict[str, Any]],
) -> tuple[float | None, float | None, float | None]:
    """Compute NDCG@10, Hit@5, MRR@10 averages across NDCG-scored cases."""
    ndcg_cases = [c for c in case_results if c.get("score_method") == "ndcg"]
    if not ndcg_cases:
        return None, None, None
    n = len(ndcg_cases)
    ndcg_at_10 = round(sum(c["score"] for c in ndcg_cases) / n, 4)
    hit_rate_at_5 = round(sum(float(c.get("hit_at_5", 0)) for c in ndcg_cases) / n, 4)
    mrr_at_10 = round(sum(c.get("rr", 0.0) for c in ndcg_cases) / n, 4)
    return ndcg_at_10, hit_rate_at_5, mrr_at_10


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def _validate_suite_prerequisites(suite: BenchmarkSuite) -> None:
    """Validate suite has usable gold references before scoring.

    Logs warnings for cases that will produce zero scores due to missing
    gold data. Raises ValueError if no cases have scorable gold references.
    """
    import logging

    logger = logging.getLogger(__name__)
    empty_recall = 0
    total_recall = 0

    for case in suite.cases:
        if case.category == "recall" and case.score_method == "ndcg":
            total_recall += 1
            if not case.gold_titles and not case.gold_paths and not case.gold_path:
                empty_recall += 1

    if empty_recall > 0:
        logger.warning(
            "benchmark: %d/%d recall cases have no gold references — these will score 0.0",
            empty_recall,
            total_recall,
        )

    if total_recall > 0 and empty_recall == total_recall:
        raise ValueError(
            f"All {total_recall} recall cases have no gold references. "
            "Cannot produce meaningful benchmark results. "
            "Regenerate the suite: kairix eval generate --output <suite.yaml>"
        )


def run_benchmark(
    suite: BenchmarkSuite,
    system: str = "hybrid",
    agent: str | None = None,
    output_dir: str | None = None,
    db_path: str | None = None,
    collection: str | None = None,
    fusion_override: str | None = None,
    retrieve_fn: Callable[..., tuple[list[str], list[str], dict[str, Any]]] | None = None,
) -> BenchmarkResult:
    """
    Run all benchmark cases and return a BenchmarkResult.

    Args:
        suite:      Loaded and validated BenchmarkSuite.
        system:     Retrieval system: 'hybrid', 'bm25', 'vector', 'mock', or 'mock-reflib'.
        agent:      Agent name for collection scoping.
        output_dir: If set, write JSON result file here.
        db_path:    Optional path to a specific database. Propagated to the retrieval
                    backend so it can target a specific store. None means use the default.

    Returns:
        BenchmarkResult with summary, category scores, and per-case results.
    """
    # Validate suite prerequisites
    _validate_suite_prerequisites(suite)

    case_results: list[dict[str, Any]] = []
    all_categories = set(CATEGORY_WEIGHTS.keys()) | {"classification"}
    category_scores: dict[str, list[float]] = {cat: [] for cat in all_categories}

    for case in suite.cases:
        t0 = time.time()

        paths, snippets, retrieval_meta = retrieve_case(
            case,
            system,
            agent,
            db_path,
            collection,
            fusion_override,
            retrieve_fn,
        )
        score, ndcg_detail = score_case(case, paths, snippets, retrieval_meta)
        elapsed_ms = (time.time() - t0) * 1000

        cat = CATEGORY_ALIASES.get(case.category, case.category)
        if cat in category_scores:
            category_scores[cat].append(score)

        # Build the canonical case-result dict first, then layer ndcg_detail
        # and retrieval_meta on top WITHOUT letting them stomp the canonical
        # fields. A custom retrieve_fn returning meta with keys like ``id``
        # or ``score`` would otherwise silently rewrite the case identity —
        # surfaced by contract test.
        canonical_keys = {
            "id",
            "category",
            "original_category",
            "query",
            "gold_path",
            "score_method",
            "score",
            "retrieved_paths",
            "elapsed_ms",
        }
        safe_extras: dict[str, Any] = {
            k: v for k, v in {**ndcg_detail, **retrieval_meta}.items() if k not in canonical_keys
        }
        case_results.append(
            {
                "id": case.id,
                "category": cat,
                "original_category": case.category,
                "query": case.query,
                "gold_path": case.gold_path,
                "score_method": case.score_method,
                "score": round(score, 4),
                "retrieved_paths": paths[:10],
                "elapsed_ms": round(elapsed_ms, 1),
                **safe_extras,
            }
        )

    # Aggregate
    per_category_avg = aggregate_scores_by_category(category_scores)

    # Phase 3 weight model: classification gets 0.15 weight; temporal reduced to 0.10.
    suite_version = suite.meta.get("version", "1.0")
    weighted_total = compute_weighted_total(per_category_avg, suite_version)

    gates = {gate: weighted_total >= threshold for gate, threshold in PHASE_GATES.items()}
    ndcg_at_10, hit_rate_at_5, mrr_at_10 = aggregate_ndcg_metrics(case_results)

    result = BenchmarkResult(
        meta={
            "suite_name": suite.meta.get("name", "unknown"),
            "system": system,
            "agent": agent,
            "collection": collection,
            "fusion_override": fusion_override,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "n_cases": len(suite.cases),
            "weighted_total": weighted_total,
        },
        summary={
            "weighted_total": weighted_total,
            "category_scores": per_category_avg,
            "gates": gates,
            "ndcg_at_10": ndcg_at_10,
            "hit_rate_at_5": hit_rate_at_5,
            "mrr_at_10": mrr_at_10,
        },
        diagnostics={
            "category_counts": {cat: len(scores) for cat, scores in category_scores.items()},
        },
        cases=case_results,
    )

    # Save to file if requested
    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        suite_slug = suite.meta.get("name", "suite").lower().replace(" ", "-")
        filename = f"B-{suite_slug}-{system}-{date_str}.json"
        out_path = Path(output_dir) / filename
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "meta": result.meta,
                    "summary": result.summary,
                    "diagnostics": result.diagnostics,
                    "cases": result.cases,
                },
                f,
                indent=2,
            )
        import logging as _logging

        _logging.getLogger(__name__).info("Results saved to: %s", out_path)

    return result
