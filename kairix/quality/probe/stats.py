"""Latency-distribution stats — p50/p95/p99 + bottleneck heuristics.

Pure functions; no I/O. Inputs are lists of millisecond latencies.

Percentile computation: nearest-rank method (numpy.percentile with
interpolation='nearest'). For N <= 20 we use the exact ordinal so
small-sample p99 doesn't collapse to "single tail observation".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LatencyStats:
    """Per-category or overall latency distribution.

    All values in milliseconds. ``n`` is the sample size; small-n stats
    (p99 with n<10) are reported but should be interpreted with care.
    """

    n: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    mean_ms: float


def latency_stats(latencies_ms: list[float]) -> LatencyStats:
    """Compute LatencyStats from a list of millisecond latencies.

    Returns zero-valued stats when the input is empty (caller decides
    whether that's a failure mode or just "no cases in this category").
    """
    if not latencies_ms:
        return LatencyStats(n=0, p50_ms=0.0, p95_ms=0.0, p99_ms=0.0, min_ms=0.0, max_ms=0.0, mean_ms=0.0)
    s = sorted(latencies_ms)
    n = len(s)

    def _percentile(p: float) -> float:
        # Nearest-rank method, 1-indexed.
        if n == 1:
            return s[0]
        rank = max(1, min(n, round(p * n / 100)))
        return s[rank - 1]

    return LatencyStats(
        n=n,
        p50_ms=round(_percentile(50), 1),
        p95_ms=round(_percentile(95), 1),
        p99_ms=round(_percentile(99), 1),
        min_ms=round(s[0], 1),
        max_ms=round(s[-1], 1),
        mean_ms=round(sum(s) / n, 1),
    )


# Bottleneck-suggestion heuristics — see
# docs/architecture/teaming-concurrency-strategy.md §"What WILL bottleneck first".
# Returns a (suspected_bottleneck, recommended_action) tuple, or None when
# nothing crossed a heuristic threshold.


def suggest_bottleneck(
    overall: LatencyStats,
    mean_concurrency: float,
    requested_concurrency: int,
    p95_threshold_ms: float,
    azure_429_count: int,
) -> tuple[str, str] | None:
    """Heuristic — name the most likely bottleneck given the probe's signals.

    Used by ``--recommend`` to print an actionable line after the run.
    Returns None when no heuristic fires (i.e. the run was healthy or the
    signal is ambiguous).
    """
    # 1. Azure rate-limited — most actionable, check first.
    if azure_429_count > 0:
        return (
            "azure_embed_rate_limit",
            "Azure embed returned 429 — pull lever 1: tune KAIRIX_EMBED_POOL_SIZE + retry/backoff in kairix._azure",
        )

    # 2. Mean concurrency far below requested → workers are blocking on
    #    a shared resource. Threshold of 60% catches obvious contention.
    if requested_concurrency >= 5 and mean_concurrency < requested_concurrency * 0.6:
        return (
            "worker_contention",
            (
                f"mean_concurrency={mean_concurrency:.2f} vs requested={requested_concurrency} — "
                "workers blocking on shared resource. Investigate lock contention via "
                "py-spy or tracemalloc against the live MCP process"
            ),
        )

    # 3. p95 above threshold but concurrency is low → not a load issue, a
    #    deployment / network issue.
    if overall.p95_ms > p95_threshold_ms and requested_concurrency <= 2:
        return (
            "deployment_or_network",
            (
                f"p95={overall.p95_ms}ms exceeds threshold {p95_threshold_ms}ms even at "
                f"concurrency={requested_concurrency} — investigate Azure embed endpoint latency, "
                "vault size, or pipeline cold-start (kairix warm)"
            ),
        )

    # 4. p95 above threshold AND concurrency is high → typical pool exhaustion.
    if overall.p95_ms > p95_threshold_ms:
        return (
            "pool_exhaustion_or_cache_miss",
            (
                f"p95={overall.p95_ms}ms over {p95_threshold_ms}ms at concurrency={requested_concurrency} — "
                "likely pool exhaustion. Pull lever 1 (Azure pool size) or lever 2 (query-result LRU cache). "
                "See docs/architecture/teaming-concurrency-strategy.md §Tier 1"
            ),
        )

    return None
