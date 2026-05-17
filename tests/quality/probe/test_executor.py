"""Unit tests for `kairix.quality.probe.executor.run_concurrent`.

Pins per-task timing isolation, never-raises behaviour, and Little's-Law
mean-concurrency calculation. Tasks are synthetic callables — no kairix
search pipeline is constructed.
"""

from __future__ import annotations

import time

import pytest

from kairix.quality.probe.executor import ConcurrentRun, run_concurrent

pytestmark = pytest.mark.unit


def _sleeper(seconds: float, value: int) -> int:
    """Return a callable that sleeps then returns ``value``."""
    time.sleep(seconds)
    return value


def test_zero_concurrency_rejected() -> None:
    """concurrency=0 has no defensible meaning — reject.

    Sabotage-proof: drop the guard and ``ThreadPoolExecutor(max_workers=0)``
    raises a less actionable error from the pool internals.
    """
    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        run_concurrent([lambda: 1], concurrency=0)


def test_empty_tasks_rejected() -> None:
    """No tasks → caller bug; raise rather than silently return 0-task report.

    Sabotage-proof: remove the guard and the function returns a meaningless
    ConcurrentRun with mean_concurrency=0 that the bottleneck heuristic
    would misinterpret as worker contention.
    """
    with pytest.raises(ValueError, match="tasks must contain at least one"):
        run_concurrent([], concurrency=1)


def test_all_tasks_succeed_results_count_matches() -> None:
    """N successful tasks → N TimedResults, all succeeded=True.

    Sabotage-proof: drop the ``futures.append`` loop and we get 0 results.
    """
    tasks = [lambda v=i: v for i in range(5)]
    run = run_concurrent(tasks, concurrency=2)
    assert isinstance(run, ConcurrentRun)
    assert len(run.results) == 5
    assert all(r.succeeded for r in run.results)
    assert sorted(r.result for r in run.results) == [0, 1, 2, 3, 4]
    assert run.errors == 0


def test_task_exception_captured_not_raised() -> None:
    """A raising task records the error and the pool keeps going for others.

    Sabotage-proof: remove the try/except in ``_wrapped`` and the future
    propagates the exception, breaking the whole run.
    """

    def raiser() -> int:
        raise RuntimeError("boom")

    tasks = [raiser, lambda: 42]
    run = run_concurrent(tasks, concurrency=2)
    assert len(run.results) == 2
    assert run.errors == 1
    failed = next(r for r in run.results if not r.succeeded)
    assert "RuntimeError" in failed.error
    assert "boom" in failed.error
    ok = next(r for r in run.results if r.succeeded)
    assert ok.result == 42


def test_mean_concurrency_approaches_requested_when_tasks_overlap() -> None:
    """Five 50ms sleepers at concurrency=5 → mean_concurrency near 5.

    Sleeps release the GIL, so a ThreadPoolExecutor really does run them in
    parallel. Little's Law: 5 tasks * 0.05s of work / ~0.05s wallclock ≈ 5.

    Sabotage-proof: replace ThreadPoolExecutor with a serial for-loop and
    mean_concurrency collapses to 1.0.
    """
    tasks = [(lambda v=i: _sleeper(0.05, v)) for i in range(5)]
    run = run_concurrent(tasks, concurrency=5)
    assert run.mean_concurrency >= 3.5, f"expected near-5, got {run.mean_concurrency}"
    assert run.errors == 0


def test_mean_concurrency_is_one_when_concurrency_one() -> None:
    """concurrency=1 forces serialisation regardless of how many tasks run.

    Sabotage-proof: change ``max_workers=concurrency`` to a fixed >1 value
    and parallel execution sneaks in, breaking this assertion.
    """
    tasks = [(lambda v=i: _sleeper(0.02, v)) for i in range(3)]
    run = run_concurrent(tasks, concurrency=1)
    assert run.mean_concurrency == pytest.approx(1.0, abs=0.2)


def test_wallclock_includes_full_run() -> None:
    """Wallclock covers from first submit to last completion.

    Sabotage-proof: measure wallclock around a single future and we'd
    miss the parallelism. With 3x 50ms tasks at concurrency=3 the wallclock
    should be near 50ms (not 150ms).
    """
    tasks = [(lambda v=i: _sleeper(0.05, v)) for i in range(3)]
    run = run_concurrent(tasks, concurrency=3)
    assert run.wallclock_s < 0.12, f"expected ~0.05s, got {run.wallclock_s}s"
    assert run.wallclock_s >= 0.04


