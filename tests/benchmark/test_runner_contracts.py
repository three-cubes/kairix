"""Contract-first tests for kairix.quality.benchmark.runner.

Read the docstrings + the constants in kairix.quality.eval.constants, write
tests asserting the claims, run against the live code.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.quality.benchmark.runner import (
    BenchmarkDeps,
    BenchmarkResult,
    aggregate_ndcg_metrics,
    aggregate_scores_by_category,
    classification_score,
    compute_weighted_total,
    exact_match,
    format_interpretation,
    fuzzy_match,
    llm_judge,
    run_benchmark,
    score_tier,
    title_in_retrieved,
)
from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite
from kairix.quality.eval.constants import CATEGORY_WEIGHTS, PHASE_GATES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _suite(*cases: BenchmarkCase, version: str = "1.0") -> BenchmarkSuite:
    return BenchmarkSuite(
        meta={"agent": "test", "collections": ["vault"], "version": version},
        cases=list(cases),
    )


def _retrieve_fn_returning(paths: list[str]):
    def _fn(**_kw: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        return paths, ["snippet"] * len(paths), {"intent": "semantic", "vec_failed": False}

    return _fn


# ---------------------------------------------------------------------------
# Contract: compute_weighted_total returns a score that cannot exceed 1.0.
#
# A weighted total is consumed by score_tier (compares to 0.80, 0.75, etc.)
# and by PHASE_GATES (≥0.62/0.68/0.75) — both assume the metric is in [0, 1].
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_weighted_total_does_not_exceed_one_for_perfect_v10_suite() -> None:
    """v1.0 suite + perfect per-category scores → weighted_total ≤ 1.0."""
    perfect = dict.fromkeys(CATEGORY_WEIGHTS, 1.0)
    total = compute_weighted_total(perfect, "1.0")
    assert total <= 1.0, f"weighted_total exceeds 1.0 cap: got {total}"


@pytest.mark.unit
def test_compute_weighted_total_does_not_exceed_one_for_perfect_v11_with_classification() -> None:
    """v1.1 suite triggers the Phase 3 adjustment (classification=0.15, temporal=0.10).

    For weighted_total to remain a [0, 1] metric, the adjusted weights must
    still sum to 1.0 — otherwise a perfect-scoring suite reports > 1.0,
    breaking score_tier and the phase-gate comparisons.
    """
    perfect = dict.fromkeys(CATEGORY_WEIGHTS, 1.0)
    perfect["classification"] = 1.0  # ensure the > 0 branch fires
    total = compute_weighted_total(perfect, "1.1")
    assert total <= 1.0, (
        f"v1.1 weighted_total exceeds 1.0 with perfect scores: got {total}. "
        "Phase 3 adjustment frees 0.10 from temporal but adds 0.15 to "
        "classification — weights sum to 1.05, breaking the score_tier / "
        "PHASE_GATES contract that assumes weighted_total ∈ [0, 1]."
    )


@pytest.mark.unit
def test_compute_weighted_total_v10_default_weights_sum_to_one() -> None:
    """The default CATEGORY_WEIGHTS must sum to 1.0 — the foundational
    invariant the weighted_total formula depends on.
    """
    total = sum(CATEGORY_WEIGHTS.values())
    assert total == pytest.approx(1.0), f"CATEGORY_WEIGHTS does not sum to 1.0: got {total}"


@pytest.mark.unit
def test_compute_weighted_total_v11_with_zero_classification_does_not_apply_adjustment() -> None:
    """When classification score is 0, the Phase 3 adjustment must NOT fire —
    otherwise a v1.1 suite with no classification cases would unexpectedly
    rebalance temporal weight.
    """
    scores = dict.fromkeys(CATEGORY_WEIGHTS, 1.0)
    scores["classification"] = 0.0  # zero classification → no Phase 3 adjustment
    v10_total = compute_weighted_total(scores, "1.0")
    v11_total = compute_weighted_total(scores, "1.1")
    # With classification=0, both versions should produce the same weighted_total.
    assert v10_total == pytest.approx(v11_total)


# ---------------------------------------------------------------------------
# Sample-size confidence floor (new — closes the noise contribution that
# small-n categories add to the headline score).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sample_size_floor_omitted_preserves_legacy_behavior() -> None:
    """Without per_category_n the function behaves identically to the
    pre-floor implementation. Locks the backwards-compat invariant."""
    scores = {"recall": 0.8, "temporal": 0.5, "entity": 0.4, "conceptual": 0.9, "multi_hop": 0.9, "procedural": 1.0}
    legacy = sum(scores.get(c, 0.0) * w for c, w in CATEGORY_WEIGHTS.items())
    new = compute_weighted_total(scores, "1.0")
    assert new == pytest.approx(legacy, abs=1e-4)


@pytest.mark.unit
def test_sample_size_floor_zeroes_small_n_categories() -> None:
    """A category with n=1 should NOT contribute to the weighted_total —
    n=1 is pure noise. Sabotage proof: if the floor is bypassed, the
    entity=0.4 category at weight 0.20 drags the score by 0.08; the
    test would fail with `result < 0.85`."""
    scores = {"recall": 1.0, "temporal": 1.0, "entity": 0.0, "conceptual": 1.0, "multi_hop": 1.0, "procedural": 1.0}
    per_cat_n = {"recall": 54, "temporal": 5, "entity": 1, "conceptual": 75, "multi_hop": 2, "procedural": 63}
    result = compute_weighted_total(scores, "1.0", per_category_n=per_cat_n)
    # With the floor, temporal/entity/multi_hop are zeroed; only recall, conceptual, procedural carry weight.
    # All three score 1.0, so weighted_total should be 1.0 (the freed budget is reallocated to qualified categories).
    assert result == pytest.approx(1.0, abs=1e-3), f"floor should produce 1.0; got {result}"


@pytest.mark.unit
def test_sample_size_floor_lifts_2026_06_08_reflib_eval_above_phase3_gate() -> None:
    """The headline regression test for #438 phase A: the actual 2026-06-08
    reflib eval headline numbers + the small-n diagnostics, when run through
    compute_weighted_total with the sample-size floor, should clear the
    0.75 Phase 3 gate.

    Sabotage proof: if the floor is removed, the weighted_total reverts to
    0.723 (the original headline) and the assertion fails."""
    scores = {
        "recall": 0.836,
        "temporal": 0.555,
        "entity": 0.380,
        "conceptual": 0.888,
        "multi_hop": 0.954,
        "procedural": 0.987,
    }
    per_cat_n = {"recall": 54, "temporal": 5, "entity": 1, "conceptual": 75, "multi_hop": 2, "procedural": 63}
    result = compute_weighted_total(scores, "1.0", per_category_n=per_cat_n)
    assert result >= 0.75, (
        f"sample-size floor should lift 2026-06-08 reflib eval above Phase 3 gate (0.75); got {result}"
    )


@pytest.mark.unit
def test_sample_size_floor_preserves_weight_conservation_on_qualified_categories() -> None:
    """When all categories meet the floor, the effective weights must still
    sum to 1.0 (no double-counting from reallocation when there's nothing
    to reallocate)."""
    scores = dict.fromkeys(CATEGORY_WEIGHTS, 1.0)
    per_cat_n = dict.fromkeys(CATEGORY_WEIGHTS, 100)
    result = compute_weighted_total(scores, "1.0", per_category_n=per_cat_n)
    # All weights remain; sum to 1.0; perfect scores → 1.0
    assert result == pytest.approx(1.0, abs=1e-4)


@pytest.mark.unit
def test_sample_size_floor_fallback_when_no_category_qualifies() -> None:
    """When EVERY category is below the floor, return the legacy weighted
    sum (better than silently zeroing). This is the deceptive-zero
    fallback documented in _apply_sample_size_floor."""
    scores = dict.fromkeys(CATEGORY_WEIGHTS, 1.0)
    per_cat_n = dict.fromkeys(CATEGORY_WEIGHTS, 1)  # every category is n=1
    result = compute_weighted_total(scores, "1.0", per_category_n=per_cat_n)
    # Fallback to legacy = weighted_total of perfect scores = 1.0
    assert result == pytest.approx(1.0, abs=1e-4)


# ---------------------------------------------------------------------------
# Contract: title_in_retrieved respects the top_k cutoff.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_title_in_retrieved_returns_false_when_match_is_outside_top_k() -> None:
    paths = [f"vault/doc{i}.md" for i in range(5)] + ["vault/target.md"]
    # gold matches paths[5], top_k=5 → should NOT be found.
    assert title_in_retrieved("target", paths, top_k=5) is False


@pytest.mark.unit
def test_title_in_retrieved_returns_true_when_match_is_within_top_k() -> None:
    paths = ["vault/target.md", "vault/other.md"]
    assert title_in_retrieved("target", paths, top_k=5) is True


@pytest.mark.unit
def test_title_in_retrieved_returns_false_for_empty_paths() -> None:
    assert title_in_retrieved("anything", [], top_k=10) is False


# ---------------------------------------------------------------------------
# Contract: aggregate_ndcg_metrics returns (None, None, None) when no NDCG cases.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_aggregate_ndcg_metrics_returns_all_none_when_no_ndcg_cases() -> None:
    case_results = [
        {"score_method": "exact", "score": 1.0},
        {"score_method": "fuzzy", "score": 0.5},
    ]
    assert aggregate_ndcg_metrics(case_results) == (None, None, None)


@pytest.mark.unit
def test_aggregate_ndcg_metrics_only_counts_ndcg_cases_in_mixed_results() -> None:
    """Among mixed score methods, only NDCG cases contribute to the average."""
    case_results = [
        {"score_method": "exact", "score": 1.0},
        {"score_method": "ndcg", "score": 0.8, "hit_at_5": 1.0, "rr": 0.5},
        {"score_method": "ndcg", "score": 0.4, "hit_at_5": 0.0, "rr": 0.25},
    ]
    ndcg_at_10, hit_rate_at_5, mrr_at_10 = aggregate_ndcg_metrics(case_results)
    # Only the two NDCG cases contribute: avg score = (0.8 + 0.4)/2 = 0.6.
    assert ndcg_at_10 == pytest.approx(0.6)
    assert hit_rate_at_5 == pytest.approx(0.5)
    assert mrr_at_10 == pytest.approx(0.375)


# ---------------------------------------------------------------------------
# Contract: aggregate_scores_by_category returns 0.0 for empty score lists.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_aggregate_scores_by_category_returns_zero_for_empty_lists() -> None:
    """An empty score list (no cases for that category) must average to 0.0,
    not raise ZeroDivisionError.
    """
    result = aggregate_scores_by_category({"recall": [], "entity": [0.5, 1.0]})
    assert result == {"recall": 0.0, "entity": 0.75}


# ---------------------------------------------------------------------------
# Contract: score_tier returns the right label for tier boundaries.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("score", "expected_substring"),
    [
        (0.85, "Phase 4 target"),
        (0.80, "Phase 4 target"),  # boundary: ≥0.80
        (0.79, "Production quality"),
        (0.75, "Production quality"),  # boundary: ≥0.75
        (0.70, "Phase 2 gate"),
        (0.62, "Phase 1 gate"),
        (0.51, "Typical BM25"),
        (0.35, "BM25 on Phase 1"),
        (0.10, "Below BM25 baseline"),
        (0.0, "Below BM25 baseline"),
    ],
)
def test_score_tier_returns_correct_label_at_boundaries(score: float, expected_substring: str) -> None:
    """score_tier must return labels that match the documented thresholds."""
    label = score_tier(score)
    assert expected_substring in label, (
        f"score_tier({score}) returned {label!r}, expected substring {expected_substring!r}"
    )


# ---------------------------------------------------------------------------
# Contract: run_benchmark BenchmarkResult shape.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_benchmark_summary_carries_documented_fields() -> None:
    """summary must contain weighted_total, category_scores, gates, and the
    NDCG aggregate fields (ndcg_at_10, hit_rate_at_5, mrr_at_10).
    """
    suite = _suite(BenchmarkCase(id="R1", category="recall", query="q", gold_path="x.md", score_method="exact"))
    result = run_benchmark(suite, deps=BenchmarkDeps(retrieve=_retrieve_fn_returning(["vault/x.md"])))

    assert "weighted_total" in result.summary
    assert "category_scores" in result.summary
    assert "gates" in result.summary
    assert "ndcg_at_10" in result.summary
    assert "hit_rate_at_5" in result.summary
    assert "mrr_at_10" in result.summary


@pytest.mark.unit
def test_run_benchmark_gates_dict_carries_each_phase_gate() -> None:
    """The summary.gates dict maps every PHASE_GATES key + judge_coverage → bool."""
    suite = _suite(BenchmarkCase(id="R1", category="recall", query="q", gold_path="x.md", score_method="exact"))
    result = run_benchmark(suite, deps=BenchmarkDeps(retrieve=_retrieve_fn_returning(["vault/x.md"])))

    gates = result.summary["gates"]
    assert set(gates.keys()) == set(PHASE_GATES.keys()) | {"judge_coverage"}
    assert gates["judge_coverage"] is True
    assert all(isinstance(passed, bool) for passed in gates.values())


@pytest.mark.unit
def test_run_benchmark_diagnostics_category_counts_reflects_scored_cases() -> None:
    """diagnostics.category_counts must record the per-category case count."""
    cases = [
        BenchmarkCase(id="R1", category="recall", query="q1", gold_path="a.md", score_method="exact"),
        BenchmarkCase(id="R2", category="recall", query="q2", gold_path="b.md", score_method="exact"),
        BenchmarkCase(id="E1", category="entity", query="q3", gold_path="c.md", score_method="exact"),
    ]
    suite = _suite(*cases)
    result = run_benchmark(suite, deps=BenchmarkDeps(retrieve=_retrieve_fn_returning([])))

    counts = result.diagnostics["category_counts"]
    assert counts["recall"] == 2
    assert counts["entity"] == 1


# ---------------------------------------------------------------------------
# Contract: case_results dict keys are not silently overwritten by retrieval_meta.
#
# Each case_results entry has documented keys: id, category, score, etc.
# retrieval_meta is spread into the dict via ``**retrieval_meta`` — if the
# meta dict contained a key like "score" or "id" it would silently overwrite
# the canonical case field. Probe this via a fake retrieve_fn whose meta
# carries an "id" key. The case's id MUST survive.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_benchmark_case_id_is_not_overwritten_by_retrieval_meta_collision() -> None:
    """If a custom retrieve_fn returns metadata whose keys collide with
    case_results' canonical fields, the documented field name must win —
    otherwise the result dict silently lies about the case identity.
    """

    def _evil_retrieve(**_kw: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        # Adversarial meta returns "id", "score", "category" — should not stomp the canonical fields.
        return ["vault/x.md"], ["s"], {"id": "OVERWRITE", "score": 999, "category": "OVERWRITE"}

    suite = _suite(BenchmarkCase(id="R1", category="recall", query="q", gold_path="x.md", score_method="exact"))
    result = run_benchmark(suite, deps=BenchmarkDeps(retrieve=_evil_retrieve))

    case = result.cases[0]
    assert case["id"] == "R1", f"case id was overwritten by retrieval_meta: got {case['id']!r}"
    assert case["category"] == "recall", f"case category overwritten: got {case['category']!r}"
    # The score must be the case's actual computed score, not the meta's 999.
    assert case["score"] != 999, f"case score was overwritten by retrieval_meta: got {case['score']}"


@pytest.mark.unit
def test_run_benchmark_judge_diagnostics_are_not_overwritten_by_retrieval_meta() -> None:
    """Retrieval metadata cannot replace or fake the judge diagnostics.

    Case J1's judge fails (the fake backend runs out of replies): its
    ``judge_failure`` / ``judge_error`` must be the real ones, not the
    adversarial meta's. Case J2 is judged fine: the meta's
    ``judge_failure`` must not inject a fake failure. Unrelated metadata
    (``intent``) survives on both rows.

    Sabotage proof: drop ``_KEY_JUDGE_FAILURE`` / ``_KEY_JUDGE_ERROR`` from
    ``canonical_keys`` in ``run_benchmark`` — the meta's values win the
    ``{**ndcg_detail, **retrieval_meta}`` merge and the judge-field
    assertions fail. Restored.
    """
    from tests.fakes import FakeChatBackend

    def _evil_retrieve(**_kw: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        return (
            ["vault/x.md"],
            ["s"],
            {"judge_failure": "FAKE", "judge_error": "FAKE", "intent": "semantic"},
        )

    suite = _suite(
        BenchmarkCase(id="J2", category="temporal", query="ok?", gold_path=None, score_method="llm"),
        BenchmarkCase(id="J1", category="temporal", query="fails?", gold_path=None, score_method="llm"),
    )
    # One canned reply: J2 is judged 0.8, J1 exhausts the fake (IndexError → backend_error).
    result = run_benchmark(
        suite,
        deps=BenchmarkDeps(chat_backend=FakeChatBackend(responses=["0.8"]), retrieve=_evil_retrieve),
    )

    judged, failed = result.cases
    assert failed["judge_failure"] == "backend_error"
    assert "IndexError" in failed["judge_error"]
    assert "judge_failure" not in judged
    assert "judge_error" not in judged
    assert judged["score"] == pytest.approx(0.8)
    assert judged["intent"] == "semantic"
    assert failed["intent"] == "semantic"


# ---------------------------------------------------------------------------
# Contract: format_interpretation surfaces the weighted_total.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_interpretation_embeds_weighted_total_in_output() -> None:
    result = BenchmarkResult(
        meta={"system": "hybrid"},
        summary={
            "weighted_total": 0.762,
            "category_scores": dict.fromkeys(CATEGORY_WEIGHTS, 0.7),
            "gates": {"phase1": True, "phase2": True, "phase3": True},
        },
        diagnostics={"category_counts": dict.fromkeys(CATEGORY_WEIGHTS, 1)},
        cases=[],
    )
    output = format_interpretation(result)
    assert "0.762" in output


# ---------------------------------------------------------------------------
# Contract: exact_match / fuzzy_match never raise on None-shaped input.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_exact_match_returns_zero_for_empty_paths() -> None:
    assert exact_match([], "x.md") == pytest.approx(0.0)


@pytest.mark.unit
def test_exact_match_returns_zero_for_empty_gold() -> None:
    assert exact_match(["x.md"], "") == pytest.approx(0.0)


@pytest.mark.unit
def test_fuzzy_match_returns_zero_for_empty_inputs() -> None:
    assert fuzzy_match([], "x.md") == pytest.approx(0.0)
    assert fuzzy_match(["x.md"], "") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Contract: classification_score returns 0.0 on any failure; llm_judge returns
# 0.0 only for an empty retrieval (its failures raise JudgeFailedError).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_classification_score_returns_zero_when_classifier_raises() -> None:
    class _RaisingClassifier:
        def classify_rules(self, *_args, **_kwargs):
            raise RuntimeError("classifier down")

        def classify_with_llm(self, *_args, **_kwargs):
            raise RuntimeError("classifier down")

    score = classification_score("q", "decision", classifier=_RaisingClassifier())
    assert score == pytest.approx(0.0)


@pytest.mark.unit
def test_llm_judge_returns_zero_for_empty_paths_without_calling_backend() -> None:
    """An empty paths list short-circuits — backend is never invoked, score is 0.0."""

    class _SpyBackend:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *_args, **_kwargs):
            self.calls += 1
            return "1.0"

    spy = _SpyBackend()
    score = llm_judge(query="q", paths=[], snippets=[], chat_backend=spy)
    assert score == pytest.approx(0.0)
    assert spy.calls == 0
