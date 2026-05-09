"""Contract-first tests for kairix.quality.eval.monitor — describe what the
docstrings/result fields claim and verify the implementation honours them.

These are written from the public contract, NOT from the current code, so
that any divergence between contract and implementation surfaces as a real
test failure.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kairix.quality.benchmark.runner import BenchmarkResult
from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite
from kairix.quality.eval.monitor import MonitorResult, run_monitor


def _suite_with_cases(n: int):
    def _loader(_path: str) -> BenchmarkSuite:
        return BenchmarkSuite(
            meta={"agent": "shape", "collections": ["vault"]},
            cases=[
                BenchmarkCase(
                    id=f"R{i}",
                    category="recall",
                    query="q",
                    gold_path=f"vault/d{i}.md",
                    score_method="exact",
                )
                for i in range(n)
            ],
        )

    return _loader


# ---------------------------------------------------------------------------
# Contract: vec_failed_count reflects retrievals that failed vector search.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_monitor_result_vec_failed_count_counts_cases_with_failed_vector_search(tmp_path: Path) -> None:
    """``MonitorResult.vec_failed_count`` should report the number of benchmark
    cases whose retrieval reported a failed vector search.

    Contract: vec_failed_count > 0 when some case results carry vec_failed=True.
    """

    def _runner(_suite: BenchmarkSuite, **_kwargs: Any) -> BenchmarkResult:
        # The benchmark runner spreads retrieval_meta into each case dict —
        # vec_failed is a direct key on the case, not nested under "meta".
        return BenchmarkResult(
            meta={"system": "hybrid"},
            summary={
                "weighted_total": 0.7,
                "category_scores": {
                    "recall": 0.7,
                    "temporal": 0.7,
                    "entity": 0.7,
                    "conceptual": 0.7,
                    "multi_hop": 0.7,
                    "procedural": 0.7,
                },
            },
            diagnostics={},
            cases=[
                {"id": "R1", "category": "recall", "score": 0.7, "vec_failed": True},
                {"id": "R2", "category": "recall", "score": 0.7, "vec_failed": True},
                {"id": "R3", "category": "recall", "score": 0.7, "vec_failed": False},
            ],
        )

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(tmp_path / "monitor.jsonl"),
        suite_loader=_suite_with_cases(3),
        benchmark_runner=_runner,
    )

    # Two of three cases have vec_failed=True — the count must reflect that.
    assert result.vec_failed_count == 2, (
        f"expected vec_failed_count to count cases with vec_failed=True; got {result.vec_failed_count}"
    )


# ---------------------------------------------------------------------------
# Contract: weighted_ndcg = sum(per_category_score * CATEGORY_WEIGHT).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_monitor_weighted_ndcg_equals_weighted_sum_of_category_scores(tmp_path: Path) -> None:
    """``MonitorResult.weighted_ndcg`` should equal sum(cat_score * cat_weight)
    over CATEGORY_WEIGHTS — categories missing from the result default to 0.
    """
    from kairix.quality.eval.constants import CATEGORY_WEIGHTS

    cat_scores = {
        "recall": 0.80,
        "temporal": 0.70,
        "entity": 0.75,
        "conceptual": 0.72,
        "multi_hop": 0.65,
        "procedural": 0.68,
    }

    def _runner(_suite: BenchmarkSuite, **_kwargs: Any) -> BenchmarkResult:
        return BenchmarkResult(
            meta={"system": "hybrid"},
            summary={"weighted_total": 0.0, "category_scores": cat_scores},
            diagnostics={},
            cases=[],
        )

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(tmp_path / "monitor.jsonl"),
        suite_loader=_suite_with_cases(6),
        benchmark_runner=_runner,
    )

    # Reconstruct the expected weighted sum: every CATEGORY_WEIGHTS key, with
    # missing categories contributing 0.
    expected = round(sum(cat_scores.get(c, 0.0) * w for c, w in CATEGORY_WEIGHTS.items()), 4)
    assert result.weighted_ndcg == pytest.approx(expected), (
        f"weighted_ndcg should equal weighted sum of category scores; expected {expected}, got {result.weighted_ndcg}"
    )


# ---------------------------------------------------------------------------
# Contract: regression_detail is None when regression is False.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_monitor_regression_detail_is_none_when_regression_is_false(tmp_path: Path) -> None:
    """When regression=False, regression_detail must be None (not an empty
    string, not a stale message from the previous run).
    """
    log_path = tmp_path / "monitor.jsonl"
    # Seed a stable baseline so no regression is detected on the second run.
    entry = {
        "ts": (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat(),
        "weighted_ndcg": 0.7,
    }
    log_path.write_text(json.dumps(entry) + "\n")

    def _runner(_suite: BenchmarkSuite, **_kwargs: Any) -> BenchmarkResult:
        return BenchmarkResult(
            meta={},
            summary={
                "weighted_total": 0.7,
                "category_scores": {
                    "recall": 0.7,
                    "temporal": 0.7,
                    "entity": 0.7,
                    "conceptual": 0.7,
                    "multi_hop": 0.7,
                    "procedural": 0.7,
                },
            },
            diagnostics={},
            cases=[],
        )

    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(log_path),
        suite_loader=_suite_with_cases(3),
        benchmark_runner=_runner,
    )

    assert result.regression is False
    assert result.regression_detail is None


# ---------------------------------------------------------------------------
# Contract: never raises — any internal error returns regression=False.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_monitor_never_raises_when_loader_returns_a_malformed_suite(tmp_path: Path) -> None:
    """The "Never raises" docstring guarantee should hold even when the loader
    returns a partially-malformed suite (cases is None instead of a list).
    """

    class _BrokenSuite:
        cases = None  # malformed — len() will raise

    def _malformed_loader(_path: str) -> Any:
        return _BrokenSuite()

    # Must not raise — the guarantee is "returns MonitorResult with regression=False".
    result = run_monitor(
        suite_path=str(tmp_path / "canary.yaml"),
        log_path=str(tmp_path / "monitor.jsonl"),
        suite_loader=_malformed_loader,
        benchmark_runner=lambda *_a, **_kw: None,  # never called
    )

    assert isinstance(result, MonitorResult)
    assert result.regression is False
