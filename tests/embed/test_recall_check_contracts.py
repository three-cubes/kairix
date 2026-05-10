"""Contract-first tests for kairix.core.embed.recall_check.

Read the docstrings, write what they claim, run against the live code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kairix.core.embed.recall_check import (
    DEGRADATION_THRESHOLD,
    RecallChecker,
    load_previous_score,
    run_recall_gate,
    save_recall_result,
)
from tests.fakes import FakeEmbedProvider, FakeVectorSearcher

# ---------------------------------------------------------------------------
# Contract: RecallChecker.check returns the documented result dict shape.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_result_has_score_passed_total_timestamp_detail_keys() -> None:
    """Docstring: returns ``{score, passed, total, timestamp, detail}``."""
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=FakeVectorSearcher())
    result = checker.check(recall_queries=[("R1", "q", "g")])
    assert {"score", "passed", "total", "timestamp", "detail"} <= set(result.keys())


# ---------------------------------------------------------------------------
# Contract: score is the fraction of non-skipped queries whose gold fragment
# appeared in the top-k results.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_score_equals_passed_over_total_when_no_queries_are_skipped() -> None:
    """Docstring: "score is the fraction of non-skipped queries whose gold
    fragment appeared in the top-k results"."""
    fake_searcher = FakeVectorSearcher(paths=["docs/match.md"])
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=fake_searcher)
    queries = [
        ("R1", "q1", "match"),
        ("R2", "q2", "no-match"),
        ("R3", "q3", "match"),
    ]
    result = checker.check(recall_queries=queries)
    # 2 hits / 3 total checked = 0.6667
    assert result["passed"] == 2
    assert result["total"] == 3
    assert result["score"] == pytest.approx(2 / 3, abs=1e-3)


@pytest.mark.unit
def test_check_score_is_zero_when_zero_queries_are_checked() -> None:
    """Edge case the docstring implies: ``passed/checked`` with ``checked=0``
    must not divide-by-zero. Returns 0.0.
    """

    class _AlwaysEmptyProvider:
        def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
            return []  # surfaces as None inside the embed step → all queries skipped

    checker = RecallChecker(embed_provider=_AlwaysEmptyProvider(), vector_searcher=FakeVectorSearcher())
    result = checker.check(recall_queries=[("R1", "q", "g")])
    assert result["score"] == pytest.approx(0.0)
    assert result["total"] == 0  # all skipped → checked is zero
    assert result["passed"] == 0


# ---------------------------------------------------------------------------
# Contract: total counts non-skipped queries only.
#
# Docstring: "score is the fraction of non-skipped queries"; the result["total"]
# field reports the denominator the score uses.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_total_reflects_non_skipped_queries_only() -> None:
    class _ConditionalProvider:
        def __init__(self) -> None:
            self.calls = 0

        def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
            self.calls += 1
            if self.calls == 2:
                return []  # skip the 2nd query
            return [[1.0, 0.0]]

    fake_searcher = FakeVectorSearcher(paths=["docs/match.md"])
    checker = RecallChecker(embed_provider=_ConditionalProvider(), vector_searcher=fake_searcher)
    queries = [
        ("R1", "q1", "match"),  # hit
        ("R2", "q2", "match"),  # SKIPPED — embed returned []
        ("R3", "q3", "match"),  # hit
    ]
    result = checker.check(recall_queries=queries)
    # 3 queries, 1 skipped → total=2 (denominator), passed=2 (both non-skipped hit).
    assert result["total"] == 2
    assert result["passed"] == 2
    # Score = 2/2 = 1.0 — the skipped query did not penalise.
    assert result["score"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Contract: gold-fragment matching is case-insensitive.
#
# The docstring doesn't say "case-insensitive" but the implementation does
# ``gold_fragment.lower() in f.lower()``. If the contract is "match the
# fragment as a substring of any returned path", case-insensitivity is the
# operator-friendly behaviour — operators don't case-match filenames.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_gold_fragment_matching_is_case_insensitive() -> None:
    fake_searcher = FakeVectorSearcher(paths=["DOCS/Builder/Patterns.md"])
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=fake_searcher)
    result = checker.check(recall_queries=[("R1", "q", "builder/patterns")])
    assert result["detail"][0]["hit"] is True
    assert result["passed"] == 1


