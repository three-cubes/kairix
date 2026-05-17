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
_WARMUP_BUCKETS_AFTER_COMPLETION = 2  # skip N more buckets once completions begin
_PARTIAL_FINAL_BUCKET_FRACTION = 0.8  # final bucket whose width < 80% of bucket_ms is partial

# Rationale strings surfaced in ``BurstResult.skipped_buckets`` so the operator
# can see *why* a bucket was trimmed from headline stats. F17-safe via constant.
_REASON_PRE_COMPLETION = "pre-completion: no queries finished in this bucket (cold factory)"
_REASON_WARMUP_AFTER_FIRST = "warmup: within the first N buckets after completions begin"
_REASON_PARTIAL_FINAL = "partial-final: bucket width is <80% of bucket_ms (clipped window)"


@dataclass(frozen=True)
class BurstBucket:
    """One time-bucketed window of the burst run."""

    window_start_s: float  # offset from run start
    window_end_s: float  # offset from run start
    queries_completed: int  # completed within this window
    errors: int  # of which were error completions
    qps: float  # queries_completed / (window_end - window_start)


@dataclass(frozen=True)
class SkippedBucket:
    """An index excluded from headline stats with operator-readable rationale."""

    index: int
    reason: str


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
        buckets: per-window throughput slices, in chronological order. The
            full list is retained for transparency even when headline stats
            exclude some of them.
        peak_qps: max QPS across headline-eligible buckets (excludes the
            partial-final bucket when present, since a partial window inflates
            QPS spuriously).
        sustained_qps: mean of headline-eligible buckets starting at
            ``first_completion_bucket_idx + _WARMUP_BUCKETS_AFTER_COMPLETION``,
            ending before any partial-final bucket. Falls back to peak when
            insufficient buckets remain after auto-skip.
        qps_drop_pct: percentage drop from peak to sustained.
        first_completion_bucket_idx: index of the first bucket with at least
            one completed query (the cold-start tail ends here). 0 when no
            warmup contamination was detected.
        partial_final_bucket: True when the final bucket's actual width is
            less than ``_PARTIAL_FINAL_BUCKET_FRACTION`` of ``bucket_ms`` —
            indicating the wallclock ended mid-window.
        skipped_buckets: indices excluded from headline stats with reason
            strings. Empty when ``include_warmup=True``.
        include_warmup: True when the operator opted to disable auto-skip
            (raw timeline mode). Headline stats then span every bucket.
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
    first_completion_bucket_idx: int = 0
    partial_final_bucket: bool = False
    skipped_buckets: list[SkippedBucket] = field(default_factory=list)
    include_warmup: bool = False
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
            "first_completion_bucket_idx": self.first_completion_bucket_idx,
            "partial_final_bucket": self.partial_final_bucket,
            "skipped_buckets": [{"index": s.index, "reason": s.reason} for s in self.skipped_buckets],
            "include_warmup": self.include_warmup,
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
    """Thin shim over :class:`InProcessSearchClient`.

    See :mod:`kairix.quality.probe.clients` for the Protocol contract +
    future MCPHttpSearchClient drop-in (#284).
    """
    from kairix.quality.probe.clients import InProcessSearchClient

    return InProcessSearchClient().search(q)


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


def _first_completion_idx(buckets: list[BurstBucket]) -> int:
    """Return the index of the first bucket with ``queries_completed > 0``.

    Cold-start contamination shows up as leading zero-completion buckets while
    the CLI subprocess pays the factory-build tax. We need to anchor the warmup
    window to "queries actually started completing" rather than wallclock zero.
    Returns 0 when every bucket has completions (no contamination detected).
    """
    for i, b in enumerate(buckets):
        if b.queries_completed > 0:
            return i
    return 0


def _is_partial_final(bucket: BurstBucket, bucket_ms: int) -> bool:
    """True when the final bucket's actual width is < 80% of nominal.

    A bucket clipped by the wallclock cliff hosts only a handful of queries
    in a sub-bucket-ms window, so its qps = N / tiny_width is artificially
    inflated. We detect that geometry and exclude it from headline peak/
    sustained to keep the operator's "peak_qps" intuition honest.
    """
    width = bucket.window_end_s - bucket.window_start_s
    nominal = bucket_ms / 1000.0
    return width < nominal * _PARTIAL_FINAL_BUCKET_FRACTION


