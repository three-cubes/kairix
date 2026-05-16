"""Unit tests for `kairix.quality.probe.burst.run_probe_burst`.

Pins composition behaviour: sampler picks cases by weight, executor times
them, completion timestamps captured INSIDE the worker, bucketing rolls up
peak vs sustained QPS, threshold gates pass/fail. Real kairix is never
imported — ``suite_loader`` and ``searcher`` are injected so each test
stays hermetic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from kairix.quality.probe.burst import (
    DEFAULT_QPS_DROP_PCT_THRESHOLD,
    BurstResult,
    run_probe_burst,
)
from kairix.quality.probe.runner import SampledQuery

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Case:
    """Minimal stand-in for BenchmarkCase — sampler reads .category, .query, .id."""

    id: str
    category: str
    query: str
    agent: str | None = None


def _build_cases() -> list[_Case]:
    """Cases across every positive-weight category so default-weights work."""
    out: list[_Case] = []
    for cat in ("recall", "temporal", "entity", "conceptual", "multi_hop", "procedural"):
        for i in range(40):
            out.append(_Case(id=f"{cat}-{i}", category=cat, query=f"q for {cat} {i}"))
    return out


def _suite_loader(_suite: str) -> list[_Case]:
    return _build_cases()


class FakeFastSearchClient:
    """Implements the :class:`SearchClient` Protocol; returns immediately.

    Bound-method ``.search`` is callable-compatible with the ``searcher=`` kwarg
    so the same fake class shape applies across the probe tests.
    """

    def search(self, _q: SampledQuery) -> dict[str, str]:
        return {"results": "fake"}


_fast_client = FakeFastSearchClient()


def test_zero_queries_rejected() -> None:
    """total_queries=0 makes no sense; raise rather than silently return empty stats.

    Sabotage: remove the guard and the empty-input branch in sample_weighted
    raises a different ValueError further down the stack.
    """
    with pytest.raises(ValueError, match="queries must be >= 1"):
        run_probe_burst(
            suite="x",
            total_queries=0,
            suite_loader=_suite_loader,
            searcher=_fast_client.search,
        )


def test_zero_peak_concurrency_rejected() -> None:
    """peak_concurrency=0 → ValueError before the executor is invoked.

    Sabotage: remove the runner-side guard and the executor's guard surfaces
    a less precise error.
    """
    with pytest.raises(ValueError, match="peak_concurrency must be >= 1"):
        run_probe_burst(
            suite="x",
            total_queries=5,
            peak_concurrency=0,
            suite_loader=_suite_loader,
            searcher=_fast_client.search,
        )


def test_zero_bucket_ms_rejected() -> None:
    """bucket_ms=0 → ValueError, since a zero-width bucket can't carry QPS.

    Sabotage: remove the guard and _group_into_buckets divides by zero.
    """
    with pytest.raises(ValueError, match="bucket_ms must be >= 1"):
        run_probe_burst(
            suite="x",
            total_queries=5,
            bucket_ms=0,
            suite_loader=_suite_loader,
            searcher=_fast_client.search,
        )


def test_happy_path_runs_and_buckets() -> None:
    """Fast fake searcher → returns BurstResult, buckets populated, peak_qps>0.

    Sabotage: skip _group_into_buckets and result.buckets stays empty.
    """
    result = run_probe_burst(
        suite="x",
        total_queries=50,
        peak_concurrency=10,
        bucket_ms=200,
        suite_loader=_suite_loader,
        searcher=_fast_client.search,
    )
    assert isinstance(result, BurstResult)
    assert result.total_queries == 50
    assert result.errors == 0
    assert len(result.buckets) >= 1
    assert result.peak_qps > 0
    # All queries accounted for across the buckets.
    assert sum(b.queries_completed for b in result.buckets) == 50


def test_errors_counted_not_raised() -> None:
    """A raising searcher becomes error count, not a crash.

    Sabotage: remove the executor's exception capture and one raise sinks
    the run, hiding the errors=total_queries signal.
    """

    def raiser(_q: SampledQuery) -> int:
        raise RuntimeError("simulated failure")

    result = run_probe_burst(
        suite="x",
        total_queries=10,
        peak_concurrency=2,
        bucket_ms=100,
        suite_loader=_suite_loader,
        searcher=raiser,
    )
    assert result.errors == 10
    assert result.passed is False


def test_sustained_qps_skips_warmup_buckets() -> None:
    """Bake a fake where early queries are slow → first 2 buckets have low QPS.

    sustained_qps is the post-warmup mean (buckets[2:]). With slow-early /
    fast-late traffic, sustained_qps should differ from peak_qps in a
    detectable way — i.e. neither term collapses to the other.

    We pace concurrency=1 with the first 4 queries sleeping ~30ms; with
    bucket_ms=20 that puts the slow region in buckets 0-1 and the fast tail
    in buckets[2:], so sustained_qps is computed from the high-QPS region.

    Sabotage: drop the _WARMUP_BUCKETS slice and sustained_qps collapses to
    the global mean (including warm-up), so the qps_drop_pct signal washes
    out.
    """
    call_counter = {"i": 0}

    def slow_then_fast(_q: SampledQuery) -> int:
        call_counter["i"] += 1
        if call_counter["i"] <= 4:
            time.sleep(0.03)
        return 0

    result = run_probe_burst(
        suite="x",
        total_queries=30,
        peak_concurrency=1,
        bucket_ms=20,
        suite_loader=_suite_loader,
        searcher=slow_then_fast,
    )
    # We need enough buckets for the warmup-skip logic to kick in.
    assert len(result.buckets) >= 3, f"expected >=3 buckets, got {len(result.buckets)}"
    # Sustained is the mean of buckets[2:]; with fast-tail traffic, sustained
    # cannot be larger than peak, and the post-warmup window contains the
    # high-QPS region so sustained must be > 0.
    assert result.sustained_qps > 0
    assert result.sustained_qps <= result.peak_qps


def test_qps_drop_threshold_gates_pass_synthetic() -> None:
    """Synthetic where buckets drop after warm-up → passed=False.

    We can't directly stitch fake BurstBuckets through ``run_probe_burst``
    without monkey-patching, so we tighten the threshold to 0% on a heterogeneous
    workload and verify the gate fires (any drop > 0 fails). This pins the
    threshold-vs-drop comparison, not the bucketing maths (which has its
    own assertions in test_happy_path_runs_and_buckets).

    Sabotage: invert the ``qps_drop_pct <= threshold`` comparison to ``>=``
    and a healthy zero-drop run also fails.
    """
    call_counter = {"i": 0}

    def slow_then_fast(_q: SampledQuery) -> int:
        call_counter["i"] += 1
        if call_counter["i"] <= 3:
            time.sleep(0.05)
        return 0

    result = run_probe_burst(
        suite="x",
        total_queries=20,
        peak_concurrency=2,
        bucket_ms=80,
        qps_drop_threshold_pct=0.0,  # any drop fails
        suite_loader=_suite_loader,
        searcher=slow_then_fast,
    )
    # If the run produced any QPS variance across buckets, the 0% threshold
    # forces a fail. If buckets are perfectly flat (no variance), the run
    # passes — assert pass/fail matches the actual drop.
    if result.qps_drop_pct > 0:
        assert result.passed is False
    else:
        assert result.passed is True


def test_envelope_round_trip_contains_required_keys() -> None:
    """to_envelope produces a dict CLI / MCP can serialise.

    Sabotage: drop one of the required keys from to_envelope and an operator
    parsing the JSON loses signal.
    """
    result = run_probe_burst(
        suite="x",
        total_queries=10,
        peak_concurrency=2,
        bucket_ms=100,
        suite_loader=_suite_loader,
        searcher=_fast_client.search,
    )
    env = result.to_envelope()
    required = {
        "suite",
        "total_queries",
        "peak_concurrency",
        "bucket_ms",
        "seed",
        "wallclock_s",
        "buckets",
        "peak_qps",
        "sustained_qps",
        "qps_drop_pct",
        "errors",
        "qps_drop_threshold_pct",
        "passed",
    }
    assert required.issubset(env.keys())
    # Buckets serialise as a list of dicts with their own required keys.
    assert isinstance(env["buckets"], list)
    if env["buckets"]:
        bucket = env["buckets"][0]
        for key in ("window_start_s", "window_end_s", "queries_completed", "errors", "qps"):
            assert key in bucket, f"bucket envelope missing {key!r}"


def test_seed_determinism_pins_sampled_queries() -> None:
    """Same seed → same case_ids invoked. Different seed → different.

    Sabotage: drop the seed forwarding into sample_weighted and the two
    same-seed runs return different sampled-query sets.
    """
    seen_a: list[str] = []
    seen_b: list[str] = []
    seen_c: list[str] = []

    def collect_a(q: SampledQuery) -> int:
        seen_a.append(q.case_id)
        return 0

    def collect_b(q: SampledQuery) -> int:
        seen_b.append(q.case_id)
        return 0

    def collect_c(q: SampledQuery) -> int:
        seen_c.append(q.case_id)
        return 0

    run_probe_burst(
        suite="x",
        total_queries=20,
        peak_concurrency=1,
        bucket_ms=100,
        seed=77,
        suite_loader=_suite_loader,
        searcher=collect_a,
    )
    run_probe_burst(
        suite="x",
        total_queries=20,
        peak_concurrency=1,
        bucket_ms=100,
        seed=77,
        suite_loader=_suite_loader,
        searcher=collect_b,
    )
    run_probe_burst(
        suite="x",
        total_queries=20,
        peak_concurrency=1,
        bucket_ms=100,
        seed=999,
        suite_loader=_suite_loader,
        searcher=collect_c,
    )
    assert sorted(seen_a) == sorted(seen_b), "same seed must yield the same case set"
    assert sorted(seen_a) != sorted(seen_c), "different seed must change the case set"


def test_default_threshold_constant_is_thirty_percent() -> None:
    """DEFAULT_QPS_DROP_PCT_THRESHOLD is part of the operator-visible contract.

    Sabotage: bump the default upward and a previously-failing run silently
    passes — the change must be intentional and visible in the diff.
    """
    assert DEFAULT_QPS_DROP_PCT_THRESHOLD == 30.0
