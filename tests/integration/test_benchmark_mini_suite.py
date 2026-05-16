"""End-to-end integration tests for the benchmark runner.

Wires the production ``run_benchmark`` orchestrator over an inline
``BenchmarkSuite`` with 4 cases spanning the major categories. The
retrieval boundary is faked at ``BenchmarkDeps.retrieve`` so the test
doesn't need a SQLite index or the bundled reflib corpus — the runner
itself, the score dispatch, the weighted-total math, and the gate
verdicts are all real production code paths.

What's covered here that unit + BDD don't catch:
  - The full per-case loop: retrieve, score_case, aggregate, with the
    4 different score_methods routing through ``_SCORE_DISPATCH``.
  - ``weighted_total`` actually composes from per-category averages
    against ``CATEGORY_WEIGHTS`` (Phase-3 weighting math).
  - The gate verdict dict (``phase1``/``phase2``/``phase3``) is
    produced and the ``[0, 1]`` invariant on weighted_total holds.
  - The classification arm doesn't try to retrieve — it goes straight
    to the injected classifier. Confirms the runner respects the
    "skip retrieval for classification" branch end-to-end.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.quality.benchmark.runner import (
    BenchmarkDeps,
    run_benchmark,
)
from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite
from tests.fakes import FakeChatBackend

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Boundary fakes
# ---------------------------------------------------------------------------


class _ScriptedRetrieve:
    """Returns a per-query canned ``(paths, snippets, meta)`` triple.

    Unknown queries map to ``([], [], {})``. The runner's classification
    arm never reaches this — that arm's score_method short-circuits
    retrieval — so the dict doesn't need a classification entry.
    """

    def __init__(self, by_query: dict[str, tuple[list[str], list[str], dict[str, Any]]]) -> None:
        self._by_query = by_query
        self.calls: list[str] = []

    def __call__(self, **kwargs: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        query = kwargs.get("query", "")
        self.calls.append(query)
        return self._by_query.get(query, ([], [], {}))


def _build_mini_suite() -> BenchmarkSuite:
    """3-category mini suite with 4 cases.

    - recall (ndcg): a graded title-based recall case.
    - entity (exact): an entity lookup with gold_path.
    - conceptual (fuzzy): a softer match across top-10.
    - classification (classification): a query/expected_type check.
    """
    return BenchmarkSuite(
        meta={"name": "mini-integration-suite", "version": "1.0"},
        cases=[
            BenchmarkCase(
                id="recall-1",
                category="recall",
                query="kairix architecture overview",
                gold_path=None,
                score_method="ndcg",
                gold_titles=[
                    {"title": "Architecture Overview", "relevance": 2},
                    {"title": "Performance Notes", "relevance": 1},
                ],
            ),
            BenchmarkCase(
                id="entity-1",
                category="entity",
                query="who is openclaw",
                gold_path="entities/openclaw.md",
                score_method="exact",
            ),
            BenchmarkCase(
                id="conceptual-1",
                category="conceptual",
                query="bm25 ranking how it works",
                gold_path="notes/bm25.md",
                score_method="fuzzy",
            ),
            BenchmarkCase(
                id="classify-1",
                category="classification",
                query="search for openclaw",
                gold_path=None,
                score_method="classification",
                expected_type="entity",
            ),
        ],
    )


class _AlwaysEntityClassifier:
    """Stand-in for ContentClassifier: always classifies as 'entity'.

    Returns objects with a ``.type`` attribute so the production
    ``classification_score`` (which reads ``result.type``) is exercised
    end-to-end without hitting the rules/judge modules.
    """

    class _R:
        type: str = "entity"

    def classify_rules(self, query: str, agent: str) -> Any:
        del query, agent
        return self._R()

    def classify_with_llm(self, query: str, agent: str) -> Any:
        del query, agent
        return self._R()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_benchmark_runs_mini_suite_and_emits_full_envelope() -> None:
    """A 4-case suite where every case is a hit produces weighted_total
    in [0, 1] and a gates dict carrying phase1/phase2/phase3 verdicts.

    Sabotage: if ``run_benchmark`` stopped composing the gates dict
    (e.g. only emitted phase1), the ``set(verdicts) == {phase1, phase2,
    phase3}`` assertion would fail. The [0, 1] clamp invariant in
    ``compute_weighted_total`` is also load-bearing — any regression
    that produced weighted_total > 1 would break the ``<= 1.0`` check.
    """
    suite = _build_mini_suite()
    retrieve = _ScriptedRetrieve(
        {
            # recall-1: top hit is the gold title -> graded NDCG ~1.0.
            "kairix architecture overview": (
                ["notes/architecture-overview.md", "notes/performance-notes.md"],
                ["snippet a", "snippet b"],
                {"latency_ms": 5.0},
            ),
            # entity-1: gold_path in top-5 -> exact score 1.0.
            "who is openclaw": (
                ["entities/openclaw.md", "notes/other.md"],
                ["snippet", "snippet"],
                {"latency_ms": 3.0},
            ),
            # conceptual-1: gold_path in top-10 -> fuzzy score 1.0.
            "bm25 ranking how it works": (
                ["notes/bm25.md", "notes/other.md", "notes/extra.md"],
                ["snippet", "snippet", "snippet"],
                {"latency_ms": 4.0},
            ),
        }
    )
    deps = BenchmarkDeps(
        classifier=_AlwaysEntityClassifier(),
        chat_backend=FakeChatBackend(),  # never invoked — no llm-arm case.
        retrieve=retrieve,
    )

    result = run_benchmark(suite, system="mock", deps=deps)

    # Envelope shape.
    assert "weighted_total" in result.summary
    assert "category_scores" in result.summary
    assert "gates" in result.summary
    assert set(result.summary["gates"].keys()) == {"phase1", "phase2", "phase3"}
    # Invariant: weighted_total in [0, 1].
    wt = result.summary["weighted_total"]
    assert 0.0 <= wt <= 1.0, f"weighted_total {wt} broke the [0,1] invariant"
    # All 4 cases produced result rows.
    assert len(result.cases) == 4
    # Each case carries its score_method back out unchanged.
    methods = sorted(c["score_method"] for c in result.cases)
    assert methods == ["classification", "exact", "fuzzy", "ndcg"]


def test_benchmark_classification_arm_skips_retrieval() -> None:
    """Classification cases never call ``retrieve``. The runner's
    ``retrieve_case`` short-circuits and dispatches straight to the
    classifier. Critical for benchmark cost — a classification suite
    over an unindexed system must still produce results.

    Sabotage: if ``retrieve_case`` were changed to always call retrieve
    (regardless of score_method), the recorded calls list would
    contain the classification case's query.
    """
    suite = BenchmarkSuite(
        meta={"name": "classify-only", "version": "1.0"},
        cases=[
            BenchmarkCase(
                id="cls-1",
                category="classification",
                query="search for openclaw",
                gold_path=None,
                score_method="classification",
                expected_type="entity",
            ),
        ],
    )
    retrieve = _ScriptedRetrieve({})  # ANY recorded call here is a violation.
    deps = BenchmarkDeps(
        classifier=_AlwaysEntityClassifier(),
        chat_backend=FakeChatBackend(),
        retrieve=retrieve,
    )

    result = run_benchmark(suite, system="mock", deps=deps)

    assert retrieve.calls == [], "Classification cases must not reach retrieve"
    assert len(result.cases) == 1
    assert result.cases[0]["score"] == 1.0


def test_benchmark_category_scores_aggregate_by_category() -> None:
    """A suite with two recall cases (one perfect, one zero) produces a
    recall category average of 0.5. Same case-grouping math drives the
    weighted total, so this test pins the aggregation contract.

    Sabotage: if ``aggregate_scores_by_category`` returned ``sum``
    instead of ``avg``, the asserted category average ``0.5`` would
    be ``1.0``.
    """
    suite = BenchmarkSuite(
        meta={"name": "two-recall", "version": "1.0"},
        cases=[
            BenchmarkCase(
                id="r-hit",
                category="recall",
                query="hits the gold",
                gold_path=None,
                score_method="ndcg",
                gold_titles=[{"title": "Hit Doc", "relevance": 2}],
            ),
            BenchmarkCase(
                id="r-miss",
                category="recall",
                query="misses the gold",
                gold_path=None,
                score_method="ndcg",
                gold_titles=[{"title": "Missing Doc", "relevance": 2}],
            ),
        ],
    )
    retrieve = _ScriptedRetrieve(
        {
            "hits the gold": (["notes/hit-doc.md"], ["s"], {}),
            "misses the gold": (["notes/something-else.md"], ["s"], {}),
        }
    )
    deps = BenchmarkDeps(
        classifier=_AlwaysEntityClassifier(),
        chat_backend=FakeChatBackend(),
        retrieve=retrieve,
    )

    result = run_benchmark(suite, system="mock", deps=deps)

    recall_avg = result.summary["category_scores"]["recall"]
    assert recall_avg == pytest.approx(0.5), f"two recall cases (1.0 + 0.0) must average to 0.5; got {recall_avg}"


def test_benchmark_low_total_fails_phase3_gate() -> None:
    """A suite where every case scores zero (no matches) produces a
    weighted_total of 0.0 — well below the phase3 threshold (0.75).
    The gates dict reports phase3=False.

    Sabotage: if the gate-verdict logic flipped (``>=`` to ``<``), a
    zero-total run would report phase3=True, and this assertion
    would fire.
    """
    suite = BenchmarkSuite(
        meta={"name": "all-misses", "version": "1.0"},
        cases=[
            BenchmarkCase(
                id="r-miss",
                category="recall",
                query="q1",
                gold_path=None,
                score_method="ndcg",
                gold_titles=[{"title": "Real Gold", "relevance": 2}],
            ),
        ],
    )
    retrieve = _ScriptedRetrieve({"q1": (["notes/totally-different.md"], ["s"], {})})
    deps = BenchmarkDeps(
        classifier=_AlwaysEntityClassifier(),
        chat_backend=FakeChatBackend(),
        retrieve=retrieve,
    )

    result = run_benchmark(suite, system="mock", deps=deps)

    assert result.summary["weighted_total"] == pytest.approx(0.0)
    gates = result.summary["gates"]
    assert gates["phase1"] is False
    assert gates["phase2"] is False
    assert gates["phase3"] is False
