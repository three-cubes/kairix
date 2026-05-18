"""Concurrent task executor with per-task timing — generic over the work unit.

The probe uses this to fan out N search calls across a thread pool of size
``concurrency`` and capture the per-call latency + an exact mean-concurrency
figure.

Concurrency theory note. Mean concurrency over a run is, by Little's Law:

    mean_concurrency = sum(per-task duration) / wallclock duration

When tasks fully overlap (perfect parallelism) the sum equals N x mean
duration and the wallclock equals one mean duration → mean_concurrency ≈ N.
When tasks fully serialise the sum equals the wallclock → mean_concurrency
≈ 1. The probe's bottleneck heuristic compares this to the requested
concurrency to spot worker contention (see kairix.quality.probe.stats).

No retries, no shielding. If a task raises, the executor records the
exception text and continues — never raises from inside the pool. Callers
decide what to do about errors.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class TimedResult(Generic[T]):
    """One task's outcome — duration + result-or-error.

    ``result`` is None when ``succeeded=False``. ``duration_ms`` is captured
    around the task callable regardless of success, so failed-but-slow tasks
    are still visible to the latency distribution.

    ``stage_latency_ms`` carries the per-stage breakdown when the task's
    return value exposes a ``stage_latency_ms`` attribute (kairix
    SearchResult does, dict-returning fakes don't). The wrapper reads it
    via ``getattr`` so non-kairix-shaped results just yield ``None``. This
    lets the probe runner surface per-query stage data without each call
    site reaching into ``.result`` (#282 follow-up).

    ``task_index`` is the 0-based position of the task in the submitted
    ``tasks`` sequence. Lets callers map each completion-order result back
    to its submission-order metadata (sampled-query case_id, category) —
    necessary because the pool returns results in completion order, not
    submission order. ``-1`` is the default for tasks submitted via APIs
    that don't track index.
    """

    duration_ms: float
    succeeded: bool
    result: T | None = None
    error: str = ""
    stage_latency_ms: dict[str, float] | None = None
    task_index: int = -1


@dataclass(frozen=True)
class ConcurrentRun(Generic[T]):
    """Aggregate report from one ``run_concurrent`` invocation."""

    results: list[TimedResult[T]] = field(default_factory=list)
    wallclock_s: float = 0.0
    mean_concurrency: float = 0.0
    errors: int = 0


def run_concurrent(
    tasks: Sequence[Callable[[], T]],
    concurrency: int,
) -> ConcurrentRun[T]:
    """Run ``tasks`` through a thread pool of ``concurrency`` workers.

    Each callable is executed exactly once. Per-call timing is captured
    inside the worker so it includes only the task body, not queueing
    delays inside the pool.

    Args:
        tasks: callables to execute. Order of completion is non-deterministic;
            the returned ``results`` list is in completion order.
        concurrency: max worker count. ``1`` runs sequentially in worker
            threads (still useful — picks up GIL release in C extensions).
            Values >= 2 enable parallel I/O.

    Returns:
        ConcurrentRun summarising results, wallclock, mean concurrency, and
        an error count. Never raises — task exceptions are captured in
        per-task ``TimedResult.error`` strings.

    Raises:
        ValueError: when concurrency < 1 or tasks is empty.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1; got {concurrency}")
    if not tasks:
        raise ValueError("tasks must contain at least one callable")

    def _wrapped(t: Callable[[], T], idx: int) -> TimedResult[T]:
        t_start = time.perf_counter()
        try:
            value = t()
        except Exception as exc:
            duration_ms = (time.perf_counter() - t_start) * 1000.0
            return TimedResult(
                duration_ms=duration_ms,
                succeeded=False,
                error=f"{type(exc).__name__}: {exc}",
                task_index=idx,
            )
        duration_ms = (time.perf_counter() - t_start) * 1000.0
        # getattr is the documented seam for non-kairix-shaped results.
        # A dict-returning fake (no .stage_latency_ms attribute) yields
        # None and the runner aggregator skips it cleanly.
        stage_map = getattr(value, "stage_latency_ms", None)
        stage_latency = dict(stage_map) if isinstance(stage_map, dict) else None
        return TimedResult(
            duration_ms=duration_ms,
            succeeded=True,
            result=value,
            stage_latency_ms=stage_latency,
            task_index=idx,
        )

    wall_start = time.perf_counter()
    results: list[TimedResult[T]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_wrapped, t, i) for i, t in enumerate(tasks)]
        for fut in as_completed(futures):
            results.append(fut.result())
    wallclock_s = time.perf_counter() - wall_start

    sum_durations_s = sum(r.duration_ms for r in results) / 1000.0
    mean_concurrency = (sum_durations_s / wallclock_s) if wallclock_s > 0 else 0.0
    errors = sum(1 for r in results if not r.succeeded)

    return ConcurrentRun(
        results=results,
        wallclock_s=round(wallclock_s, 4),
        mean_concurrency=round(mean_concurrency, 3),
        errors=errors,
    )
