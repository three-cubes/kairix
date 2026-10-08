"""
Benchmark runner for kairix retrieval quality evaluation.

Runs a BenchmarkSuite against a configured retrieval system and produces
per-category and weighted-total scores.

Score methods:
  exact - gold_path present in top-5 retrieved paths (case-insensitive substring)
  fuzzy - gold_path present in top-10 (relaxed, for approximate matching)
  llm   - gpt-4o-mini rates retrieved content relevance 0.0-1.0; a judge failure
          leaves the case unscored (score None, counted in summary['judge_failures'])
          and excluded from every aggregate
  ndcg  - true NDCG@10 with graded relevance (0/1/2); also computes Hit@5 and MRR@10
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kairix.quality.benchmark.per_type_slicing import (
    aggregate_canary,
    aggregate_per_source_type,
)
from kairix.quality.benchmark.suite import BenchmarkSuite
from kairix.quality.completeness import judge_failures as _count_judge_failures
from kairix.quality.completeness import partial_warning
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
from kairix.quality.redaction import describe_exception
from kairix.quality.scoring.types import (
    JUDGE_FAILURE_BACKEND_ERROR,
    JUDGE_FAILURE_UNPARSEABLE,
    JudgeFailedError,
)

# F17 — category names + per-case fields repeated across scorer dispatch, summary
# emit, and CSV header / row paths; extract so a key rename hits a single edit site.
_CATEGORY_CLASSIFICATION = "classification"
_KEY_WEIGHTED_TOTAL = "weighted_total"
_KEY_SCORE_METHOD = "score_method"
_KEY_ELAPSED_MS = "elapsed_ms"
# F17 — NDCG@10 is the primary IR metric; the key appears in the summary
# emit, the human format block, and the ADR-028 per-source-type slice.
_KEY_NDCG_AT_10 = "ndcg_at_10"
# Per-case reason key + summary counter for LLM-judge failures (excluded from
# aggregates — a failed judgement is not a 0.0 "irrelevant" verdict).
_KEY_JUDGE_FAILURE = "judge_failure"
_KEY_JUDGE_ERROR = "judge_error"
_KEY_JUDGE_FAILURES = "judge_failures"
_GATE_JUDGE_COVERAGE = "judge_coverage"

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

    def classify_rules(self, query: str, agent: str) -> Any:
        """Return a rule-based classification result for ``(query, agent)``."""
        ...

    def classify_with_llm(self, query: str, agent: str) -> Any:
        """Return an LLM-based classification result for ``(query, agent)``."""
        ...


class DefaultContentClassifier:
    """Production ``ContentClassifier`` — delegates to the real classify modules.

    The two-step lookup is exercised end-to-end by unit tests that call
    ``classification_score`` without injecting a classifier:
      - a positive query hits ``classify_rules`` → ``classify_content`` (rule
        match returns a typed result).
      - an empty query produces ``unknown`` from rules, falling through to
        ``classify_with_llm`` → ``classify_with_llm("")``, which short-circuits
        to ``unknown`` without an API call.
    """

    def classify_rules(self, query: str, agent: str) -> Any:
        from kairix.core.classify.rules import classify_content

        return classify_content(query, agent=agent)

    def classify_with_llm(self, query: str, agent: str) -> Any:
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


def _default_retrieve(
    query: str,
    system: str,
    agent: str | None,
    db_path: str | None,
    collection: str | None,
    fusion_override: str | None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Production retrieve callable — delegates to ``runner.retrieve``.

    Wrapper exists so ``BenchmarkDeps.retrieve`` has a stable, typed
    default that doesn't import ``runner.retrieve`` at module-import
    time (avoiding a circular-import risk on the retrieve callers).
    """
    return retrieve(
        query=query,
        system=system,
        agent=agent or "",
        db_path=db_path,
        collection=collection,
        fusion_override=fusion_override,
    )


