"""
Unit tests for kairix.quality.eval.monitor.

Every test drives behaviour through the public surface — ``run_monitor`` and
``generate_report``. The benchmark runner and suite loader are injected via
the ``suite_loader=`` and ``benchmark_runner=`` kwargs (real callables, not
@patch). No private helpers are imported. Log-file content is observed by
reading the JSONL output and via the regression_detail string emitted by
``run_monitor`` (which embeds the rolling baseline).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kairix.quality.benchmark.runner import BenchmarkResult
from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite
from kairix.quality.eval.monitor import (
    MonitorResult,
    generate_report,
    run_monitor,
)

# ---------------------------------------------------------------------------
# Helpers — fake suite_loader / benchmark_runner factories
# ---------------------------------------------------------------------------


def _suite_loader_with_n_cases(n_cases: int) -> Callable[[str], BenchmarkSuite]:
    """Return a loader that yields a suite with ``n_cases`` recall cases."""

    def _loader(_path: str) -> BenchmarkSuite:
        return BenchmarkSuite(
            meta={"agent": "shape", "collections": ["vault"]},
            cases=[
                BenchmarkCase(
                    id=f"R{i:02d}",
                    category="recall",
                    query=f"q{i}",
                    gold_path=f"vault/doc{i}.md",
                    score_method="exact",
                )
                for i in range(n_cases)
            ],
        )

    return _loader


def _benchmark_runner_with_scores(
    weighted: float,
    cat_scores: dict[str, float] | None = None,
) -> Callable[..., BenchmarkResult]:
    """Return a runner whose result has the given category scores."""
    cat_scores = cat_scores or {
        "recall": weighted,
        "temporal": weighted,
        "entity": weighted,
        "conceptual": weighted,
        "multi_hop": weighted,
        "procedural": weighted,
    }

    def _runner(_suite: BenchmarkSuite, **_kwargs: Any) -> BenchmarkResult:
        return BenchmarkResult(
            meta={"system": "hybrid"},
            summary={"weighted_total": weighted, "category_scores": cat_scores},
            diagnostics={},
            cases=[],
        )

    return _runner


def _make_log_entry(weighted_ndcg: float, days_ago: float = 0.0, regression: bool = False) -> dict:
    ts = (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        "ts": ts,
        "suite_path": "/tmp/canary.yaml",  # opaque label — never read as a real path
        "n_cases": 10,
        "ndcg_by_category": {"recall": weighted_ndcg},
        "weighted_ndcg": weighted_ndcg,
        "vec_failed_count": 0,
        "regression": regression,
        "regression_detail": None,
    }


def _seed_log(log_path: Path, entries: list[dict]) -> None:
    log_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )


def _read_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# run_monitor — happy path + result shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_returns_monitor_result_with_n_cases_and_weighted_ndcg(tmp_path: Path) -> None:
    log_path = tmp_path / "monitor.jsonl"

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(log_path),
        suite_loader=_suite_loader_with_n_cases(10),
        benchmark_runner=_benchmark_runner_with_scores(
            0.75,
            {
                "recall": 0.80,
                "temporal": 0.70,
                "entity": 0.75,
                "conceptual": 0.72,
                "multi_hop": 0.65,
                "procedural": 0.68,
            },
        ),
    )

    assert isinstance(result, MonitorResult)
    assert result.n_cases == 10
    # weighted_ndcg is recomputed from category scores * CATEGORY_WEIGHTS — must be > 0.
    assert result.weighted_ndcg > 0.0
    # Per-category NDCG round-trips through the result.
    assert result.ndcg_by_category["recall"] == pytest.approx(0.80)
    assert result.ndcg_by_category["multi_hop"] == pytest.approx(0.65)


@pytest.mark.unit
def test_returns_empty_result_when_suite_has_zero_cases(tmp_path: Path) -> None:
    """A 0-case suite returns the all-zero MonitorResult sentinel without invoking the runner."""
    runner_calls: list[BenchmarkSuite] = []

    def _spy_runner(suite: BenchmarkSuite, **_kwargs: Any) -> BenchmarkResult:
        runner_calls.append(suite)
        raise AssertionError("runner must not run when suite has 0 cases")

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(tmp_path / "monitor.jsonl"),
        suite_loader=_suite_loader_with_n_cases(0),
        benchmark_runner=_spy_runner,
    )

    assert result.n_cases == 0
    assert result.weighted_ndcg == 0.0
    assert result.regression is False
    assert runner_calls == []


# ---------------------------------------------------------------------------
# Regression detection — drives _rolling_average through run_monitor's
# regression_detail string (which embeds the computed baseline).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_first_run_with_no_baseline_does_not_flag_regression(tmp_path: Path) -> None:
    """No previous log entries → no baseline → no regression."""
    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(tmp_path / "monitor.jsonl"),
        suite_loader=_suite_loader_with_n_cases(5),
        benchmark_runner=_benchmark_runner_with_scores(0.72),
    )
    assert result.regression is False
    assert result.regression_detail is None


@pytest.mark.unit
def test_baseline_average_is_computed_from_in_window_log_entries(tmp_path: Path) -> None:
    """Three entries (0.8/0.6/0.7) within the 7-day window average to 0.7;
    a current score of 0.6 is a 14.3% drop, above the 5% threshold → regression.
    The baseline value is embedded in regression_detail.
    """
    log_path = tmp_path / "monitor.jsonl"
    _seed_log(
        log_path,
        [
            _make_log_entry(0.8, days_ago=1),
            _make_log_entry(0.6, days_ago=2),
            _make_log_entry(0.7, days_ago=3),
        ],
    )

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(log_path),
        alert_threshold=0.05,
        window_days=7,
        suite_loader=_suite_loader_with_n_cases(5),
        benchmark_runner=_benchmark_runner_with_scores(0.6),
    )

    assert result.regression is True
    assert result.regression_detail is not None
    # Baseline average of [0.8, 0.6, 0.7] = 0.7 — verbatim in the detail string.
    assert "0.7000" in result.regression_detail


@pytest.mark.unit
def test_baseline_excludes_log_entries_outside_window(tmp_path: Path) -> None:
    """An entry 10 days old is excluded from the 7-day rolling average; only
    the 1-day-old entry contributes → baseline = 0.5. Current 0.4 → 20% drop,
    above threshold. detail names baseline 0.5000 (NOT 0.7000 from a 0.9+0.5 mean).
    """
    log_path = tmp_path / "monitor.jsonl"
    _seed_log(
        log_path,
        [
            _make_log_entry(0.9, days_ago=10),  # outside window
            _make_log_entry(0.5, days_ago=1),  # inside window
        ],
    )

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(log_path),
        alert_threshold=0.05,
        window_days=7,
        suite_loader=_suite_loader_with_n_cases(5),
        benchmark_runner=_benchmark_runner_with_scores(0.4),
    )

    assert result.regression is True
    assert result.regression_detail is not None
    assert "0.5000" in result.regression_detail
    # 0.9 was outside the window — baseline must NOT include it. Sabotage check:
    # if the window filter were dropped, baseline would be (0.9+0.5)/2 = 0.7.
    assert "0.7000" not in result.regression_detail


@pytest.mark.unit
def test_no_regression_when_drop_is_within_alert_threshold(tmp_path: Path) -> None:
    """A 2.7% drop (0.75 → 0.73) is below the 5% threshold → no regression."""
    log_path = tmp_path / "monitor.jsonl"
    _seed_log(log_path, [_make_log_entry(0.75, days_ago=i) for i in range(1, 4)])

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(log_path),
        alert_threshold=0.05,
        suite_loader=_suite_loader_with_n_cases(5),
        benchmark_runner=_benchmark_runner_with_scores(0.73),
    )
    assert result.regression is False
    assert result.regression_detail is None


@pytest.mark.unit
def test_no_regression_when_score_improves(tmp_path: Path) -> None:
    """Score increase above baseline never flags regression, even with tight threshold."""
    log_path = tmp_path / "monitor.jsonl"
    _seed_log(log_path, [_make_log_entry(0.6, days_ago=1)])

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(log_path),
        alert_threshold=0.01,
        suite_loader=_suite_loader_with_n_cases(5),
        benchmark_runner=_benchmark_runner_with_scores(0.9),
    )
    assert result.regression is False


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_returns_empty_result_when_suite_loader_raises(tmp_path: Path) -> None:
    """A FileNotFoundError from the loader is swallowed; result.regression=False."""

    def _raising_loader(_path: str) -> BenchmarkSuite:
        raise FileNotFoundError("suite not found")

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(tmp_path / "monitor.jsonl"),
        suite_loader=_raising_loader,
        benchmark_runner=_benchmark_runner_with_scores(0.5),  # never called
    )

    assert isinstance(result, MonitorResult)
    assert result.regression is False
    assert result.weighted_ndcg == 0.0


@pytest.mark.unit
def test_returns_empty_result_when_runner_raises(tmp_path: Path) -> None:
    """A RuntimeError from the benchmark runner is swallowed; no regression."""

    def _raising_runner(_suite: BenchmarkSuite, **_kwargs: Any) -> BenchmarkResult:
        raise RuntimeError("retrieval down")

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(tmp_path / "monitor.jsonl"),
        suite_loader=_suite_loader_with_n_cases(5),
        benchmark_runner=_raising_runner,
    )

    assert isinstance(result, MonitorResult)
    assert result.regression is False


# ---------------------------------------------------------------------------
# Log persistence — observed by reading the JSONL file directly.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_each_run_appends_an_entry_to_the_log(tmp_path: Path) -> None:
    log_path = tmp_path / "monitor.jsonl"

    for _ in range(3):
        run_monitor(
            suite_path=str(tmp_path / "canary.yaml"),
            log_path=str(log_path),
            suite_loader=_suite_loader_with_n_cases(3),
            benchmark_runner=_benchmark_runner_with_scores(0.72),
        )

    entries = _read_log(log_path)
    assert len(entries) == 3
    assert all(e.get("weighted_ndcg") is not None for e in entries)
    assert all(e["n_cases"] == 3 for e in entries)


@pytest.mark.unit
def test_log_entry_for_a_run_records_its_ts_n_cases_and_weighted_ndcg(tmp_path: Path) -> None:
    log_path = tmp_path / "monitor.jsonl"

    run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(log_path),
        suite_loader=_suite_loader_with_n_cases(7),
        benchmark_runner=_benchmark_runner_with_scores(
            0.8,
            {
                "recall": 1.0,
                "temporal": 1.0,
                "entity": 1.0,
                "conceptual": 1.0,
                "multi_hop": 1.0,
                "procedural": 1.0,
            },
        ),
    )

    entries = _read_log(log_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["n_cases"] == 7
    assert entry["weighted_ndcg"] > 0.0
    assert entry["ndcg_by_category"]["recall"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# generate_report — markdown surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generate_report_returns_markdown_with_run_data(tmp_path: Path) -> None:
    """Recent log entries surface as rows in the rendered markdown."""
    log_path = tmp_path / "monitor.jsonl"
    _seed_log(
        log_path,
        [
            _make_log_entry(0.75, days_ago=1),
            _make_log_entry(0.72, days_ago=2),
        ],
    )

    report = generate_report(log_path=str(log_path), days=30)
    assert "# Kairix Monitor Report" in report
    # Both entries' weighted scores appear somewhere in the rendered table.
    assert "0.75" in report or "0.72" in report
    # Run count is reported.
    assert "Runs:" in report


@pytest.mark.unit
def test_generate_report_handles_missing_log_file(tmp_path: Path) -> None:
    """A non-existent log path renders the empty-data fallback."""
    report = generate_report(log_path=str(tmp_path / "no-such-log.jsonl"), days=30)
    assert "# Kairix Monitor Report" in report
    assert "No monitor data" in report or "no" in report.lower()


@pytest.mark.unit
def test_generate_report_handles_log_with_only_old_entries(tmp_path: Path) -> None:
    """A log with all entries outside the requested window renders the no-recent-data fallback."""
    log_path = tmp_path / "monitor.jsonl"
    _seed_log(log_path, [_make_log_entry(0.7, days_ago=60)])

    report = generate_report(log_path=str(log_path), days=7)
    assert f"No data in the last {7} days" in report


@pytest.mark.unit
def test_generate_report_renders_regression_events_section(tmp_path: Path) -> None:
    """Entries with regression=True surface in a dedicated Regression Events section."""
    log_path = tmp_path / "monitor.jsonl"
    entries = [
        _make_log_entry(0.4, days_ago=2, regression=True),
        _make_log_entry(0.7, days_ago=1, regression=False),
    ]
    # First entry needs a regression_detail to render — patch directly here.
    entries[0]["regression_detail"] = "weighted_ndcg dropped 0.30 (-30%)"
    _seed_log(log_path, entries)

    report = generate_report(log_path=str(log_path), days=30)
    assert "Regression Events" in report
    assert "weighted_ndcg dropped" in report


# ---------------------------------------------------------------------------
# Log resilience — corrupted / partial entries are skipped, not propagated.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_corrupt_log_lines_are_skipped_when_loading_for_baseline(tmp_path: Path) -> None:
    """A log file with one corrupt line and one valid entry yields a baseline
    computed from the valid entry only — the corrupt line is silently dropped.
    """
    log_path = tmp_path / "monitor.jsonl"
    valid = json.dumps(_make_log_entry(0.8, days_ago=1))
    log_path.write_text(f"{{not valid json\n{valid}\n", encoding="utf-8")

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(log_path),
        alert_threshold=0.05,
        suite_loader=_suite_loader_with_n_cases(5),
        benchmark_runner=_benchmark_runner_with_scores(0.5),
    )

    # Baseline = 0.8 (from the one valid entry). 0.5 is a 37.5% drop → regression.
    assert result.regression is True
    assert result.regression_detail is not None
    assert "0.8000" in result.regression_detail


@pytest.mark.unit
def test_naive_timestamps_in_log_are_treated_as_utc_for_baseline_window(tmp_path: Path) -> None:
    """A log entry whose ``ts`` lacks timezone info is normalised to UTC before
    comparison against the window cutoff. Without that branch, a naive timestamp
    would either crash or be wrongly excluded.
    """
    log_path = tmp_path / "monitor.jsonl"
    naive_ts = (datetime.now(tz=timezone.utc) - timedelta(days=1)).replace(tzinfo=None).isoformat()
    entry = _make_log_entry(0.8)
    entry["ts"] = naive_ts  # remove the +00:00 suffix
    _seed_log(log_path, [entry])

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(log_path),
        alert_threshold=0.05,
        suite_loader=_suite_loader_with_n_cases(5),
        benchmark_runner=_benchmark_runner_with_scores(0.5),
    )
    # Baseline = 0.8 (the naive entry was correctly placed within the window).
    assert result.regression is True
    assert "0.8000" in (result.regression_detail or "")


@pytest.mark.unit
def test_run_monitor_writes_to_explicit_log_path(tmp_path: Path) -> None:
    """run_monitor writes to the explicit log_path argument and creates the parent dir."""
    log_path = tmp_path / "subdir" / "monitor.jsonl"

    run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(log_path),
        suite_loader=_suite_loader_with_n_cases(2),
        benchmark_runner=_benchmark_runner_with_scores(0.6),
    )

    assert log_path.exists(), "run_monitor must persist the log to the explicit path"
    entries = _read_log(log_path)
    assert len(entries) == 1


@pytest.mark.unit
def test_generate_report_normalises_naive_timestamps_in_log_entries(tmp_path: Path) -> None:
    """A log entry with a naive ts (no tz) is treated as UTC for the report's
    window filter. Without normalisation, a same-day naive timestamp could be
    rejected as outside the window.
    """
    log_path = tmp_path / "monitor.jsonl"
    naive_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=12)).replace(tzinfo=None).isoformat()
    entry = _make_log_entry(0.83)
    entry["ts"] = naive_ts
    _seed_log(log_path, [entry])

    report = generate_report(log_path=str(log_path), days=7)
    # The report rendered the entry — proves the naive ts was placed inside the window.
    assert "0.83" in report
    assert "Runs:** 1" in report or "Runs: 1" in report or " 1\n" in report


@pytest.mark.unit
def test_generate_report_skips_log_entries_with_missing_ts_field(tmp_path: Path) -> None:
    """An entry without a ``ts`` key raises KeyError inside the window filter; the
    filter catches it and skips that entry. The remaining valid entries still render.
    """
    log_path = tmp_path / "monitor.jsonl"
    valid_entry = _make_log_entry(0.71, days_ago=1)
    no_ts_entry = {"weighted_ndcg": 0.99}  # missing ts → KeyError → skipped
    _seed_log(log_path, [no_ts_entry, valid_entry])

    report = generate_report(log_path=str(log_path), days=30)
    assert "0.71" in report
    # The 0.99 entry was skipped — its score must NOT appear.
    assert "0.9900" not in report


@pytest.mark.unit
def test_log_entries_missing_ts_or_weighted_ndcg_are_excluded_from_baseline(tmp_path: Path) -> None:
    """An entry missing the ``weighted_ndcg`` key is skipped by the rolling average.
    Only complete entries contribute to the baseline.
    """
    log_path = tmp_path / "monitor.jsonl"
    entries = [
        # Missing weighted_ndcg → skipped silently.
        {"ts": (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()},
        _make_log_entry(0.8, days_ago=2),
    ]
    _seed_log(log_path, entries)

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(log_path),
        alert_threshold=0.05,
        suite_loader=_suite_loader_with_n_cases(5),
        benchmark_runner=_benchmark_runner_with_scores(0.5),
    )

    # Only the one complete entry (0.8) contributes. 0.5 is a 37.5% drop → regression.
    assert result.regression is True
    assert "0.8000" in (result.regression_detail or "")
