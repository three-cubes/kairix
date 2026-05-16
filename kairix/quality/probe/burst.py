"""Burst-load probe — rapid query injection, measure throughput drop after warm-up.

The signal probe search misses: even when sustained-concurrency p95 is fine, a
system can leak resources or evict caches under burst load such that QPS
collapses past the warm-up window. Burst measures queries-per-second over
time-bucketed windows, surfacing post-warmup degradation that p95 averages out.

Use case: decide whether to ship the Tier 1 query-cache lever (#281) when probe
search alone doesn't show cache pressure, OR to detect resource leaks under
burst that soak's repeat-shape wouldn't catch.

Module API:
    from kairix.quality.probe.burst import run_probe_burst
    result = run_probe_burst(suite="reflib", total_queries=200, peak_concurrency=20)
    if not result.passed:
        print(f"qps_drop={result.qps_drop_pct}% (peak={result.peak_qps} sustained={result.sustained_qps})")

Test seam: ``suite_loader`` and ``searcher`` are injectable (same shape as
``runner.py``) so tests stay hermetic with fakes from tests/fakes.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kairix.quality.probe.executor import run_concurrent
from kairix.quality.probe.runner import SampledQuery
from kairix.quality.probe.sampler import sample_weighted

DEFAULT_QPS_DROP_PCT_THRESHOLD = 30.0  # % — sustained must stay within this of peak
_WARMUP_BUCKETS = 2  # skip the first N buckets when computing sustained_qps


@dataclass(frozen=True)
class BurstBucket:
    """One time-bucketed window of the burst run."""

    window_start_s: float  # offset from run start
    window_end_s: float  # offset from run start
    queries_completed: int  # completed within this window
    errors: int  # of which were error completions
    qps: float  # queries_completed / (window_end - window_start)


@dataclass(frozen=True)
class BurstResult:
    """Outcome of one ``run_probe_burst`` call. Round-trippable via ``to_envelope``.

    Attributes:
        suite: workload identifier (suite name or path).
        total_queries: number of queries actually executed.
        peak_concurrency: max worker count used in the burst.
        bucket_ms: time-bucket width in milliseconds.
        seed: deterministic sample/shuffle seed.
        wallclock_s: total elapsed time across the run.
        buckets: per-window throughput slices, in chronological order.
        peak_qps: max QPS across all buckets.
        sustained_qps: mean of post-warmup buckets (skips first
            ``_WARMUP_BUCKETS``). Falls back to peak_qps when fewer than
            ``_WARMUP_BUCKETS + 1`` buckets exist.
        qps_drop_pct: percentage drop from peak to sustained.
        errors: number of tasks that raised inside the executor.
        qps_drop_threshold_pct: pass-fail cap (default 30%).
        passed: True when qps_drop_pct <= threshold AND errors == 0.
    """

    suite: str
    total_queries: int
    peak_concurrency: int
    bucket_ms: int
    seed: int
    wallclock_s: float
    buckets: list[BurstBucket] = field(default_factory=list)
    peak_qps: float = 0.0
    sustained_qps: float = 0.0
    qps_drop_pct: float = 0.0
    errors: int = 0
    qps_drop_threshold_pct: float = DEFAULT_QPS_DROP_PCT_THRESHOLD
    passed: bool = True

    def to_envelope(self) -> dict[str, Any]:
        """Project to the JSON envelope CLI ``--json`` + MCP would emit."""
        return {
            "suite": self.suite,
            "total_queries": self.total_queries,
            "peak_concurrency": self.peak_concurrency,
            "bucket_ms": self.bucket_ms,
            "seed": self.seed,
            "wallclock_s": self.wallclock_s,
            "buckets": [_bucket_to_envelope(b) for b in self.buckets],
            "peak_qps": self.peak_qps,
            "sustained_qps": self.sustained_qps,
            "qps_drop_pct": self.qps_drop_pct,
            "errors": self.errors,
            "qps_drop_threshold_pct": self.qps_drop_threshold_pct,
            "passed": self.passed,
        }


def _bucket_to_envelope(b: BurstBucket) -> dict[str, float | int]:
    return {
        "window_start_s": b.window_start_s,
        "window_end_s": b.window_end_s,
        "queries_completed": b.queries_completed,
        "errors": b.errors,
        "qps": b.qps,
    }


def _default_suite_loader(suite: str) -> list[Any]:  # pragma: no cover — production path
    """Resolve a suite name → list of BenchmarkCase. Production-only seam.

    Mirrors ``run_probe_search``'s loader so the operator gets the same
    name-shortcut UX (#222). Tests inject a fake list of cases.
    """
    from kairix.quality.benchmark.suite import load_suite, resolve_suite_path

    suite_path = resolve_suite_path(suite)
    return load_suite(str(suite_path)).cases


def _default_search_fn(q: SampledQuery) -> Any:  # pragma: no cover — production path
    """Run one search via the production pipeline. Production-only seam."""
    from kairix.core.factory import build_search_pipeline

    pipeline = build_search_pipeline()
    return pipeline.search(query=q.query, agent=q.agent)


def _build_sampled_queries(cases: list[Any], total_queries: int, seed: int) -> list[SampledQuery]:
    sampled_cases = sample_weighted(cases, n=total_queries, seed=seed)
    return [
        SampledQuery(
            case_id=getattr(c, "id", f"case_{i}"),
            category=c.category,
            query=c.query,
            agent=getattr(c, "agent", None),
        )
        for i, c in enumerate(sampled_cases)
    ]


def _build_timed_tasks(
    sampled: list[SampledQuery],
    fn: Callable[[SampledQuery], Any],
    run_start: float,
) -> list[Callable[[], tuple[Any, float]]]:
    """Wrap each sampled query as a callable that returns (result, completion_offset_s).

    The completion timestamp is captured INSIDE the worker, in monotonic time,
    relative to the shared ``run_start`` perf_counter — so bucket assignment
    is wall-time accurate regardless of as_completed ordering.
    """

    def _make(sq: SampledQuery) -> Callable[[], tuple[Any, float]]:
        def _task() -> tuple[Any, float]:
            value = fn(sq)
            completion = time.perf_counter() - run_start
            return value, completion

        return _task

    return [_make(sq) for sq in sampled]


def _group_into_buckets(
    completions: list[tuple[float, bool]],
    bucket_ms: int,
    wallclock_s: float,
) -> list[BurstBucket]:
    """Partition (completion_offset_s, succeeded) entries into bucket_ms-wide windows.

    Bucket edges are deterministic: [0, bucket_s), [bucket_s, 2*bucket_s), ...
    The last bucket extends to ``wallclock_s`` to capture any tail completions.
    """
    bucket_s = bucket_ms / 1000.0
    if wallclock_s <= 0:
        return []
    n_buckets = max(1, int(wallclock_s // bucket_s) + (1 if wallclock_s % bucket_s > 0 else 0))

    counts = [0] * n_buckets
    error_counts = [0] * n_buckets
    for offset_s, succeeded in completions:
        idx = min(int(offset_s // bucket_s), n_buckets - 1)
        counts[idx] += 1
        if not succeeded:
            error_counts[idx] += 1

    buckets: list[BurstBucket] = []
    for i in range(n_buckets):
        start = i * bucket_s
        end = min((i + 1) * bucket_s, wallclock_s)
        width = end - start
        qps = counts[i] / width if width > 0 else 0.0
        buckets.append(
            BurstBucket(
                window_start_s=round(start, 4),
                window_end_s=round(end, 4),
                queries_completed=counts[i],
                errors=error_counts[i],
                qps=round(qps, 3),
            )
        )
    return buckets


def _compute_qps_summary(buckets: list[BurstBucket]) -> tuple[float, float, float]:
    """Return (peak_qps, sustained_qps, qps_drop_pct).

    sustained_qps = mean(qps for buckets[_WARMUP_BUCKETS:]). When fewer than
    _WARMUP_BUCKETS + 1 buckets exist, sustained falls back to peak so
    qps_drop_pct stays 0 (insufficient data to claim degradation).
    """
    if not buckets:
        return 0.0, 0.0, 0.0
    peak_qps = max(b.qps for b in buckets)
    post_warmup = buckets[_WARMUP_BUCKETS:]
    if not post_warmup:
        return peak_qps, peak_qps, 0.0
    sustained_qps = sum(b.qps for b in post_warmup) / len(post_warmup)
    if peak_qps <= 0:
        return peak_qps, sustained_qps, 0.0
    qps_drop_pct = max(0.0, (peak_qps - sustained_qps) / peak_qps * 100.0)
    return round(peak_qps, 3), round(sustained_qps, 3), round(qps_drop_pct, 2)


def run_probe_burst(
    suite: str,
    total_queries: int = 200,
    peak_concurrency: int = 20,
    bucket_ms: int = 500,
    seed: int = 0,
    qps_drop_threshold_pct: float = DEFAULT_QPS_DROP_PCT_THRESHOLD,
    *,
    suite_loader: Callable[[str], list[Any]] | None = None,
    searcher: Callable[[SampledQuery], Any] | None = None,
) -> BurstResult:
    """Inject ``total_queries`` as fast as possible and measure throughput drop.

    Args:
        suite: benchmark suite name (e.g. ``reflib``) or explicit path.
        total_queries: total queries to inject (>=1).
        peak_concurrency: thread-pool size (>=1).
        bucket_ms: time-bucket width in milliseconds (>=1).
        seed: deterministic sample + shuffle seed.
        qps_drop_threshold_pct: pass-fail cap on sustained-from-peak QPS drop.
        suite_loader: test seam — returns list[BenchmarkCase] for a suite name.
        searcher: test seam — runs one SampledQuery through a search pipeline.

    Returns:
        BurstResult with per-bucket QPS, peak/sustained summary, error count,
        and pass/fail verdict.

    Raises:
        ValueError: when total_queries<1, peak_concurrency<1, or bucket_ms<1.
    """
    if total_queries < 1:
        raise ValueError(f"queries must be >= 1; got {total_queries}")
    if peak_concurrency < 1:
        raise ValueError(f"peak_concurrency must be >= 1; got {peak_concurrency}")
    if bucket_ms < 1:
        raise ValueError(f"bucket_ms must be >= 1; got {bucket_ms}")

    loader = suite_loader or _default_suite_loader
    fn = searcher or _default_search_fn

    cases = loader(suite)
    sampled = _build_sampled_queries(cases, total_queries, seed)

    run_start = time.perf_counter()
    tasks = _build_timed_tasks(sampled, fn, run_start)
    run = run_concurrent(tasks, concurrency=peak_concurrency)
    wallclock_s = round(time.perf_counter() - run_start, 4)

    # Completions: for failed tasks the executor returned None (no tuple), so
    # we approximate the completion time as the executor's wallclock end —
    # they still count toward the bucket near the run's tail. This matches
    # the brief's "errors counted, not raised" contract.
    completions: list[tuple[float, bool]] = []
    for r in run.results:
        if r.succeeded and isinstance(r.result, tuple) and len(r.result) == 2:
            _value, offset_s = r.result
            completions.append((float(offset_s), True))
        else:
            completions.append((wallclock_s, False))

    buckets = _group_into_buckets(completions, bucket_ms, wallclock_s)
    peak_qps, sustained_qps, qps_drop_pct = _compute_qps_summary(buckets)
    passed = qps_drop_pct <= qps_drop_threshold_pct and run.errors == 0

    return BurstResult(
        suite=suite,
        total_queries=len(sampled),
        peak_concurrency=peak_concurrency,
        bucket_ms=bucket_ms,
        seed=seed,
        wallclock_s=wallclock_s,
        buckets=buckets,
        peak_qps=peak_qps,
        sustained_qps=sustained_qps,
        qps_drop_pct=qps_drop_pct,
        errors=run.errors,
        qps_drop_threshold_pct=qps_drop_threshold_pct,
        passed=passed,
    )