def test_durations_recorded_per_task() -> None:
    """Each TimedResult.duration_ms reflects only its own task body.

    Sabotage-proof: capture duration outside the worker (around as_completed
    instead) and slow tasks pull fast tasks' durations up.
    """
    tasks = [(lambda: _sleeper(0.02, 1)), (lambda: _sleeper(0.06, 2))]
    run = run_concurrent(tasks, concurrency=2)
    durs = sorted(r.duration_ms for r in run.results)
    assert durs[0] < 40, f"fast task duration leaked: {durs[0]}ms"
    assert durs[1] >= 50


class _StagedReturn:
    """Carries a stage_latency_ms dict — matches kairix SearchResult shape."""

    def __init__(self, stages: dict[str, float]) -> None:
        self.stage_latency_ms = stages


def test_timed_result_caches_stage_latency_from_kairix_shaped_results() -> None:
    """A task returning a SearchResult-shaped value populates TimedResult.stage_latency_ms.

    The executor reads ``stage_latency_ms`` off the task's return value via
    ``getattr`` and caches a defensive copy on TimedResult so the runner
    can build per-query stage records without reaching into ``.result``.

    Sabotage-proof: drop the ``getattr(value, "stage_latency_ms", None)``
    capture in ``_wrapped`` and ``TimedResult.stage_latency_ms`` stays
    None even when the task returned a kairix-shaped result.
    """
    stages = {"classify": 1.0, "vector": 200.0, "embed_http": 180.0, "vector_ann": 20.0}
    run = run_concurrent([lambda: _StagedReturn(stages)], concurrency=1)
    assert len(run.results) == 1
    cached = run.results[0].stage_latency_ms
    assert cached == stages, f"expected stage map {stages!r} cached on TimedResult; got {cached!r}"
    # Defensive copy: mutating the source dict must not mutate the cache.
    stages["classify"] = 999.0
    assert run.results[0].stage_latency_ms is not None
    assert run.results[0].stage_latency_ms["classify"] == 1.0


def test_timed_result_stage_latency_none_for_dict_returning_fakes() -> None:
    """Non-kairix-shaped results yield TimedResult.stage_latency_ms = None.

    A dict-returning fake has no ``stage_latency_ms`` attribute. The
    ``getattr`` default is None, the runner aggregator skips it, and
    the probe envelope stays valid with an empty stage_means_ms.

    Sabotage-proof: change the ``isinstance(stage_map, dict)`` guard to
    ``stage_map is not None`` and a fake returning ``{"results": [...]}``
    (where dict items are scalars, not stage->ms) populates a bogus
    stage map.
    """
    run = run_concurrent([lambda: {"results": "fake"}], concurrency=1)
    assert run.results[0].stage_latency_ms is None


def test_timed_result_records_submission_order_task_index() -> None:
    """Each TimedResult carries its 0-based submission-order index.

    Necessary because the pool returns results in completion order; without
    task_index the runner can't map a completed result back to its source
    SampledQuery (case_id, category).

    Sabotage-proof: drop the ``enumerate(tasks)`` / ``i`` plumbing and every
    task_index stays at the -1 default, so per_query_stages in the runner
    is built off the wrong sampled-query metadata.
    """

    def make_task(v: int):
        return lambda: v

    tasks = [make_task(v) for v in range(4)]
    run = run_concurrent(tasks, concurrency=2)
    # Index must round-trip to the original value the task returned —
    # proves the index in TimedResult matches the submission position.
    by_index = {r.task_index: r.result for r in run.results}
    assert by_index == {0: 0, 1: 1, 2: 2, 3: 3}


def test_timed_result_task_index_set_even_when_task_raises() -> None:
    """A raising task still gets its task_index recorded.

    Sabotage-proof: omit ``task_index=idx`` from the failure-path
    TimedResult and a failed task can't be mapped back to its sampled
    query for triage.
    """

    def raiser() -> int:
        raise RuntimeError("boom")

    def succeeder() -> int:
        return 7

    run = run_concurrent([raiser, succeeder], concurrency=2)
    failed = next(r for r in run.results if not r.succeeded)
    succeeded = next(r for r in run.results if r.succeeded)
    assert failed.task_index == 0
    assert succeeded.task_index == 1
