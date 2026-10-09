"""Unit tests for :func:`kairix.quality.benchmark.modes.run_single_shot`.

Coverage targets (F7 >= 90% on single_shot.py):
- happy path with a single case
- happy path with multiple cases (phase labelling)
- empty suite (no cases)
- one query raises - others continue, error is captured
- one query returns ``succeeded=False`` - error is captured
- one slow query doesn't poison neighbour latency
- aggregate metrics shape

Each test carries a ``# sabotage:`` note describing the production
mutation that flips the assertion; sabotage was executed prior to
commit per the project's "Sabotage proofs must be executed" memory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.quality.benchmark.modes import (
    ModeRunRequest,
    ModeRunResult,
    QueryRunResult,
    run_single_shot,
)
from kairix.quality.probe.runner import SampledQuery


@dataclass
class _StubCase:
    """Minimal BenchmarkCase shape - the dispatcher only reads four fields."""

    id: str
    category: str
    query: str
    agent: str | None = None


@dataclass
class _StubSuite:
    """Minimal BenchmarkSuite shape - the dispatcher only reads ``cases``."""

    cases: list[_StubCase]
    meta: dict[str, Any] = field(default_factory=dict)


def _executor_returning(
    scores: dict[str, float] | None = None,
    *,
    latency_ms: float = 1.0,
) -> Any:
    """Build a deterministic executor closure that returns succeeded=True."""
    score_map = scores or {}

    def _exec(sampled: SampledQuery) -> QueryRunResult:
        # ``score_map`` retained for legacy parametrisation but not
        # surfaced on the canonical QueryRunResult; scorers (P2) compute
        # scores from ranked_doc_ids / synthesised_answer post-hoc.
        _ = score_map.get(sampled.case_id, 1.0)
        return QueryRunResult(
            query_id=sampled.case_id,
            category=sampled.category,
            query_text=sampled.query,
            latency_ms=latency_ms,
        )

    return _exec


# ---------------------------------------------------------------------------
# Happy path - single case
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_case_run_returns_one_result() -> None:
    """A one-case suite produces exactly one ``QueryRunResult``.

    sabotage: replace ``for idx, case in enumerate(...)`` with an
    early return - the length assertion fails.
    """
    suite = _StubSuite(cases=[_StubCase(id="R01", category="recall", query="hi")])
    req = ModeRunRequest(suite=suite, query_executor=_executor_returning())

    result = run_single_shot(req)

    assert isinstance(result, ModeRunResult)
    assert len(result.per_query_runs) == 1
    assert result.per_query_runs[0].query_id == "R01"
    assert result.per_query_runs[0].latency_ms >= 0.0
    assert result.errors == ()


# ---------------------------------------------------------------------------
# Empty suite
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_suite_returns_empty_result_list() -> None:
    """An empty suite yields empty ``per_query_runs`` + zeroed metrics.

    sabotage: replace ``if n else 0.0`` with ``sum(...) / n`` - empty
    suite hits ZeroDivisionError instead of returning 0.0.
    """
    suite = _StubSuite(cases=[])
    req = ModeRunRequest(suite=suite, query_executor=_executor_returning())

    result = run_single_shot(req)

    assert result.per_query_runs == ()
    assert result.errors == ()
    assert result.mode_metrics["n"] == 0.0
    assert result.mode_metrics["mean_latency_ms"] == 0.0


# ---------------------------------------------------------------------------
# Exception in one query - others continue
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_exception_in_one_query_is_captured_others_continue() -> None:
    """When the executor raises for one case, that case gets
    ``succeeded=False`` + populated ``error``; later cases run normally.

    sabotage: drop the ``try/except`` from ``_run_one`` - the
    exception propagates and the third case never runs (length 2).
    """

    def _exec(sampled: SampledQuery) -> QueryRunResult:
        if sampled.case_id == "BAD":
            raise RuntimeError("boom; api-key credential-sentinel-7f3a")
        return QueryRunResult(
            query_id=sampled.case_id,
            category=sampled.category,
            query_text=sampled.query,
            latency_ms=0.5,
        )

    suite = _StubSuite(
        cases=[
            _StubCase(id="OK1", category="recall", query="a"),
            _StubCase(id="BAD", category="recall", query="b"),
            _StubCase(id="OK2", category="recall", query="c"),
        ]
    )
    req = ModeRunRequest(suite=suite, query_executor=_exec)

    result = run_single_shot(req)

    assert len(result.per_query_runs) == 3
    by_id = {r.query_id: r for r in result.per_query_runs}
    assert by_id["OK1"].error is None
    assert by_id["BAD"].error is not None
    # Class-only — the exception message never reaches the per-query row.
    assert by_id["BAD"].error == "query raised RuntimeError"
    assert all("credential-sentinel-7f3a" not in e for e in result.errors)
    assert "credential-sentinel-7f3a" not in str(result.per_query_runs)
    assert by_id["OK2"].error is None
    assert any("BAD" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Executor returns succeeded=False - non-raising failure path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_executor_returning_failure_is_recorded_as_error() -> None:
    """An executor that returns ``succeeded=False`` (without raising)
    has its ``error`` surfaced in the aggregate ``errors`` tuple.

    sabotage: drop the ``[outcome.case_id]`` prefix - the case_id-
    prefix assertion fails.
    """

    def _exec(sampled: SampledQuery) -> QueryRunResult:
        return QueryRunResult(
            query_id=sampled.case_id,
            category=sampled.category,
            query_text=sampled.query,
            latency_ms=0.1,
            error="planned failure",
        )

    suite = _StubSuite(cases=[_StubCase(id="F01", category="recall", query="x")])
    req = ModeRunRequest(suite=suite, query_executor=_exec)

    result = run_single_shot(req)

    assert len(result.per_query_runs) == 1
    assert result.per_query_runs[0].error is not None
    assert result.errors == ("[F01] planned failure",)
    assert result.mode_metrics["errors"] == 1.0


# ---------------------------------------------------------------------------
# Cold/warm phase labelling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_first_case_is_cold_subsequent_are_warm() -> None:
    """Phase label is ``cold`` on case index 0 and ``warm`` on every
    subsequent case.

    sabotage: invert the ``cold if is_first else warm`` to
    ``warm if is_first else cold`` - first-case-is-cold assertion fails.
    """
    suite = _StubSuite(
        cases=[
            _StubCase(id="Q1", category="recall", query="a"),
            _StubCase(id="Q2", category="recall", query="b"),
            _StubCase(id="Q3", category="recall", query="c"),
        ]
    )
    req = ModeRunRequest(suite=suite, query_executor=_executor_returning())

    result = run_single_shot(req)

    phases = [r.latency_phase for r in result.per_query_runs]
    assert phases == ["cold", "warm", "warm"]


# ---------------------------------------------------------------------------
# Per-query latency isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_slow_query_latency_does_not_leak_into_neighbours() -> None:
    """When the executor reports per-query latency, one slow query's
    latency belongs only to that case - surrounding cases keep their
    fast reported figures.

    sabotage: rewrite ``_label_phase`` to compute a fresh
    ``latency_ms = (time.perf_counter() - run_start) * 1000`` - the
    fast neighbour's recorded latency grows past the 10ms ceiling.
    """

    def _exec(sampled: SampledQuery) -> QueryRunResult:
        if sampled.case_id == "SLOW":
            time.sleep(0.03)
            reported = 30.0
        else:
            reported = 0.5
        return QueryRunResult(
            query_id=sampled.case_id,
            category=sampled.category,
            query_text=sampled.query,
            latency_ms=reported,
        )

    suite = _StubSuite(
        cases=[
            _StubCase(id="FAST1", category="recall", query="a"),
            _StubCase(id="SLOW", category="recall", query="b"),
            _StubCase(id="FAST2", category="recall", query="c"),
        ]
    )
    req = ModeRunRequest(suite=suite, query_executor=_exec)

    result = run_single_shot(req)

    by_id = {r.query_id: r for r in result.per_query_runs}
    assert by_id["FAST1"].latency_ms < 10.0
    assert by_id["FAST2"].latency_ms < 10.0
    assert by_id["SLOW"].latency_ms >= 25.0


# ---------------------------------------------------------------------------
# Aggregate metrics shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mode_metrics_carry_wallclock_mean_latency_and_counts() -> None:
    """``mode_metrics`` exposes the four single-shot aggregates:
    ``wallclock_s``, ``mean_latency_ms``, ``errors``, ``n``.

    sabotage: drop ``"n": float(n)`` from the metrics dict - the n
    assertion KeyErrors.
    """
    suite = _StubSuite(
        cases=[
            _StubCase(id="A", category="recall", query="q1"),
            _StubCase(id="B", category="recall", query="q2"),
        ]
    )
    req = ModeRunRequest(
        suite=suite,
        query_executor=_executor_returning(latency_ms=2.5),
    )

    result = run_single_shot(req)

    metrics = result.mode_metrics
    assert metrics["n"] == 2.0
    assert metrics["errors"] == 0.0
    assert metrics["mean_latency_ms"] == pytest.approx(2.5, rel=0.01)
    assert metrics["wallclock_s"] >= 0.0


# ---------------------------------------------------------------------------
# Per-case agent override flows through
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stubs for concurrent + soak — P3.b / P3.c affordance markers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_concurrent_stub_raises_with_p3b_marker() -> None:
    """Calling the concurrent stub raises NotImplementedError with the F21
    ``next:`` action marker pointing to the P3.b slice.

    sabotage: replace the NotImplementedError with a silent ``pass`` -
    the assertion-on-message check fails because no exception is raised.
    """
    from kairix.quality.benchmark.modes import run_concurrent

    suite = _StubSuite(cases=[_StubCase(id="X", category="recall", query="q")])
    req = ModeRunRequest(suite=suite, query_executor=_executor_returning())
    with pytest.raises(NotImplementedError, match=r"P3\.b"):
        run_concurrent(req)


@pytest.mark.unit
def test_soak_stub_raises_with_p3c_marker() -> None:
    """Calling the soak stub raises NotImplementedError with the F21
    ``next:`` action marker pointing to the P3.c slice.

    sabotage: replace the NotImplementedError with a silent ``pass`` -
    the assertion-on-message check fails because no exception is raised.
    """
    from kairix.quality.benchmark.modes import run_soak

    suite = _StubSuite(cases=[_StubCase(id="X", category="recall", query="q")])
    req = ModeRunRequest(suite=suite, query_executor=_executor_returning())
    with pytest.raises(NotImplementedError, match=r"P3\.c"):
        run_soak(req)


@pytest.mark.unit
def test_per_case_agent_is_propagated_to_executor() -> None:
    """When a ``BenchmarkCase`` carries a per-case ``agent`` override,
    the dispatcher reflects it on the ``SampledQuery`` the executor sees.

    sabotage: drop the ``agent`` argument from ``_to_sampled_query`` -
    the executor sees ``None`` and the assertion fails.
    """
    seen: list[str | None] = []

    def _exec(sampled: SampledQuery) -> QueryRunResult:
        seen.append(sampled.agent)
        return QueryRunResult(
            query_id=sampled.case_id,
            category=sampled.category,
            query_text=sampled.query,
            latency_ms=0.1,
        )

    suite = _StubSuite(
        cases=[
            _StubCase(id="X", category="recall", query="q", agent="builder"),
            _StubCase(id="Y", category="recall", query="q", agent=None),
        ]
    )
    req = ModeRunRequest(suite=suite, query_executor=_exec)

    run_single_shot(req)

    assert seen == ["builder", None]
