"""
Tests for kairix.quality.benchmark.dual_runner — verify the dual runner produces
baseline + comparison + deltas and detects regressions.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from kairix.paths import bundled_suites_root
from kairix.quality.benchmark.dual_runner import DualBenchmarkResult, run_dual_benchmark

# #450 — the suites moved under kairix/data/suites/ (package-data). Resolve
# through bundled_suites_root() so the path tracks the relocation instead of
# a brittle ``../../suites`` literal.
_SUITE_PATH = os.fspath(bundled_suites_root() / "reflib-contract-suite.yaml")

# Path to the original contract suite
_CONTRACT_SUITE_PATH = os.fspath(bundled_suites_root() / "contract-suite.yaml")


@pytest.mark.unit
class TestDualRunnerBaselineOnly:
    """Dual runner with no comparison_db produces baseline only."""

    def test_baseline_only_returns_result(self) -> None:
        result = run_dual_benchmark(
            suite_path=_SUITE_PATH,
            baseline_db=None,
            comparison_db=None,
            system="mock-reflib",
        )
        assert isinstance(result, DualBenchmarkResult)
        assert result.baseline is not None
        assert result.comparison is None
        assert result.deltas == {}
        assert result.regression_detected is False

    def test_baseline_has_scores(self) -> None:
        result = run_dual_benchmark(
            suite_path=_SUITE_PATH,
            baseline_db=None,
            comparison_db=None,
            system="mock-reflib",
        )
        wt = result.baseline.summary["weighted_total"]
        assert wt > 0.0, f"Weighted total should be > 0, got {wt}"

    def test_baseline_has_category_scores(self) -> None:
        result = run_dual_benchmark(
            suite_path=_SUITE_PATH,
            baseline_db=None,
            comparison_db=None,
            system="mock-reflib",
        )
        cats = result.baseline.summary["category_scores"]
        assert "recall" in cats
        assert "entity" in cats
        assert "conceptual" in cats
        assert "procedural" in cats


@pytest.mark.unit
class TestDualRunnerWithComparison:
    """Dual runner with both baseline and comparison backends."""

    def test_comparison_produces_deltas(self) -> None:
        """Run both mock and mock-reflib against the contract suite to get deltas."""
        # Use the same mock-reflib system for both — deltas should be zero
        result = run_dual_benchmark(
            suite_path=_SUITE_PATH,
            baseline_db=None,
            comparison_db="dummy",  # comparison_db is not None, triggering comparison run
            system="mock-reflib",
        )
        assert result.comparison is not None
        assert "weighted_total" in result.deltas

        # Same system, same suite => deltas should be 0
        assert result.deltas["weighted_total"] == pytest.approx(0.0)
        assert result.regression_detected is False

    def test_deltas_contain_all_categories(self) -> None:
        result = run_dual_benchmark(
            suite_path=_SUITE_PATH,
            baseline_db=None,
            comparison_db="dummy",
            system="mock-reflib",
        )
        for cat in ("recall", "entity", "conceptual", "procedural"):
            assert cat in result.deltas, f"Missing delta for {cat}"


@pytest.mark.unit
class TestDualRunnerRegressionDetection:
    """Verify regression detection logic."""

    def test_no_regression_when_same_system(self) -> None:
        result = run_dual_benchmark(
            suite_path=_SUITE_PATH,
            baseline_db=None,
            comparison_db="dummy",
            system="mock-reflib",
        )
        assert result.regression_detected is False

    def test_dataclass_fields(self) -> None:
        result = run_dual_benchmark(
            suite_path=_SUITE_PATH,
            baseline_db=None,
            comparison_db=None,
            system="mock-reflib",
        )
        # Verify DualBenchmarkResult has the expected fields
        assert hasattr(result, "baseline")
        assert hasattr(result, "comparison")
        assert hasattr(result, "deltas")
        assert hasattr(result, "regression_detected")


@pytest.mark.unit
def test_dual_benchmark_with_partial_run_is_inconclusive(tmp_path: Path) -> None:
    """When a run has judge failures, the dual comparison is inconclusive and
    never reports a regression, even though the deltas look like one.

    Sabotage proof: drop ``not inconclusive and`` from the
    ``regression_detected`` expression in ``run_dual_benchmark`` — the
    partial comparison (judged 0.0 vs a backend that failed later) flags a
    regression. Restored.
    """
    from kairix.quality.benchmark.dual_runner import run_dual_benchmark
    from kairix.quality.benchmark.runner import BenchmarkDeps
    from tests.fakes import FakeChatBackend

    suite_path = tmp_path / "dual.yaml"
    suite_path.write_text(
        "meta:\n  name: dual\n  version: '1.0'\ncases:\n"
        "  - id: T1\n    category: temporal\n    query: q1\n    score_method: llm\n"
        "  - id: T2\n    category: temporal\n    query: q2\n    score_method: llm\n",
        encoding="utf-8",
    )

    def _retrieve(**_kw: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        return ["vault/doc.md"], ["s"], {}

    # Baseline run judges both 1.0; the comparison run gets 0.0 then exhausts
    # the fake (judge failure) — a "drop" that must not count as a regression.
    backend = FakeChatBackend(responses=["1.0", "1.0", "0.0"])
    dual = run_dual_benchmark(
        str(suite_path),
        comparison_db=str(tmp_path / "cmp.db"),
        deps=BenchmarkDeps(chat_backend=backend, retrieve=_retrieve),
    )
    assert dual.inconclusive is True
    assert dual.regression_detected is False


def _single_llm_case_suite(tmp_path: Path) -> Path:
    suite_path = tmp_path / "single.yaml"
    suite_path.write_text(
        "meta:\n  name: single\n  version: '1.0'\ncases:\n"
        "  - id: T1\n    category: temporal\n    query: q1\n    score_method: llm\n",
        encoding="utf-8",
    )
    return suite_path


def _doc_retrieve(**_kw: Any) -> tuple[list[str], list[str], dict[str, Any]]:
    return ["vault/doc.md"], ["s"], {}


@pytest.mark.unit
def test_dual_benchmark_complete_runs_are_not_inconclusive(tmp_path: Path) -> None:
    """Fully judged runs (baseline-only and with a comparison) are never
    flagged inconclusive.

    Sabotage proof: initialise ``inconclusive = True`` in
    ``run_dual_benchmark`` — the baseline-only result reports inconclusive
    and the first assertion fails. Restored.
    """
    from kairix.quality.benchmark.runner import BenchmarkDeps
    from tests.fakes import FakeChatBackend

    suite_path = _single_llm_case_suite(tmp_path)
    baseline_only = run_dual_benchmark(
        str(suite_path),
        deps=BenchmarkDeps(chat_backend=FakeChatBackend(responses=["1.0"]), retrieve=_doc_retrieve),
    )
    assert baseline_only.inconclusive is False

    both = run_dual_benchmark(
        str(suite_path),
        comparison_db=str(tmp_path / "cmp.db"),
        deps=BenchmarkDeps(chat_backend=FakeChatBackend(responses=["1.0", "1.0"]), retrieve=_doc_retrieve),
    )
    assert both.inconclusive is False
    assert both.regression_detected is False


@pytest.mark.unit
def test_dual_benchmark_drop_exactly_at_threshold_is_not_a_regression(tmp_path: Path) -> None:
    """A weighted-total drop of exactly REGRESSION_THRESHOLD (0.02) is within
    tolerance; anything beyond it is a regression.

    The single temporal case carries weight 0.20, so judge scores 1.0 → 0.9
    move the weighted total by exactly -0.02, and 1.0 → 0.8 by -0.04.

    Sabotage proof: change ``< -REGRESSION_THRESHOLD`` to ``<=`` in
    ``run_dual_benchmark`` — the at-threshold drop is flagged and the first
    assertion fails. Restored.
    """
    from kairix.quality.benchmark.runner import BenchmarkDeps
    from tests.fakes import FakeChatBackend

    suite_path = _single_llm_case_suite(tmp_path)

    def _dual(replies: list[str]) -> Any:
        return run_dual_benchmark(
            str(suite_path),
            comparison_db=str(tmp_path / "cmp.db"),
            deps=BenchmarkDeps(chat_backend=FakeChatBackend(responses=replies), retrieve=_doc_retrieve),
        )

    at_threshold = _dual(["1.0", "0.9"])
    assert at_threshold.deltas["weighted_total"] == pytest.approx(-0.02)
    assert at_threshold.regression_detected is False

    beyond = _dual(["1.0", "0.8"])
    assert beyond.regression_detected is True
