"""Step definitions for eval_monitor.feature.

Drives ``run_monitor`` through its public surface — injected suite loader
and benchmark runner via the ``suite_loader=`` / ``benchmark_runner=`` kwargs.
No @patch on ``kairix.quality.benchmark.*``; the real run_monitor logic is
exercised end-to-end against fakes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.quality.benchmark.runner import BenchmarkResult
from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite
from kairix.quality.eval.monitor import run_monitor

pytestmark = pytest.mark.bdd


# ---------------------------------------------------------------------------
# Per-scenario state
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scenario_state(tmp_path: Path) -> dict[str, Any]:
    """Reset shared state for every scenario.

    pytest-bdd shares fixtures across the Given/When/Then within one scenario
    but starts fresh for the next; binding to ``_state`` via this fixture
    keeps each scenario isolated without resorting to a module-global dict.
    """
    return {
        "tmp_path": tmp_path,
        "log_path": tmp_path / "monitor.jsonl",
        "alert_threshold": 0.05,
        "result": None,
        "n_runs": 1,
        "case_results": None,
    }


# ---------------------------------------------------------------------------
# Background givens
# ---------------------------------------------------------------------------


@given(parsers.parse("an injected suite loader returning {n:d} recall cases"))
def _given_suite_loader(_scenario_state: dict[str, Any], n: int) -> None:
    def _loader(_path: str) -> BenchmarkSuite:
        return BenchmarkSuite(
            meta={"agent": "shape", "collections": ["vault"]},
            cases=[
                BenchmarkCase(
                    id=f"R{i}",
                    category="recall",
                    query=f"q{i}",
                    gold_path=f"vault/d{i}.md",
                    score_method="exact",
                )
                for i in range(n)
            ],
        )

    _scenario_state["suite_loader"] = _loader


@given("an injected benchmark runner that returns deterministic scores")
def _given_default_runner(_scenario_state: dict[str, Any]) -> None:
    # Default runner — overridden by later givens. Returns a baseline 0.7
    # weighted_total with all six categories at 0.7. Scenarios that need
    # specific scores set ``_scenario_state["weighted_total"]`` and
    # ``_scenario_state["case_results"]``; this is the fallback.
    _scenario_state.setdefault("weighted_total", 0.7)


# ---------------------------------------------------------------------------
# Score / log setup
# ---------------------------------------------------------------------------


@given(parsers.parse("the benchmark runner returns weighted_total {score:f}"))
def _given_runner_score(_scenario_state: dict[str, Any], score: float) -> None:
    _scenario_state["weighted_total"] = score


@given(parsers.parse("the benchmark runner returns {n:d} case results, {failed:d} of which carry vec_failed True"))
def _given_case_results_with_vec_failures(_scenario_state: dict[str, Any], n: int, failed: int) -> None:
    _scenario_state["case_results"] = [
        {"id": f"C{i}", "category": "recall", "score": 0.7, "vec_failed": i < failed} for i in range(n)
    ]


@given(parsers.parse("an alert threshold of {value:f}"))
def _given_alert_threshold(_scenario_state: dict[str, Any], value: float) -> None:
    _scenario_state["alert_threshold"] = value


@given("no previous monitor log entries")
def _given_no_log(_scenario_state: dict[str, Any]) -> None:
    log_path: Path = _scenario_state["log_path"]
    if log_path.exists():
        log_path.unlink()


@given("an empty monitor log file")
def _given_empty_log(_scenario_state: dict[str, Any]) -> None:
    log_path: Path = _scenario_state["log_path"]
    log_path.write_text("")


@given(parsers.parse("a previous monitor log with weighted_ndcg {score:f} from one day ago"))
def _given_seeded_log(_scenario_state: dict[str, Any], score: float) -> None:
    log_path: Path = _scenario_state["log_path"]
    ts = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()
    entry = {"ts": ts, "weighted_ndcg": score}
    log_path.write_text(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# When — invoke run_monitor with the configured fakes
# ---------------------------------------------------------------------------


def _build_runner(state: dict[str, Any]):
    """Build a benchmark_runner callable from the configured state."""
    weighted = state.get("weighted_total", 0.7)
    cat_scores = {
        "recall": weighted,
        "temporal": weighted,
        "entity": weighted,
        "conceptual": weighted,
        "multi_hop": weighted,
        "procedural": weighted,
    }
    cases = state.get("case_results") or []

    def _runner(_suite: BenchmarkSuite, **_kwargs: Any) -> BenchmarkResult:
        return BenchmarkResult(
            meta={"system": "hybrid"},
            summary={"weighted_total": weighted, "category_scores": cat_scores},
            diagnostics={},
            cases=cases,
        )

    return _runner


@when("the operator runs the monitor")
def _when_run_monitor(_scenario_state: dict[str, Any]) -> None:
    _scenario_state["result"] = run_monitor(
        suite_path=str(_scenario_state["tmp_path"] / "canary.yaml"),
        log_path=str(_scenario_state["log_path"]),
        alert_threshold=_scenario_state["alert_threshold"],
        suite_loader=_scenario_state["suite_loader"],
        benchmark_runner=_build_runner(_scenario_state),
    )


@when(parsers.parse("the operator runs the monitor {n:d} times"))
def _when_run_monitor_n_times(_scenario_state: dict[str, Any], n: int) -> None:
    last = None
    for _ in range(n):
        last = run_monitor(
            suite_path=str(_scenario_state["tmp_path"] / "canary.yaml"),
            log_path=str(_scenario_state["log_path"]),
            alert_threshold=_scenario_state["alert_threshold"],
            suite_loader=_scenario_state["suite_loader"],
            benchmark_runner=_build_runner(_scenario_state),
        )
    _scenario_state["result"] = last


# ---------------------------------------------------------------------------
# Then — assertions on the MonitorResult / log file
# ---------------------------------------------------------------------------


@then("the result reports regression as False")
def _then_no_regression(_scenario_state: dict[str, Any]) -> None:
    assert _scenario_state["result"].regression is False


@then("the result reports regression as True")
def _then_regression(_scenario_state: dict[str, Any]) -> None:
    assert _scenario_state["result"].regression is True


@then("the result reports regression_detail as None")
def _then_no_detail(_scenario_state: dict[str, Any]) -> None:
    assert _scenario_state["result"].regression_detail is None


@then("the regression_detail names the baseline and the drop amount")
def _then_detail_named_baseline_and_drop(_scenario_state: dict[str, Any]) -> None:
    detail = _scenario_state["result"].regression_detail
    assert detail is not None, "expected non-empty regression_detail when regression is True"
    # Baseline 0.80 → "0.8000" appears verbatim; drop = 0.80-0.50 = 0.30 → "0.3000".
    assert "0.8000" in detail, f"expected baseline 0.8000 in detail; got: {detail}"
    assert "0.3000" in detail, f"expected drop 0.3000 in detail; got: {detail}"


@then(parsers.parse("the result reports vec_failed_count as {n:d}"))
def _then_vec_failed_count(_scenario_state: dict[str, Any], n: int) -> None:
    assert _scenario_state["result"].vec_failed_count == n


@then(parsers.parse("the monitor log file contains {n:d} JSONL entries"))
def _then_log_entry_count(_scenario_state: dict[str, Any], n: int) -> None:
    log_path: Path = _scenario_state["log_path"]
    assert log_path.exists(), "expected the monitor log file to have been written"
    lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) == n
    # Each line is valid JSON.
    for line in lines:
        json.loads(line)
