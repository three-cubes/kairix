"""Unit tests for :mod:`kairix.quality.completeness` — the shared rule."""

from __future__ import annotations

from typing import Any

import pytest

from kairix.quality.benchmark.runner import BenchmarkResult
from kairix.quality.completeness import (
    EXIT_INCONCLUSIVE,
    is_complete,
    judge_failures,
    partial_diagnostic,
    partial_warning,
)
from kairix.quality.eval.suite_runner import SuiteResult

pytestmark = pytest.mark.unit


def _suite_result(**overrides: Any) -> SuiteResult:
    fields: dict[str, Any] = {
        "suite_name": "s",
        "n_questions": 1,
        "n_passed": 1,
        "mean_score": 1.0,
        "per_category": {},
        "per_extraction_f1": None,
        "extraction_precision": None,
        "extraction_recall": None,
    }
    fields.update(overrides)
    return SuiteResult(**fields)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"summary": {"weighted_total": 0.9}}, 0),  # legacy benchmark artifact
        ({"n_questions": 3, "mean_score": 0.5}, 0),  # legacy suite artifact
        ({"summary": {"judge_failures": 2}}, 2),  # benchmark: summary.judge_failures
        ({"judge_failures": 4}, 4),  # suite / partial marker: top-level judge_failures
        (None, 0),
    ],
    ids=["legacy_benchmark", "legacy_suite", "benchmark_count", "suite_count", "none"],
)
def test_judge_failures_reads_the_canonical_count(result: Any, expected: int) -> None:
    """Partial ⇔ the canonical count is > 0: ``summary.judge_failures`` for a
    benchmark result, top-level ``judge_failures`` otherwise; legacy
    artifacts without a count are complete.

    Sabotage-proof: read only the top-level count — the ``benchmark_count``
    case reports 0 and fails. Restored.
    """
    assert judge_failures(result) == expected
    assert is_complete(result) is (expected == 0)


def test_non_canonical_fields_are_not_read() -> None:
    """Only the canonical count decides completeness: failed rows / cases and
    the derived ``judge_coverage`` gate are not separate sources (every
    writer sets the count alongside them)."""
    assert judge_failures({"summary": {"gates": {"judge_coverage": False}}, "cases": [{"judge_failure": "x"}]}) == 0
    assert judge_failures({"rows": [{"judge_failure": "backend_error"}]}) == 0


def test_judge_failures_reads_in_memory_dataclasses() -> None:
    """``BenchmarkResult`` and ``SuiteResult`` dataclasses are read directly."""
    bench = BenchmarkResult(meta={}, summary={"judge_failures": 1}, diagnostics={}, cases=[])
    assert judge_failures(bench) == 1
    assert judge_failures(_suite_result(judge_failures=2)) == 2
    assert is_complete(_suite_result())
    assert judge_failures(BenchmarkResult) == 0  # a class, not an instance


def test_messages_are_actionable() -> None:
    """The gate diagnostic carries fix:/next: hints; both name the count."""
    diag = partial_diagnostic("run X", 3)
    assert diag.startswith("INCONCLUSIVE: run X is PARTIAL")
    assert "3 case(s)" in diag
    assert "fix:" in diag
    assert "next:" in diag
    assert "PARTIAL RESULT: run X" in partial_warning("run X", 3)
    assert EXIT_INCONCLUSIVE == 3
