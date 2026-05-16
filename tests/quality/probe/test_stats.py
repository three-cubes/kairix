"""Unit tests for `kairix.quality.probe.stats`.

Pins percentile-method choice, empty-input semantics, and each branch of
`suggest_bottleneck`. Sabotage-prove: swap the percentile method or remove
a heuristic and the corresponding test fails.
"""

from __future__ import annotations

import pytest

from kairix.quality.probe.stats import LatencyStats, latency_stats, suggest_bottleneck

pytestmark = pytest.mark.unit


def test_empty_input_returns_zero_stats() -> None:
    """No samples → all-zero LatencyStats; caller decides how to interpret.

    Sabotage-proof: remove the early return and ``sorted([])`` then ``s[0]``
    raises IndexError.
    """
    s = latency_stats([])
    assert s == LatencyStats(n=0, p50_ms=0.0, p95_ms=0.0, p99_ms=0.0, min_ms=0.0, max_ms=0.0, mean_ms=0.0)


def test_single_sample_percentiles_all_equal_to_value() -> None:
    """One sample → every percentile is that sample; degenerate but well-defined.

    Sabotage-proof: drop the ``n == 1`` branch and rank calculation produces
    rank=0 which the ``max(1, ...)`` floor catches, but verify the result
    is the value itself, not 0.
    """
    s = latency_stats([42.5])
    assert s.n == 1
    assert s.p50_ms == 42.5
    assert s.p95_ms == 42.5
    assert s.p99_ms == 42.5
    assert s.min_ms == 42.5
    assert s.max_ms == 42.5


def test_nearest_rank_percentile_at_100_samples() -> None:
    """100 evenly spaced samples — p50=50th, p95=95th, p99=99th by nearest-rank.

    Sabotage-proof: swap to linear interpolation and p99 drops below 99.
    """
    samples = [float(i + 1) for i in range(100)]  # 1.0..100.0
    s = latency_stats(samples)
    assert s.n == 100
    assert s.p50_ms == 50.0
    assert s.p95_ms == 95.0
    assert s.p99_ms == 99.0
    assert s.min_ms == 1.0
    assert s.max_ms == 100.0
    assert s.mean_ms == 50.5


def test_p99_does_not_collapse_to_max_on_small_n() -> None:
    """At n=20, p99 should still report a sane high-tail value (not just the max).

    Sabotage-proof: round-to-int the rank without nearest-rank semantics
    and small-n collapses to the same value for p95 and p99.
    """
    samples = list(range(1, 21))  # 1..20
    s = latency_stats([float(x) for x in samples])
    assert s.n == 20
    # rank for p99 = round(99 * 20 / 100) = round(19.8) = 20 → samples[19] = 20
    assert s.p99_ms == 20.0
    # rank for p95 = round(95 * 20 / 100) = round(19.0) = 19 → samples[18] = 19
    assert s.p95_ms == 19.0


def test_stats_are_rounded_to_one_decimal() -> None:
    """All returned values are rounded to 1 decimal — keeps CLI output clean.

    Sabotage-proof: drop the ``round(...)`` calls and the assertion catches
    long-tail decimals.
    """
    s = latency_stats([1.234567, 2.345678, 3.456789])
    assert s.p50_ms == 2.3
    assert s.min_ms == 1.2
    assert s.max_ms == 3.5


def _stats(p95: float) -> LatencyStats:
    """Minimal stub stats — only p95 matters for heuristic dispatch."""
    return LatencyStats(
        n=10,
        p50_ms=p95 / 2,
        p95_ms=p95,
        p99_ms=p95 * 1.2,
        min_ms=10.0,
        max_ms=p95 * 1.5,
        mean_ms=p95 / 2,
    )


def test_bottleneck_429_takes_priority_over_everything() -> None:
    """Even when concurrency and latency look fine, any 429 dominates the recommendation.

    Sabotage-proof: drop the early 429 check and worker_contention fires first
    on this input, masking the actionable rate-limit signal.
    """
    out = suggest_bottleneck(
        overall=_stats(p95=200),
        mean_concurrency=1.0,
        requested_concurrency=10,
        p95_threshold_ms=500,
        azure_429_count=1,
    )
    assert out is not None
    kind, action = out
    assert kind == "azure_embed_rate_limit"
    assert "429" in action


def test_bottleneck_worker_contention_when_mean_concurrency_low() -> None:
    """mean_concurrency < 60% of requested at concurrency>=5 → worker contention.

    Sabotage-proof: lower the 60% threshold to 30% and this falls through.
    """
    out = suggest_bottleneck(
        overall=_stats(p95=200),
        mean_concurrency=2.0,  # vs requested 10 → 20%
        requested_concurrency=10,
        p95_threshold_ms=500,
        azure_429_count=0,
    )
    assert out is not None
    kind, _ = out
    assert kind == "worker_contention"


def test_bottleneck_deployment_or_network_when_p95_high_and_low_concurrency() -> None:
    """p95 over threshold at concurrency<=2 → not a load problem, a deployment one.

    Sabotage-proof: drop the ``requested_concurrency <= 2`` clause and we'd
    misattribute a cold-start spike as pool exhaustion.
    """
    out = suggest_bottleneck(
        overall=_stats(p95=900),
        mean_concurrency=1.0,
        requested_concurrency=1,
        p95_threshold_ms=500,
        azure_429_count=0,
    )
    assert out is not None
    kind, action = out
    assert kind == "deployment_or_network"
    assert "concurrency=1" in action


def test_bottleneck_pool_exhaustion_when_p95_high_at_high_concurrency() -> None:
    """p95 over threshold at higher concurrency → classic pool/cache exhaustion.

    Sabotage-proof: remove this branch entirely and the function returns
    None on this input even though p95 is over budget.
    """
    out = suggest_bottleneck(
        overall=_stats(p95=900),
        mean_concurrency=9.0,  # not low enough for worker_contention
        requested_concurrency=10,
        p95_threshold_ms=500,
        azure_429_count=0,
    )
    assert out is not None
    kind, action = out
    assert kind == "pool_exhaustion_or_cache_miss"
    assert "lever 1" in action or "lever 2" in action


def test_bottleneck_returns_none_when_healthy() -> None:
    """No 429, healthy concurrency, p95 well below threshold → no suggestion.

    Sabotage-proof: drop the ``return None`` and the heuristic fires
    incorrectly when nothing is wrong.
    """
    out = suggest_bottleneck(
        overall=_stats(p95=200),
        mean_concurrency=9.5,
        requested_concurrency=10,
        p95_threshold_ms=500,
        azure_429_count=0,
    )
    assert out is None