# ---------------------------------------------------------------------------
# Contract: load_previous_score / save_recall_result round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_save_and_load_previous_score_round_trip(tmp_path: Path) -> None:
    """save_recall_result writes a JSON list of runs; load_previous_score
    reads back the most-recent score. Round-trip must be exact.
    """
    log = tmp_path / "log.json"
    save_recall_result({"score": 0.83, "passed": 5, "total": 6}, log)
    save_recall_result({"score": 0.91, "passed": 6, "total": 6}, log)

    assert load_previous_score(log) == pytest.approx(0.91)


@pytest.mark.unit
def test_save_recall_result_caps_log_at_90_runs(tmp_path: Path) -> None:
    """The hardcoded 90-run cap from kairix/embed/recall_check.py."""
    log = tmp_path / "log.json"
    log.write_text(json.dumps([{"score": 0.0}] * 90))
    save_recall_result({"score": 0.99}, log)

    runs = json.loads(log.read_text())
    assert len(runs) == 90
    assert runs[-1]["score"] == pytest.approx(0.99)


# ---------------------------------------------------------------------------
# Contract: run_recall_gate's degradation threshold.
#
# Docstring: "When the score has dropped more than DEGRADATION_THRESHOLD
# since the previous run the gate fails and alert_callback is invoked".
# DEGRADATION_THRESHOLD = 0.10 — a drop of exactly 0.10 should NOT trigger
# (must be strictly more than).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_recall_gate_passes_when_drop_equals_threshold_exactly(tmp_path: Path) -> None:
    """Boundary: drop of exactly DEGRADATION_THRESHOLD does NOT fail the gate.
    The docstring says "more than" — equal is not more than.
    """
    log = tmp_path / "log.json"
    log.write_text(json.dumps([{"score": 0.90}]))

    class _StaticChecker(RecallChecker):
        def check(self, **_kwargs: object) -> dict[str, object]:
            # 0.90 → 0.80 = drop of exactly 0.10 = DEGRADATION_THRESHOLD.
            return {"score": 0.80, "passed": 4, "total": 5, "timestamp": 0, "detail": []}

    captured: list[str] = []
    passed, _result = run_recall_gate(
        alert_callback=captured.append,
        checker=_StaticChecker(),
        log_path=log,
    )
    assert passed is True, (
        f"a drop of exactly DEGRADATION_THRESHOLD ({DEGRADATION_THRESHOLD}) "
        "should not fail the gate per the 'more than' wording"
    )
    assert captured == [], "alert_callback should not fire when drop equals threshold"


@pytest.mark.unit
def test_run_recall_gate_passes_when_first_run_has_no_previous_score(tmp_path: Path) -> None:
    """Docstring implies: degradation comparison only fires when a previous
    score exists. First run has no baseline → no alert, gate passes.
    """
    log = tmp_path / "log.json"  # does not exist

    class _StaticChecker(RecallChecker):
        def check(self, **_kwargs: object) -> dict[str, object]:
            return {"score": 0.10, "passed": 1, "total": 10, "timestamp": 0, "detail": []}

    captured: list[str] = []
    passed, _result = run_recall_gate(
        alert_callback=captured.append,
        checker=_StaticChecker(),
        log_path=log,
    )
    assert passed is True, "first run with no baseline cannot regress"
    assert captured == []


# ---------------------------------------------------------------------------
# Contract: load_previous_score is resilient to corrupt log files.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_previous_score_returns_none_for_corrupt_log_file(tmp_path: Path) -> None:
    log = tmp_path / "log.json"
    log.write_text("{not valid json")
    assert load_previous_score(log) is None


@pytest.mark.unit
def test_load_previous_score_returns_none_for_empty_runs_list(tmp_path: Path) -> None:
    log = tmp_path / "log.json"
    log.write_text("[]")
    assert load_previous_score(log) is None