def _identify_skipped_buckets(
    buckets: list[BurstBucket],
    bucket_ms: int,
    first_completion: int,
) -> tuple[list[SkippedBucket], bool]:
    """Return (skipped_buckets, partial_final_bucket).

    Buckets are skipped in three layers:
      1. Every leading bucket with ``queries_completed == 0`` (cold factory).
      2. ``_WARMUP_BUCKETS_AFTER_COMPLETION`` buckets after completions begin
         (steady-state hasn't been reached yet — old hand-tuned constant).
      3. The final bucket if its width is < 80% of ``bucket_ms`` (partial).
    """
    skipped: list[SkippedBucket] = []
    n = len(buckets)
    partial = bool(buckets) and _is_partial_final(buckets[-1], bucket_ms)
    final_idx = n - 1 if buckets else -1
    for i in range(min(first_completion, n)):
        # Pre-completion takes precedence over partial-final at the same idx —
        # cold start is the bigger signal. (Hits when the run produced zero
        # completions; n_buckets degenerates to 1.)
        skipped.append(SkippedBucket(index=i, reason=_REASON_PRE_COMPLETION))
    warmup_end = min(first_completion + _WARMUP_BUCKETS_AFTER_COMPLETION, n)
    for i in range(first_completion, warmup_end):
        # If this warmup bucket is *also* the partial-final, prefer the
        # partial-final reason — clipping is the more actionable diagnosis
        # (operator can re-run with a longer wallclock to fix it).
        if partial and i == final_idx:
            skipped.append(SkippedBucket(index=i, reason=_REASON_PARTIAL_FINAL))
        else:
            skipped.append(SkippedBucket(index=i, reason=_REASON_WARMUP_AFTER_FIRST))
    if partial and final_idx >= warmup_end:
        skipped.append(SkippedBucket(index=final_idx, reason=_REASON_PARTIAL_FINAL))
    return skipped, partial


def _summarise_from_eligible(eligible: list[BurstBucket]) -> tuple[float, float, float]:
    """Compute (peak_qps, sustained_qps, qps_drop_pct) from headline-eligible buckets.

    ``eligible`` is the bucket list with auto-skip already applied (cold start,
    post-completion warmup, partial-final all removed). When empty we return
    zeros — the caller falls back to the peak-equals-sustained branch.
    """
    if not eligible:
        return 0.0, 0.0, 0.0
    peak_qps = max(b.qps for b in eligible)
    sustained_qps = sum(b.qps for b in eligible) / len(eligible)
    if peak_qps <= 0:
        return round(peak_qps, 3), round(sustained_qps, 3), 0.0
    qps_drop_pct = max(0.0, (peak_qps - sustained_qps) / peak_qps * 100.0)
    return round(peak_qps, 3), round(sustained_qps, 3), round(qps_drop_pct, 2)


def _compute_qps_summary(
    buckets: list[BurstBucket],
    bucket_ms: int,
    *,
    include_warmup: bool,
) -> tuple[float, float, float, int, bool, list[SkippedBucket]]:
    """Return (peak_qps, sustained_qps, qps_drop_pct, first_completion_idx,
    partial_final_bucket, skipped_buckets).

    Headline stats auto-skip pre-completion (cold-start) and partial-final
    buckets unless ``include_warmup=True`` (raw timeline mode). The full
    ``buckets`` list is always preserved on the result for operator inspection.
    """
    if not buckets:
        return 0.0, 0.0, 0.0, 0, False, []

    first_completion = _first_completion_idx(buckets)
    skipped, partial = _identify_skipped_buckets(buckets, bucket_ms, first_completion)

    if include_warmup:
        # Operator opted to see the raw timeline; surface diagnostics but use
        # every bucket for headline numbers (matches pre-auto-skip behaviour).
        peak, sustained, drop = _summarise_from_eligible(buckets)
        return peak, sustained, drop, first_completion, partial, []

    skipped_idxs = {s.index for s in skipped}
    eligible = [b for i, b in enumerate(buckets) if i not in skipped_idxs]
    if not eligible:
        # Auto-skip consumed every bucket — fall back to raw peak so the
        # operator still sees a number rather than a silent zero.
        peak_raw = max(b.qps for b in buckets)
        return (
            round(peak_raw, 3),
            round(peak_raw, 3),
            0.0,
            first_completion,
            partial,
            skipped,
        )
    peak, sustained, drop = _summarise_from_eligible(eligible)
    return peak, sustained, drop, first_completion, partial, skipped


def run_probe_burst(
    suite: str,
    total_queries: int = 200,
    peak_concurrency: int = 20,
    bucket_ms: int = 500,
    seed: int = 0,
    qps_drop_threshold_pct: float = DEFAULT_QPS_DROP_PCT_THRESHOLD,
    include_warmup: bool = False,
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
        include_warmup: when True, disable auto-skip and compute headline
            stats from every bucket (raw timeline mode). Default False — most
            operators want pre-completion + partial-final buckets excluded so
            ``peak_qps`` / ``sustained_qps`` reflect steady-state behaviour.
        suite_loader: test seam — returns list[BenchmarkCase] for a suite name.
        searcher: test seam — runs one SampledQuery through a search pipeline.

    Returns:
        BurstResult with the full per-bucket timeline, headline peak/sustained
        summary (auto-skip applied unless ``include_warmup=True``), the
        bucket indices skipped with rationale, error count, and pass/fail
        verdict.

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
    (
        peak_qps,
        sustained_qps,
        qps_drop_pct,
        first_completion_idx,
        partial_final,
        skipped,
    ) = _compute_qps_summary(buckets, bucket_ms, include_warmup=include_warmup)
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
        first_completion_bucket_idx=first_completion_idx,
        partial_final_bucket=partial_final,
        skipped_buckets=skipped,
        include_warmup=include_warmup,
        errors=run.errors,
        qps_drop_threshold_pct=qps_drop_threshold_pct,
        passed=passed,
    )
