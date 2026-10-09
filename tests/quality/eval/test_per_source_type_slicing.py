"""ADR-028 §"Quality evaluation" #1 — per-source-type Recall@k slicing.

Asserts the BenchmarkResult.summary["per_source_type"] mapping is
populated with one row per source type the suite covers, and that the
per-type NDCG@10 / MRR@10 / Hit@10 values match a hand-computed gold
suite.

Sabotage proofs (each test carries one):

* test_per_source_type_keys_match_suite — mutate
  ``aggregate_per_source_type`` to always return ``{}``; the assert
  on ``per_source_type`` non-empty fails.
* test_per_source_type_ndcg_matches_hand_computed — mutate the runner
  to drop ``per_source_type`` from the summary; the lookup raises
  ``KeyError`` / the assert fails.
* test_per_source_type_human_format_includes_block — mutate
  ``_format_per_source_type_block`` to return ``[]``; the assert that
  the rendered text contains ``Per source type:`` fails.
"""

from __future__ import annotations

import pytest

from kairix.quality.benchmark.per_type_slicing import (
    aggregate_canary,
    aggregate_per_source_type,
)
from kairix.quality.benchmark.runner import (
    BenchmarkResult,
    format_interpretation,
)
from kairix.quality.benchmark.suite import BenchmarkCase

pytestmark = pytest.mark.unit


def _make_case(
    case_id: str,
    *,
    gold_title: str,
    source_type: str | None = None,
    canary: bool = False,
    canary_unit: str | None = None,
) -> BenchmarkCase:
    return BenchmarkCase(
        id=case_id,
        category="recall",
        query="test query",
        gold_path=None,
        score_method="ndcg",
        gold_titles=[{"title": gold_title, "relevance": 2}],
        source_type=source_type,
        canary=canary,
        canary_unit=canary_unit,
    )


def _make_case_result(case_id: str, score: float, rr: float = 0.5, hit_at_5: float = 1.0) -> dict:
    return {
        "id": case_id,
        "category": "recall",
        "query": "test query",
        "score_method": "ndcg",
        "score": score,
        "retrieved_paths": [],
        "elapsed_ms": 10.0,
        "rr": rr,
        "hit_at_5": hit_at_5,
    }


def test_per_source_type_keys_match_suite() -> None:
    """Every source-type tag declared on the suite appears in the slicing.

    Sabotage: mutate ``aggregate_per_source_type`` to ``return {}`` and
    re-run — the assert on ``"pptx" in slices`` raises.
    """
    cases = [
        _make_case("M1", gold_title="agent-loop-patterns.md"),
        _make_case("P1", gold_title="deck-1.pptx"),
        _make_case("X1", gold_title="rollup.xlsx"),
    ]
    case_results = [
        _make_case_result("M1", 0.85),
        _make_case_result("P1", 0.71),
        _make_case_result("X1", 0.62),
    ]
    slices = aggregate_per_source_type(cases, case_results)
    assert set(slices.keys()) == {"markdown", "pptx", "xlsx"}, slices


def test_per_source_type_ndcg_matches_hand_computed() -> None:
    """NDCG@10 per type equals the mean of the slice's case scores.

    Sabotage: drop the ``return summary`` line from
    ``aggregate_per_source_type`` and re-run — the lookup raises
    AttributeError on ``None``.
    """
    cases = [
        _make_case("M1", gold_title="note-1.md"),
        _make_case("M2", gold_title="note-2.md"),
        _make_case("P1", gold_title="deck-1.pptx"),
    ]
    case_results = [
        _make_case_result("M1", 0.90, rr=1.0),
        _make_case_result("M2", 0.70, rr=0.5),
        _make_case_result("P1", 0.60, rr=0.33),
    ]
    slices = aggregate_per_source_type(cases, case_results)
    # Markdown: mean of 0.90 + 0.70 = 0.80
    assert slices["markdown"]["ndcg_at_10"] == pytest.approx(0.80, abs=1e-3)
    assert slices["markdown"]["n"] == 2.0
    # MRR: mean of 1.0 + 0.5 = 0.75
    assert slices["markdown"]["mrr_at_10"] == pytest.approx(0.75, abs=1e-3)
    # PPTX: single row
    assert slices["pptx"]["ndcg_at_10"] == pytest.approx(0.60, abs=1e-3)
    assert slices["pptx"]["n"] == 1.0


def test_per_source_type_human_format_includes_block() -> None:
    """``format_interpretation`` emits the per-source-type block when present.

    Sabotage: change ``_format_per_source_type_block`` to ``return []``
    and re-run — the assert on the ``Per source type:`` substring fails.
    """
    result = BenchmarkResult(
        meta={
            "suite_name": "test",
            "system": "mock",
            "agent": None,
            "collection": None,
            "fusion_override": None,
            "date": "2026-05-30",
            "n_cases": 0,
            "weighted_total": 0.0,
            "mode": None,
        },
        summary={
            "weighted_total": 0.80,
            "category_scores": {"recall": 0.80},
            "gates": {"phase1": True, "phase2": True, "phase3": True},
            "ndcg_at_10": 0.80,
            "hit_rate_at_5": 0.95,
            "mrr_at_10": 0.75,
            "per_source_type": {
                "markdown": {"ndcg_at_10": 0.85, "mrr_at_10": 0.83, "hit_at_10": 0.92, "n": 42.0},
                "pptx": {"ndcg_at_10": 0.71, "mrr_at_10": 0.68, "hit_at_10": 0.81, "n": 18.0},
            },
            "canary": {"overall": {"passed": 0.0, "total": 0.0, "rate": 0.0}, "by_unit": {}},
        },
        diagnostics={"category_counts": {"recall": 60}},
        cases=[],
    )
    text = format_interpretation(result)
    assert "Per source type:" in text
    assert "markdown" in text and "pptx" in text
    assert "NDCG@10=0.850" in text or "NDCG@10=0.85" in text


