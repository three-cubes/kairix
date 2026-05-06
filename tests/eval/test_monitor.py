"""
Unit tests for kairix.quality.eval.monitor.

Benchmark runner is mocked — no live retrieval in CI.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kairix.quality.eval.monitor import (
    MonitorResult,
    _load_log,
    _rolling_average,
    generate_report,
    run_monitor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_log_entry(weighted_ndcg: float, days_ago: float = 0.0, regression: bool = False) -> dict:
    ts = (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        "ts": ts,
        # NOSONAR(python:S5443): suite_path is an opaque label string in the
        # monitor log JSON — never used as a real filesystem path in this test.
        "suite_path": "/tmp/canary.yaml",
        "n_cases": 10,
        "ndcg_by_category": {"recall": weighted_ndcg},
        "weighted_ndcg": weighted_ndcg,
        "vec_failed_count": 0,
        "regression": regression,
        "regression_detail": None,
    }


def _mock_benchmark_result(weighted: float, cat_scores: dict | None = None) -> MagicMock:
    """Build a mock BenchmarkResult with the given weighted total."""
    cat_scores = cat_scores or {
        "recall": weighted,
        "temporal": weighted,
        "entity": weighted,
        "conceptual": weighted,
        "multi_hop": weighted,
        "procedural": weighted,
    }
    mock = MagicMock()
    mock.summary = {"weighted_total": weighted, "category_scores": cat_scores}
    mock.cases = []
    return mock


# ---------------------------------------------------------------------------
# _rolling_average
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rolling_average_returns_none_when_empty() -> None:
    assert _rolling_average([], window_days=7) is None


@pytest.mark.unit
def test_rolling_average_computes_mean_within_window() -> None:
    entries = [
        _make_log_entry(0.8, days_ago=1),
        _make_log_entry(0.6, days_ago=2),
        _make_log_entry(0.7, days_ago=3),
    ]
    avg = _rolling_average(entries, window_days=7)
    assert avg == pytest.approx(0.7, abs=0.001)


@pytest.mark.unit
def test_rolling_average_excludes_entries_outside_window() -> None:
    entries = [
        _make_log_entry(0.9, days_ago=10),  # outside 7-day window
        _make_log_entry(0.5, days_ago=1),  # inside window
    ]
    avg = _rolling_average(entries, window_days=7)
    assert avg == pytest.approx(0.5, abs=0.001)


# ---------------------------------------------------------------------------
# run_monitor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_monitor_returns_result_with_correct_weighted_ndcg(tmp_path: Path) -> None:
    """run_monitor computes weighted_ndcg from benchmark result."""
    log_path = str(tmp_path / "monitor.jsonl")
    suite_path = str(tmp_path / "canary.yaml")

    # Create dummy suite file
    Path(suite_path).write_text("meta: {}\ncases: []\n", encoding="utf-8")

    mock_result = _mock_benchmark_result(
        weighted=0.75,
        cat_scores={
            "recall": 0.80,
            "temporal": 0.70,
            "entity": 0.75,
            "conceptual": 0.72,
            "multi_hop": 0.65,
            "procedural": 0.68,
        },
    )

    with (
        patch("kairix.quality.benchmark.suite.load_suite") as mock_load,
        patch("kairix.quality.benchmark.runner.run_benchmark", return_value=mock_result),
    ):
        mock_suite = MagicMock()
        mock_suite.cases = [MagicMock()] * 10
        mock_load.return_value = mock_suite

        result = run_monitor(
            suite_path=suite_path,
            log_path=log_path,
        )

    assert isinstance(result, MonitorResult)
    assert result.n_cases == 10
    assert result.weighted_ndcg > 0.0


@pytest.mark.unit
def test_run_monitor_detects_regression(tmp_path: Path) -> None:
    """run_monitor sets regression=True when ndcg drops >threshold vs baseline."""
    log_path = str(tmp_path / "monitor.jsonl")
    suite_path = str(tmp_path / "canary.yaml")

    # Seed log with high baseline (0.80)
    entries = [_make_log_entry(0.80, days_ago=i) for i in range(1, 4)]
    with open(log_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    mock_result = _mock_benchmark_result(0.60)  # 25% drop from 0.80

    with (
        patch("kairix.quality.benchmark.suite.load_suite") as mock_load,
        patch("kairix.quality.benchmark.runner.run_benchmark", return_value=mock_result),
    ):
        mock_suite = MagicMock()
        mock_suite.cases = [MagicMock()] * 5
        mock_load.return_value = mock_suite

        result = run_monitor(
            suite_path=suite_path,
            log_path=log_path,
            alert_threshold=0.05,
        )

    assert result.regression is True
    assert result.regression_detail is not None
    assert "dropped" in result.regression_detail.lower()


@pytest.mark.unit
def test_run_monitor_no_regression_within_threshold(tmp_path: Path) -> None:
    """run_monitor sets regression=False when ndcg is within threshold."""
    log_path = str(tmp_path / "monitor.jsonl")
    suite_path = str(tmp_path / "canary.yaml")

    # Seed log with baseline of 0.75
    entries = [_make_log_entry(0.75, days_ago=i) for i in range(1, 4)]
    with open(log_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    mock_result = _mock_benchmark_result(0.73)  # 2.7% drop — within 5% threshold

    with (
        patch("kairix.quality.benchmark.suite.load_suite") as mock_load,
        patch("kairix.quality.benchmark.runner.run_benchmark", return_value=mock_result),
    ):
        mock_suite = MagicMock()
        mock_suite.cases = [MagicMock()] * 5
        mock_load.return_value = mock_suite

        result = run_monitor(
            suite_path=suite_path,
            log_path=log_path,
            alert_threshold=0.05,
        )

    assert result.regression is False


@pytest.mark.unit
def test_run_monitor_no_regression_on_first_run(tmp_path: Path) -> None:
    """run_monitor returns regression=False on the first run (no baseline)."""
    log_path = str(tmp_path / "monitor.jsonl")
    suite_path = str(tmp_path / "canary.yaml")

    mock_result = _mock_benchmark_result(0.72)

    with (
        patch("kairix.quality.benchmark.suite.load_suite") as mock_load,
        patch("kairix.quality.benchmark.runner.run_benchmark", return_value=mock_result),
    ):
        mock_suite = MagicMock()
        mock_suite.cases = [MagicMock()] * 5
        mock_load.return_value = mock_suite

        result = run_monitor(suite_path=suite_path, log_path=log_path)

    assert result.regression is False


@pytest.mark.unit
def test_run_monitor_returns_false_on_benchmark_error(tmp_path: Path) -> None:
    """run_monitor returns regression=False on any internal error (never raises)."""
    log_path = str(tmp_path / "monitor.jsonl")
    suite_path = str(tmp_path / "canary.yaml")

    with patch(
        "kairix.quality.benchmark.suite.load_suite",
        side_effect=FileNotFoundError("suite not found"),
    ):
        result = run_monitor(suite_path=suite_path, log_path=log_path)

    assert isinstance(result, MonitorResult)
    assert result.regression is False


@pytest.mark.unit
def test_run_monitor_appends_to_log(tmp_path: Path) -> None:
    """run_monitor appends a new entry to the JSONL log on each run."""
    log_path = str(tmp_path / "monitor.jsonl")
    suite_path = str(tmp_path / "canary.yaml")

    mock_result = _mock_benchmark_result(0.72)

    for _ in range(3):
        with (
            patch("kairix.quality.benchmark.suite.load_suite") as mock_load,
            patch(
                "kairix.quality.benchmark.runner.run_benchmark",
                return_value=mock_result,
            ),
        ):
            mock_suite = MagicMock()
            mock_suite.cases = [MagicMock()] * 3
            mock_load.return_value = mock_suite
            run_monitor(suite_path=suite_path, log_path=log_path)

    entries = _load_log(log_path)
    assert len(entries) == 3
    assert all(e.get("weighted_ndcg") is not None for e in entries)


# ---------------------------------------------------------------------------
# generate_report
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generate_report_returns_markdown(tmp_path: Path) -> None:
    """generate_report returns a non-empty markdown string from log entries."""
    log_path = str(tmp_path / "monitor.jsonl")
    entries = [
        _make_log_entry(0.75, days_ago=1),
        _make_log_entry(0.72, days_ago=2),
    ]
    with open(log_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    report = generate_report(log_path=log_path, days=30)
    assert "# Kairix Monitor Report" in report
    assert "0.75" in report or "0.72" in report


@pytest.mark.unit
def test_generate_report_handles_empty_log(tmp_path: Path) -> None:
    """generate_report returns a polite message when log is empty."""
    log_path = str(tmp_path / "empty.jsonl")
    report = generate_report(log_path=log_path, days=30)
    assert "no" in report.lower() or "No" in report