class _LazyDefaultChatBackend:
    """Production chat backend that defers provider resolution to call-time.

    Constructed eagerly (``BenchmarkDeps.chat_backend`` is a dataclass
    ``default_factory`` — the field is built every time ``BenchmarkDeps()``
    runs, even in tests that override the field via constructor kwarg),
    but the underlying ``kairix.config.yaml`` lookup + provider plugin
    resolution happens on first ``complete()`` call. This preserves the
    historical contract (``BenchmarkDeps()`` constructs cheaply; the chat
    backend only fails when actually used and credentials / config are
    missing) while routing the production path through the provider plugin.

    ``llm_judge`` converts the ValueError raised on ``complete()`` into a
    typed :class:`JudgeFailedError`, so a credential / config failure is
    reported as a judge failure rather than scored as "irrelevant".
    """

    def complete(
        self,
        prompt: str,
        *,
        api_key: str,
        endpoint: str,
        deployment: str,
        system: str | None = None,
        temperature: float = 0.0,
        timeout_s: float = 30.0,
    ) -> str:
        from kairix.paths import provider_name
        from kairix.providers import get_provider
        from kairix.quality.eval.chat_backend import ProviderEvalChatBackend

        name = provider_name()
        if name is None:
            raise ValueError(
                "kairix.config.yaml is missing the required 'provider:' field. "
                "fix: set 'provider: <plugin-name>' in kairix.config.yaml. "
                "next: see docs/architecture/provider-plugin-architecture.md."
            )
        backend = ProviderEvalChatBackend(get_provider(name))
        return backend.complete(
            prompt,
            api_key=api_key,
            endpoint=endpoint,
            deployment=deployment,
            system=system,
            temperature=temperature,
            timeout_s=timeout_s,
        )


def _default_chat_backend() -> ChatBackend:
    """Construct the production chat backend — backed by the configured provider plugin.

    Returns a :class:`_LazyDefaultChatBackend` that resolves the plugin from
    ``kairix.config.yaml``'s ``provider:`` field on the first ``complete()``
    call (deferred so ``BenchmarkDeps()`` stays cheap to construct even
    when the config is incomplete).
    """
    return _LazyDefaultChatBackend()