def test_per_source_type_extension_fallback_when_no_explicit_tag() -> None:
    """Source-type is derived from gold-title extension when ``source_type`` is unset.

    Sabotage: remove the ``_EXTENSION_TO_TYPE`` lookup from
    ``_derive_source_type`` — every case lands under "unknown".
    """
    cases = [
        _make_case("E1", gold_title="message-2026-05-04.eml"),
        _make_case("E2", gold_title="meeting.ics"),
    ]
    case_results = [
        _make_case_result("E1", 0.80),
        _make_case_result("E2", 0.75),
    ]
    slices = aggregate_per_source_type(cases, case_results)
    assert "email" in slices
    assert "calendar" in slices
    assert slices["email"]["n"] == 1.0


def test_per_source_type_explicit_tag_wins_over_extension() -> None:
    """``case.source_type`` overrides the extension-derived fallback.

    Sabotage: remove the early ``if explicit: return`` in
    ``_derive_source_type`` — the .md extension wins instead.
    """
    case = _make_case("T1", gold_title="hybrid.md", source_type="email")
    slices = aggregate_per_source_type([case], [_make_case_result("T1", 0.5)])
    assert set(slices.keys()) == {"email"}


def test_canary_summary_overall_and_by_unit() -> None:
    """Canary summary projects passed/total/rate per atomic unit.

    Sabotage: change the pass threshold in ``aggregate_canary`` from
    ≥0.5 to ≥1.0 — every canary scores below it and ``rate`` collapses
    to 0.0.
    """
    cases = [
        _make_case("CS1", gold_title="deck.pptx", canary=True, canary_unit="slide"),
        _make_case("CS2", gold_title="deck.pptx", canary=True, canary_unit="slide"),
        _make_case("CS3", gold_title="deck.pptx", canary=True, canary_unit="slide"),
        _make_case("CR1", gold_title="rollup.xlsx", canary=True, canary_unit="row"),
        _make_case("CR2", gold_title="rollup.xlsx", canary=True, canary_unit="row"),
    ]
    case_results = [
        _make_case_result("CS1", 0.9),
        _make_case_result("CS2", 0.7),
        _make_case_result("CS3", 0.6),
        _make_case_result("CR1", 0.55),
        _make_case_result("CR2", 0.3),  # below threshold — fails
    ]
    summary = aggregate_canary(cases, case_results)
    assert summary["overall"]["total"] == 5.0
    assert summary["overall"]["passed"] == 4.0
    assert summary["overall"]["rate"] == pytest.approx(0.80, abs=1e-3)
    assert summary["by_unit"]["slide"]["passed"] == 3.0
    assert summary["by_unit"]["slide"]["total"] == 3.0
    assert summary["by_unit"]["row"]["passed"] == 1.0
    assert summary["by_unit"]["row"]["total"] == 2.0


def test_canary_summary_skips_unscored_judge_failures() -> None:
    """An unscored canary (score None after an LLM-judge failure) is neither
    passed nor failed — it drops out of the totals instead of crashing.

    Sabotage: remove the ``score is None`` skip in ``aggregate_canary`` —
    ``float(None)`` raises TypeError and the test fails. Restored.
    """
    cases = [
        _make_case("CS1", gold_title="deck.pptx", canary=True, canary_unit="slide"),
        _make_case("CS2", gold_title="deck.pptx", canary=True, canary_unit="slide"),
    ]
    unscored = _make_case_result("CS2", 0.0)
    unscored["score"] = None
    summary = aggregate_canary(cases, [_make_case_result("CS1", 0.9), unscored])
    assert summary["overall"]["total"] == 1.0
    assert summary["overall"]["passed"] == 1.0


def test_canary_summary_empty_when_no_canaries() -> None:
    """No canary-flagged cases → overall total = 0; no unit rows.

    Sabotage: change ``aggregate_canary`` to always return ``{"overall":
    {"total": 1.0, ...}}`` — the assert that total is 0.0 fails.
    """
    cases = [_make_case("R1", gold_title="note.md")]  # no canary flag
    case_results = [_make_case_result("R1", 0.9)]
    summary = aggregate_canary(cases, case_results)
    assert summary["overall"]["total"] == 0.0
    assert summary["by_unit"] == {}


def test_aggregate_raises_on_mismatched_inputs() -> None:
    """Sequences of different lengths raise an actionable error.

    Sabotage: drop the ``if len(...) != len(...)`` guard — the inner
    ``zip`` silently truncates and the test loses its safety net.
    """
    with pytest.raises(ValueError, match="index-aligned"):
        aggregate_per_source_type([_make_case("X", gold_title="x.md")], [])
    with pytest.raises(ValueError, match="index-aligned"):
        aggregate_canary([_make_case("X", gold_title="x.md")], [])