@dataclass
class BenchmarkDeps:
    """Injectable dependencies for ``run_benchmark`` and its helpers.

    Each field defaults to a production implementation; tests construct
    ``BenchmarkDeps`` with fakes from ``tests/fakes.py``. All boundary
    collaborators that ``run_benchmark`` / ``retrieve_case`` / ``score_case``
    delegate to are reified as one typed bag (F6 — no ``*_fn=None`` test
    seams threaded through production signatures).
    """

    classifier: ContentClassifier = field(default_factory=DefaultContentClassifier)
    chat_backend: ChatBackend = field(default_factory=_default_chat_backend)
    retrieve: Callable[..., tuple[list[str], list[str], dict[str, Any]]] = field(
        default_factory=lambda: _default_retrieve
    )


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
    steps; production uses ``DefaultContentClassifier`` which delegates to the
    real classify modules.
    """
    try:
        if classifier is None:
            classifier = DefaultContentClassifier()
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


def _judge_prompt(query: str, paths: list[str]) -> str:
    """Build the paths-only, 6-point relevance prompt.

    Matches the original run-benchmark-hybrid.py scorer so scores stay
    comparable across runs.
    """
    paths_text = "\n".join(f"- {p}" for p in paths[:5])
    return (
        f"You are evaluating memory retrieval quality for an AI agent system.\n\n"
        f"Query: {query}\n\n"
        f"Retrieved documents (paths):\n{paths_text}\n\n"
        "Score the retrieval quality from 0.0 to 1.0:\n"
        "- 1.0: Retrieved documents directly and completely answer the query\n"
        "- 0.8: Retrieved documents mostly answer the query with minor gaps\n"
        "- 0.6: Retrieved documents partially answer the query\n"
        "- 0.4: Retrieved documents are tangentially related\n"
        "- 0.2: Retrieved documents have minimal relevance\n"
        "- 0.0: Retrieved documents are irrelevant or empty\n\n"
        "Reply with ONLY a number between 0.0 and 1.0."
    )


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
        snippets:     Retrieved document snippets — accepted for caller
                      symmetry with ``score_case`` (``llm`` arm), but the
                      prompt judges paths-only to match the original
                      ``run-benchmark-hybrid.py`` scorer for cross-run
                      comparability.
        chat_backend: ``ChatBackend`` protocol implementation. Defaults to
                      ``_default_chat_backend()`` constructed lazily (resolves
                      the configured provider plugin from ``kairix.config.yaml``).

    Returns 0.0 when nothing was retrieved (a genuine "irrelevant or empty"
    verdict — the backend is not called).

    Raises:
        JudgeFailedError: ``backend_error`` when the backend raised (API /
            auth / timeout / missing provider) or replied blank (the
            provider's failure sentinel); ``unparseable_response`` when a
            non-blank reply is non-numeric or non-finite. A failure is never
            reported as a 0.0 score.
    """
    _ = snippets  # consumed at signature time; explicit drop documents intent
    if not paths:
        return 0.0
    if chat_backend is None:
        # Lazy production default — resolves the configured provider plugin
        # on first ``complete()``; a resolution failure surfaces as a
        # JudgeFailedError below. Success-path tests inject FakeChatBackend.
        chat_backend = _default_chat_backend()

    try:
        reply = chat_backend.complete(
            _judge_prompt(query, paths),
            api_key="",
            endpoint="",
            deployment="gpt-4o-mini",
        )
    except Exception as exc:
        raise JudgeFailedError.from_backend_exception(exc) from exc

    if not reply or not reply.strip():
        # Blank is the provider's failure sentinel, not an unparseable verdict.
        raise JudgeFailedError(JUDGE_FAILURE_BACKEND_ERROR, "empty judge reply (LLM backend failure)")
    try:
        score = float(reply)
    except (TypeError, ValueError) as exc:
        raise JudgeFailedError(JUDGE_FAILURE_UNPARSEABLE, f"non-numeric reply ({len(reply)} chars)") from exc
    if not math.isfinite(score):
        raise JudgeFailedError(JUDGE_FAILURE_UNPARSEABLE, "non-finite reply")
    return max(0.0, min(1.0, score))


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
    searcher: Callable[..., Any] | None = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """
    Run retrieval and return (paths, snippets, metadata).

    Args:
        searcher: Pre-bound search callable. Tests pass a ``_CapturingSearch``
                  to verify the resolved RetrievalConfig flows through.
                  ``None`` means use the production pipeline.
    """
    from kairix.quality.eval.retrieval import RetrievalDeps, retrieve

    deps = RetrievalDeps(searcher=searcher) if searcher is not None else None
    result = retrieve(
        query=query,
        system=system,
        agent=agent,
        limit=limit,
        db_path=db_path,
        collection=collection,
        fusion_override=fusion_override,
        deps=deps,
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
    """Return a brief diagnosis for a low-scoring category.

    ``category`` must be one of CATEGORY_WEIGHTS keys — the only call site,
    ``format_interpretation``, iterates exactly those keys. Unknown category
    raises KeyError (caller invariant violation).
    """
    if score >= CATEGORY_FLOOR:
        return "✅ above floor"
    diagnoses = {
        "recall": "❌ semantic matching not finding exact docs — check vector index freshness",
        "temporal": "❌ temporal weakness is likely an ingestion problem — date-aware chunking needed (Phase 2)",
        "entity": "❌ entity graph may be empty — seed entities via kairix entity suggest/validate",
        "conceptual": "❌ abstract queries not resolving — check intent classifier routing",
        "multi_hop": "❌ multi-hop requires connected retrieval — Phase 3 planning layer",
        "procedural": "❌ procedural docs not surfacing — check collection scope",
        _CATEGORY_CLASSIFICATION: "❌ classification rules not matching — check rules.py patterns",
    }
    return diagnoses[category]


def _format_per_source_type_block(per_source_type: dict[str, dict[str, float]]) -> list[str]:
    """Render the per-source-type slicing as fixed-column text lines.

    ADR-028 §"Quality evaluation" #1 — slice the overall NDCG@10 /
    MRR@10 / Hit@10 by the source-type of the gold answer doc. Empty
    block when no NDCG cases carry a derivable source type.
    """
    if not per_source_type:
        return []
    lines = ["", "Per source type:"]
    for stype in sorted(per_source_type.keys()):
        row = per_source_type[stype]
        n = int(row.get("n", 0))
        lines.append(
            f"  {stype:11} NDCG@10={row.get('ndcg_at_10', 0.0):.3f}  "
            f"MRR@10={row.get('mrr_at_10', 0.0):.3f}  "
            f"Hit@10={row.get('hit_at_10', 0.0):.3f}  (queries={n})"
        )
    return lines


def _format_canary_block(canary: dict[str, Any]) -> list[str]:
    """Render the canary pass-rate block. Empty when no canaries ran.

    ADR-028 §"Quality evaluation" #3 — boundary-spanning canaries fail
    loudly when a chunker regression splits an atomic unit.
    """
    overall = canary.get("overall", {}) if canary else {}
    total = int(overall.get("total", 0))
    if total == 0:
        return []
    passed = int(overall.get("passed", 0))
    rate = overall.get("rate", 0.0)
    lines = [
        "",
        f"Boundary-spanning canaries: {passed}/{total} passed ({rate * 100:.0f}%)",
    ]
    by_unit = canary.get("by_unit", {})
    for unit in sorted(by_unit.keys()):
        row = by_unit[unit]
        unit_passed = int(row.get("passed", 0))
        unit_total = int(row.get("total", 0))
        lines.append(f"  {unit:9} {unit_passed}/{unit_total} passed")
    return lines


def _format_judge_failures_block(judge_failures: int) -> list[str]:
    """Warn when LLM-judge failures left cases unscored (empty when none)."""
    if not judge_failures:
        return []
    return [
        "",
        partial_warning("this benchmark run", judge_failures),
        "   Unscored cases are excluded from every score (not counted as 0).",
        "   fix: check the provider credentials / 'provider:' in kairix.config.yaml.",
        "   next: re-run; per-case 'judge_failure' / 'judge_error' fields in the JSON output name the cause.",
    ]


def format_interpretation(result: BenchmarkResult) -> str:
    """Return a human-readable interpretation section."""
    lines: list[str] = []
    wt = result.summary[_KEY_WEIGHTED_TOTAL]
    tier = score_tier(wt)

    lines.append("=" * 60)
    lines.append("BENCHMARK RESULTS")
    lines.append("=" * 60)
    lines.append(f"Weighted total: {wt:.3f}  [{tier}]")
    ndcg = result.summary.get(_KEY_NDCG_AT_10)
    hit5 = result.summary.get("hit_rate_at_5")
    mrr = result.summary.get("mrr_at_10")
    if ndcg is not None:
        lines.append(f"NDCG@10:       {ndcg:.3f}  (Hit@5: {hit5:.3f}  MRR@10: {mrr:.3f})")
    lines.append("")
    lines.append("Category breakdown:")
    cat_scores = result.summary["category_scores"]
    # A partial run gets no per-category verdicts (its scores cover only the
    # judged cases) — never a "✅ above floor".
    partial = _count_judge_failures(result) > 0
    for cat, weight in CATEGORY_WEIGHTS.items():
        score = cat_scores.get(cat, 0.0)
        n = result.diagnostics.get("category_counts", {}).get(cat, 0)
        diagnosis = "(partial — not evaluated)" if partial else _category_diagnosis(cat, score)
        lines.append(f"  {cat:12} {score:.3f}  (weight {weight:.0%}, n={n})  {diagnosis}")

    # ADR-028 — per-source-type + canary slices, layered below the
    # legacy category breakdown so existing scrapers keep their format.
    lines.extend(_format_per_source_type_block(result.summary.get("per_source_type", {})))
    lines.extend(_format_canary_block(result.summary.get("canary", {})))
    lines.extend(_format_judge_failures_block(result.summary.get(_KEY_JUDGE_FAILURES, 0)))

    lines.append("")

    lines.extend(_format_verdict_block(result, wt, cat_scores))
    lines.append("=" * 60)
    return "\n".join(lines)


def _format_verdict_block(result: BenchmarkResult, wt: float, cat_scores: dict[str, float]) -> list[str]:
    """Phase-gate + category-floor verdict lines.

    A partial result (LLM-judge failures) gets NO pass / fail verdicts — its
    scores cover only the judged cases — just the failed judge-coverage gate
    and an INCONCLUSIVE line.
    """
    failures = _count_judge_failures(result)
    if failures:
        return [
            f"  JUDGE COVERAGE gate (0 judge failures): FAIL ❌ ({failures} unscored)",
            "  Phase gates / category floors: INCONCLUSIVE — partial result, not evaluated.",
            "",
        ]
    lines: list[str] = []
    for gate_name, gate_threshold in PHASE_GATES.items():
        status = "PASS ✅" if wt >= gate_threshold else f"FAIL ❌ (need +{gate_threshold - wt:.3f})"
        lines.append(f"  {gate_name.upper()} gate (≥{gate_threshold}): {status}")
    lines.append("")
    floors_failed = [cat for cat, score in cat_scores.items() if score < CATEGORY_FLOOR]
    if floors_failed:
        lines.append(f"Categories below floor ({CATEGORY_FLOOR}):")
        lines.extend(f"  {cat}: {cat_scores[cat]:.3f}" for cat in floors_failed)
    else:
        lines.append("All categories above floor ✅")
    return lines


# ---------------------------------------------------------------------------
# Extracted helpers for run_benchmark (reduce cognitive complexity)
# ---------------------------------------------------------------------------


def _score_classification(case: Any, _paths: list[str], deps: BenchmarkDeps) -> tuple[float, dict[str, Any]]:
    return classification_score(case.query, case.expected_type or "", classifier=deps.classifier), {}


def _score_exact(case: Any, paths: list[str], _deps: BenchmarkDeps) -> tuple[float, dict[str, Any]]:
    if case.gold_title:
        score = 1.0 if title_in_retrieved(case.gold_title, paths, EXACT_MATCH_TOPK) else 0.0
    else:
        score = exact_match(paths, case.gold_path or "")
    return score, {}


def _score_fuzzy(case: Any, paths: list[str], _deps: BenchmarkDeps) -> tuple[float, dict[str, Any]]:
    if case.gold_title:
        score = 1.0 if title_in_retrieved(case.gold_title, paths, FUZZY_MATCH_TOPK) else 0.0
    else:
        score = fuzzy_match(paths, case.gold_path or "")
    return score, {}


def _score_ndcg(case: Any, paths: list[str], _deps: BenchmarkDeps) -> tuple[float, dict[str, Any]]:
    effective_gold = (
        case.gold_titles or case.gold_paths or ([{"path": case.gold_path, "relevance": 2}] if case.gold_path else [])
    )
    score = ndcg_graded(paths, effective_gold, k=10)
    ndcg_detail = {
        "hit_at_5": hit_at_k_graded(paths, effective_gold, k=5),
        "rr": reciprocal_rank_graded(paths, effective_gold, k=10),
    }
    return score, ndcg_detail


_SCORE_DISPATCH: dict[str, Callable[[Any, list[str], BenchmarkDeps], tuple[float, dict[str, Any]]]] = {
    _CATEGORY_CLASSIFICATION: _score_classification,
    "exact": _score_exact,
    "fuzzy": _score_fuzzy,
    "ndcg": _score_ndcg,
}


def score_case(
    case: Any,
    paths: list[str],
    snippets: list[str],
    retrieval_meta: dict[str, Any],
    deps: BenchmarkDeps | None = None,
) -> tuple[float | None, dict[str, Any]]:
    """Dispatch to the correct score method for a single benchmark case.

    Returns (score, detail). ``detail`` carries the NDCG sub-metrics for NDCG
    cases. For ``llm`` cases whose judge failed, ``score`` is ``None`` and
    ``detail`` carries ``judge_failure`` (the :class:`JudgeFailedError`
    reason) and ``judge_error`` — the case is unscored, not scored 0.0.

    ``deps`` carries the classifier (for ``classification`` cases) and the
    chat backend (for ``llm`` cases). When ``None``, production defaults are
    constructed — the unit-test path always passes deps with fakes.

    ``retrieval_meta`` is accepted for caller symmetry with ``retrieve_case``
    (both helpers share the same tuple shape inside ``run_benchmark``); the
    dispatcher itself doesn't consult metadata to pick a score method.
    """
    _ = retrieval_meta  # explicit drop documents intent
    deps = deps if deps is not None else BenchmarkDeps()
    handler = _SCORE_DISPATCH.get(case.score_method)
    if handler is not None:
        return handler(case, paths, deps)
    # llm fallback
    try:
        return llm_judge(query=case.query, paths=paths, snippets=snippets, chat_backend=deps.chat_backend), {}
    except JudgeFailedError as exc:
        return None, {_KEY_JUDGE_FAILURE: exc.reason, _KEY_JUDGE_ERROR: exc.detail}


def retrieve_case(
    case: Any,
    system: str,
    agent: str | None,
    db_path: str | None,
    collection: str | None,
    fusion_override: str | None,
    deps: BenchmarkDeps | None = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Wrap retrieval with error handling; classification cases skip retrieval.

    The retrieval callable lives on ``deps.retrieve``; production calls land
    on ``runner.retrieve`` (delegating to the shared retrieval module).
    """
    if case.score_method == _CATEGORY_CLASSIFICATION:
        return [], [], {"scored_by": _CATEGORY_CLASSIFICATION}
    deps = deps if deps is not None else BenchmarkDeps()
    try:
        return deps.retrieve(
            query=case.query,
            system=system,
            agent=case.agent or agent,
            db_path=db_path,
            collection=collection,
            fusion_override=fusion_override,
        )
    except Exception as exc:
        return [], [], {"error": f"retrieval {describe_exception(exc)}"}


def aggregate_scores_by_category(
    category_scores: dict[str, list[float]],
) -> dict[str, float]:
    """Compute per-category averages from accumulated score lists."""
    return {cat: round(sum(scores) / len(scores), 4) if scores else 0.0 for cat, scores in category_scores.items()}


def compute_weighted_total(
    per_category_avg: dict[str, float],
    suite_version: str,
    *,
    per_category_n: dict[str, int] | None = None,
    min_n_for_full_weight: int = 10,
) -> float:
    """Apply category weights (with Phase 3 classification adjustment) and return weighted total.

    The result is in the closed interval [0, 1] — both ``score_tier`` and
    ``PHASE_GATES`` assume this. The Phase 3 adjustment moves 0.10 from
    ``temporal`` to ``classification`` so the weights conserve. (A previous
    revision used 0.15 for classification, which broke the [0, 1] range and
    let perfect-scoring v1.1 suites report 1.05; surfaced by contract test.)

    **Sample-size confidence floor** (new — closes the noise contribution
    that small-n categories add to the headline score). When
    ``per_category_n`` is provided, any category with ``n <
    min_n_for_full_weight`` has its weight zeroed and the freed budget is
    reallocated proportionally to categories that meet the floor.
    Rationale: a category with n=1 contributes pure noise to the weighted
    average; treating it as if it had the same statistical power as a
    category with n=75 distorts the headline.

    Example: on the 2026-06-08 reflib eval, temporal (n=5), entity (n=1),
    and multi_hop (n=2) had their full configured weights (0.20 / 0.20 /
    0.10 = 0.50 budget) despite being below the noise floor. With
    min_n_for_full_weight=10, those three contribute 0; their 0.50 budget
    is reallocated to recall (n=54), conceptual (n=75), and procedural
    (n=63) in proportion to their configured weights. The
    suite-design noise stops dragging the headline number.

    Backwards-compat: ``per_category_n=None`` (the default) reproduces
    the legacy behaviour exactly — every existing caller keeps working.
    """
    effective_weights = dict(CATEGORY_WEIGHTS)
    if suite_version >= "1.1" and per_category_avg.get(_CATEGORY_CLASSIFICATION, 0.0) > 0:
        # Conservation: temporal donates 0.10 to classification.
        effective_weights[_CATEGORY_CLASSIFICATION] = 0.10
        effective_weights["temporal"] = 0.10

    if per_category_n is not None:
        effective_weights = _apply_sample_size_floor(
            effective_weights,
            per_category_n,
            min_n_for_full_weight,
        )

    return round(
        sum(per_category_avg.get(cat, 0.0) * w for cat, w in effective_weights.items()),
        4,
    )


def _apply_sample_size_floor(
    weights: dict[str, float],
    per_category_n: dict[str, int],
    min_n: int,
) -> dict[str, float]:
    """Zero weights for categories below the noise floor; reallocate budget.

    Returns a new dict; never mutates ``weights``. Categories with n < min_n
    get weight 0; their original weight is summed into ``freed_budget`` and
    redistributed across categories with n >= min_n in proportion to their
    pre-reallocation weights. When EVERY category is below the floor, the
    function returns the input unchanged — a degraded-but-deterministic
    fallback that's better than silently zeroing everything.
    """
    qualified = {cat: weight for cat, weight in weights.items() if weight > 0 and per_category_n.get(cat, 0) >= min_n}
    if not qualified:
        # No category qualifies — keep the original weights as a fallback
        # so the score doesn't silently zero. The headline number will be
        # wrong but visible; the alternative is a deceptive 0.0 that hides
        # the underlying noise.
        return dict(weights)

    qualified_total = sum(qualified.values())
    freed_budget = sum(weight for cat, weight in weights.items() if weight > 0 and per_category_n.get(cat, 0) < min_n)

    new_weights: dict[str, float] = {cat: 0.0 for cat in weights}
    # Categories that qualify: keep their configured weight + their
    # proportional share of the freed budget.
    for cat, weight in qualified.items():
        share = weight / qualified_total
        new_weights[cat] = weight + freed_budget * share
    return new_weights


def aggregate_ndcg_metrics(
    case_results: list[dict[str, Any]],
) -> tuple[float | None, float | None, float | None]:
    """Compute NDCG@10, Hit@5, MRR@10 averages across NDCG-scored cases."""
    ndcg_cases = [c for c in case_results if c.get(_KEY_SCORE_METHOD) == "ndcg"]
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


def _build_single_shot_runs(case_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project the legacy case_results list onto the unified per-query shape.

    Returns a list of dicts matching ``QueryRunResult`` field names so the
    diagnostics envelope stays JSON-serialisable (the dataclass would
    require ``dataclasses.asdict`` or a custom encoder otherwise). First
    case is labelled ``cold``; the rest are ``warm`` — matching the
    convention encoded by :func:`kairix.quality.benchmark.modes.run_single_shot`.

    Pure projection — no retrieval, no scoring, no clock work. The legacy
    case loop has already produced everything we need; this just relabels.
    """
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(case_results):
        phase = "cold" if idx == 0 else "warm"
        succeeded = "error" not in row
        rows.append(
            {
                "case_id": row.get("id", ""),
                "category": row.get("category", ""),
                "query": row.get("query", ""),
                "latency_ms": row.get(_KEY_ELAPSED_MS, 0.0),
                "succeeded": succeeded,
                "score": row.get("score"),
                "stage_latency_ms": {},
                "error": row.get("error", "") or "",
                "latency_phase": phase,
            }
        )
    return rows


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
    deps: BenchmarkDeps | None = None,
    mode: str | None = None,
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
        deps:       Injectable boundary collaborators (classifier, chat backend,
                    retrieve). ``None`` means construct production defaults; tests
                    pass ``BenchmarkDeps(retrieve=fake_retrieve, classifier=...)``.
        mode:       Optional unified-runner mode tag. ``None`` (default) preserves the
                    legacy NDCG-aggregate-only path. ``"single-shot"`` additionally
                    surfaces per-query ``QueryRunResult`` rows on
                    ``result.diagnostics["per_query_runs"]`` via
                    :func:`kairix.quality.benchmark.modes.run_single_shot`. Concurrent
                    and soak modes are deferred (P3.b / P3.c slices).

    Returns:
        BenchmarkResult with summary, category scores, and per-case results.
    """
    # Validate suite prerequisites
    _validate_suite_prerequisites(suite)

    deps = deps if deps is not None else BenchmarkDeps()

    case_results: list[dict[str, Any]] = []
    all_categories = set(CATEGORY_WEIGHTS.keys()) | {_CATEGORY_CLASSIFICATION}
    category_scores: dict[str, list[float]] = {cat: [] for cat in all_categories}
    judge_failures = 0

    for case in suite.cases:
        t0 = time.time()

        paths, snippets, retrieval_meta = retrieve_case(
            case,
            system,
            agent,
            db_path,
            collection,
            fusion_override,
            deps,
        )
        score, ndcg_detail = score_case(case, paths, snippets, retrieval_meta, deps)
        elapsed_ms = (time.time() - t0) * 1000

        cat = CATEGORY_ALIASES.get(case.category, case.category)
        if score is None:
            # Judge failure — excluded from every aggregate (not counted as 0).
            judge_failures += 1
        elif cat in category_scores:
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
            _KEY_SCORE_METHOD,
            "score",
            "retrieved_paths",
            _KEY_ELAPSED_MS,
            # Judge diagnostics come only from score_case's detail — never
            # from retrieval metadata, which could fake or erase a failure.
            _KEY_JUDGE_FAILURE,
            _KEY_JUDGE_ERROR,
        }
        safe_extras: dict[str, Any] = {
            k: v for k, v in {**ndcg_detail, **retrieval_meta}.items() if k not in canonical_keys
        }
        judge_diagnostics = {k: ndcg_detail[k] for k in (_KEY_JUDGE_FAILURE, _KEY_JUDGE_ERROR) if k in ndcg_detail}
        case_results.append(
            {
                "id": case.id,
                "category": cat,
                "original_category": case.category,
                "query": case.query,
                "gold_path": case.gold_path,
                _KEY_SCORE_METHOD: case.score_method,
                "score": round(score, 4) if score is not None else None,
                "retrieved_paths": paths[:10],
                _KEY_ELAPSED_MS: round(elapsed_ms, 1),
                **judge_diagnostics,
                **safe_extras,
            }
        )

    # Aggregate
    per_category_avg = aggregate_scores_by_category(category_scores)
    per_category_n = {cat: len(scores) for cat, scores in category_scores.items()}

    # Phase 3 weight model: classification gets 0.15 weight; temporal reduced to 0.10.
    # Sample-size confidence floor: categories with n < 10 are noisy; their
    # weight is reallocated to qualified categories. Avoids small-n drag
    # on the headline score that was observed on the 2026-06-08 reflib eval.
    suite_version = suite.meta.get("version", "1.0")
    weighted_total = compute_weighted_total(
        per_category_avg,
        suite_version,
        per_category_n=per_category_n,
    )

    gates = {gate: weighted_total >= threshold for gate, threshold in PHASE_GATES.items()}
    # Incomplete judge coverage: the weighted total covers only the judged
    # cases, so it can clear every phase gate while a whole slice went
    # unscored. Any judge failure fails this gate (and so ``--gates``);
    # the unscored case rows keep their judge_failure / judge_error detail.
    gates[_GATE_JUDGE_COVERAGE] = judge_failures == 0
    ndcg_at_10, hit_rate_at_5, mrr_at_10 = aggregate_ndcg_metrics(case_results)

    diagnostics: dict[str, Any] = {
        "category_counts": {cat: len(scores) for cat, scores in category_scores.items()},
    }
    if mode == "single-shot":
        diagnostics["mode"] = "single-shot"
        diagnostics["per_query_runs"] = _build_single_shot_runs(case_results)

    # ADR-028 §"Quality evaluation" — per-source-type Recall@k slicing
    # + boundary-spanning canary aggregation. Layered on top of the
    # existing summary so the overall NDCG@10 / MRR / Hit@5 numbers
    # stay untouched and downstream consumers that only read those keys
    # see no regression. New consumers read ``per_source_type`` /
    # ``canary`` for the per-type breakdown.
    per_source_type = aggregate_per_source_type(suite.cases, case_results)
    canary_summary = aggregate_canary(suite.cases, case_results)

    result = BenchmarkResult(
        meta={
            "suite_name": suite.meta.get("name", "unknown"),
            "system": system,
            "agent": agent,
            "collection": collection,
            "fusion_override": fusion_override,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "n_cases": len(suite.cases),
            _KEY_WEIGHTED_TOTAL: weighted_total,
            "mode": mode,
        },
        summary={
            _KEY_WEIGHTED_TOTAL: weighted_total,
            "category_scores": per_category_avg,
            "gates": gates,
            _KEY_NDCG_AT_10: ndcg_at_10,
            "hit_rate_at_5": hit_rate_at_5,
            "mrr_at_10": mrr_at_10,
            "per_source_type": per_source_type,
            "canary": canary_summary,
            _KEY_JUDGE_FAILURES: judge_failures,
        },
        diagnostics=diagnostics,
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
