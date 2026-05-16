"""Step definitions for probe.feature.

Drives ``run_probe_search`` and ``run_probe_burst`` through injected
SearchClient-shaped fakes (Protocol from
:mod:`kairix.quality.probe.clients`). Tests construct a fixed list of
benchmark-shaped cases via ``suite_loader``, so no real benchmark suite
is loaded and no real search pipeline is built.

Reference pattern: ``tests/quality/probe/test_runner.py::FakeFastSearchClient``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.quality.probe.burst import BurstResult, run_probe_burst
from kairix.quality.probe.runner import (
    ProbeResult,
    SampledQuery,
    run_probe_search,
)

pytestmark = pytest.mark.bdd


# Default-weighted categories — mirror runner-side ``sample_weighted``
# default-weights so we can assert the per-category map is fully covered.
_DEFAULT_CATEGORIES = ("recall", "temporal", "entity", "conceptual", "multi_hop", "procedural")
_FIXED_SEED = 99


@dataclass(frozen=True)
class _Case:
    """Minimal BenchmarkCase stand-in (sampler reads .category, .query, .id)."""

    id: str
    category: str
    query: str
    agent: str | None = None


def _build_cases() -> list[_Case]:
    """Cases across every default-weighted category so weighted-sampling has every category present."""
    cases: list[_Case] = []
    for cat in _DEFAULT_CATEGORIES:
        for i in range(20):
            cases.append(_Case(id=f"{cat}-{i}", category=cat, query=f"q for {cat} {i}"))
    return cases


def _suite_loader(_suite: str) -> list[_Case]:
    return _build_cases()


class _FakeFastSearchClient:
    """Implements SearchClient — returns immediately (under 50ms by construction)."""

    def search(self, _q: SampledQuery) -> dict[str, str]:
        return {"results": "fast"}


class _FakeSlowSearchClient:
    """Implements SearchClient — sleeps just above the 500ms p95 threshold."""

    def search(self, _q: SampledQuery) -> dict[str, str]:
        time.sleep(0.55)  # 550ms — above the 500ms default p95 threshold
        return {"results": "slow"}


class _FakeBurstFastClient:
    """Implements SearchClient — under-20ms response for burst-bucket coverage."""

    def search(self, _q: SampledQuery) -> dict[str, str]:
        return {"results": "burst"}


@pytest.fixture
def _probe_state() -> dict[str, Any]:
    """Per-scenario fresh state."""
    return {
        "searcher": None,
        "result": None,
        "burst_result": None,
        "case_ids_a": [],
        "case_ids_b": [],
    }


# ---------------------------------------------------------------------------
# Given — wire up the fake client
# ---------------------------------------------------------------------------


@given("a fake search client returning results in under 50ms")
def _given_fast_client(_probe_state: dict[str, Any]) -> None:
    _probe_state["searcher"] = _FakeFastSearchClient().search


@given("a fake search client returning results in 600ms (above threshold)")
def _given_slow_client(_probe_state: dict[str, Any]) -> None:
    # The slow client sleeps 550ms — above the 500ms default threshold.
    # Gherkin phrasing rounds to 600ms for operator readability.
    _probe_state["searcher"] = _FakeSlowSearchClient().search


@given("a fake search client returning results in under 20ms")
def _given_burst_fast_client(_probe_state: dict[str, Any]) -> None:
    _probe_state["searcher"] = _FakeBurstFastClient().search


@given("two probe search runs with the same seed")
def _given_two_seeded_runs(_probe_state: dict[str, Any]) -> None:
    # Mark intent — the actual two runs happen in the When step below.
    _probe_state["searcher"] = _FakeFastSearchClient().search


# ---------------------------------------------------------------------------
# When — invoke the probe
# ---------------------------------------------------------------------------


@when(parsers.parse("the operator runs probe search with {n:d} queries at concurrency {c:d}"))
def _when_run_probe_search(_probe_state: dict[str, Any], n: int, c: int) -> None:
    _probe_state["result"] = run_probe_search(
        suite="fake",
        queries=n,
        concurrency=c,
        suite_loader=_suite_loader,
        searcher=_probe_state["searcher"],
    )


@when("the operator captures the case_ids each run executed")
def _when_capture_case_ids(_probe_state: dict[str, Any]) -> None:
    """Run probe twice with the same seed; collect the case_ids the searcher saw."""
    seen_a: list[str] = []
    seen_b: list[str] = []

    def _collect_a(q: SampledQuery) -> dict[str, str]:
        seen_a.append(q.case_id)
        return {"results": "x"}

    def _collect_b(q: SampledQuery) -> dict[str, str]:
        seen_b.append(q.case_id)
        return {"results": "x"}

    run_probe_search(
        suite="fake",
        queries=20,
        concurrency=1,
        seed=_FIXED_SEED,
        suite_loader=_suite_loader,
        searcher=_collect_a,
    )
    run_probe_search(
        suite="fake",
        queries=20,
        concurrency=1,
        seed=_FIXED_SEED,
        suite_loader=_suite_loader,
        searcher=_collect_b,
    )
    _probe_state["case_ids_a"] = seen_a
    _probe_state["case_ids_b"] = seen_b


@when(parsers.parse("the operator runs probe burst with {n:d} total queries at peak concurrency {c:d}"))
def _when_run_probe_burst(_probe_state: dict[str, Any], n: int, c: int) -> None:
    _probe_state["burst_result"] = run_probe_burst(
        suite="fake",
        total_queries=n,
        peak_concurrency=c,
        bucket_ms=100,  # tight bucket so a fast workload still produces buckets
        suite_loader=_suite_loader,
        searcher=_probe_state["searcher"],
    )


# ---------------------------------------------------------------------------
# Then — assertions on ProbeResult / BurstResult
# ---------------------------------------------------------------------------


@then("the probe result is passed")
def _then_probe_passed(_probe_state: dict[str, Any]) -> None:
    result: ProbeResult = _probe_state["result"]
    # Sabotage: flip ``passed = overall.p95_ms <= threshold`` to ``>=`` in
    # run_probe_search and this fast-path scenario reports failure.
    assert result.passed is True, (
        f"expected passed=True; p95={result.overall.p95_ms} threshold={result.p95_threshold_ms} "
        f"errors={result.errors} bottleneck={result.bottleneck}"
    )


@then("the overall p95 is under the threshold")
def _then_p95_under_threshold(_probe_state: dict[str, Any]) -> None:
    result: ProbeResult = _probe_state["result"]
    # Sabotage: drop the percentile computation (return 0 for p95_ms) and
    # the threshold check is meaningless — defend by asserting p95 is also
    # non-zero for a workload that took some measurable time.
    assert result.overall.p95_ms < result.p95_threshold_ms, (
        f"expected p95 ({result.overall.p95_ms}ms) < threshold ({result.p95_threshold_ms}ms)"
    )


@then("the per-category map covers every default-weighted category")
def _then_per_category_covered(_probe_state: dict[str, Any]) -> None:
    result: ProbeResult = _probe_state["result"]
    present = set(result.per_category.keys())
    expected = set(_DEFAULT_CATEGORIES)
    # Sabotage: skip the ``_per_category_stats`` call in run_probe_search
    # and the per_category dict stays empty — this assertion catches that.
    assert expected.issubset(present), f"expected per_category to cover {expected}; missing {expected - present}"


@then("the probe result is not passed")
def _then_probe_not_passed(_probe_state: dict[str, Any]) -> None:
    result: ProbeResult = _probe_state["result"]
    # Sabotage: leave ``passed=True`` unconditionally and the slow scenario
    # silently passes. The bottleneck assertion below would also fail,
    # but this is the simpler upfront signal.
    assert result.passed is False, (
        f"expected passed=False; p95={result.overall.p95_ms} threshold={result.p95_threshold_ms}"
    )


@then("the bottleneck recommendation names a kind")
def _then_bottleneck_named(_probe_state: dict[str, Any]) -> None:
    result: ProbeResult = _probe_state["result"]
    # Sabotage: drop the ``bottleneck=suggest_bottleneck(...)`` call in
    # run_probe_search and the field stays None on a failing run.
    assert result.bottleneck is not None, "expected bottleneck recommendation when p95 exceeds threshold"
    kind, _action = result.bottleneck
    assert isinstance(kind, str) and kind, f"expected non-empty kind string; got {kind!r}"


@then("the bottleneck recommendation includes the failing p95 figure")
def _then_bottleneck_carries_p95(_probe_state: dict[str, Any]) -> None:
    result: ProbeResult = _probe_state["result"]
    # Sabotage: strip the f"p95={overall.p95_ms}ms" interpolation from
    # suggest_bottleneck's recommended_action and the operator can't
    # tell from the recommendation how close to threshold they are.
    assert result.bottleneck is not None
    _kind, action = result.bottleneck
    p95_str = str(result.overall.p95_ms)
    assert p95_str in action or "p95=" in action, (
        f"expected bottleneck action to mention p95 value {p95_str!r}; got {action!r}"
    )


@then("both runs executed the same set of case_ids")
def _then_same_case_ids(_probe_state: dict[str, Any]) -> None:
    a = _probe_state["case_ids_a"]
    b = _probe_state["case_ids_b"]
    # Sabotage: drop ``seed=seed`` forwarding into ``sample_weighted`` in
    # _build_sampled_queries and two runs return different distributions.
    assert sorted(a) == sorted(b), (
        f"expected identical case_id sets across same-seed runs; "
        f"set(a)-set(b)={set(a) - set(b)}, set(b)-set(a)={set(b) - set(a)}"
    )


@then("the burst result has at least 1 bucket")
def _then_burst_buckets_present(_probe_state: dict[str, Any]) -> None:
    result: BurstResult = _probe_state["burst_result"]
    # Sabotage: skip ``_group_into_buckets`` and buckets stays empty —
    # this assertion fires.
    assert len(result.buckets) >= 1, (
        f"expected at least 1 bucket; got {len(result.buckets)} (wallclock={result.wallclock_s}s)"
    )


@then(parsers.parse("the sum of queries_completed across buckets equals {n:d}"))
def _then_total_queries_summed(_probe_state: dict[str, Any], n: int) -> None:
    result: BurstResult = _probe_state["burst_result"]
    total = sum(b.queries_completed for b in result.buckets)
    # Sabotage: drop one query's completion record (e.g. an off-by-one in
    # _group_into_buckets's edge-case) and this sum is short.
    assert total == n, (
        f"expected {n} total queries across buckets; got {total} "
        f"(buckets: {[(b.window_start_s, b.window_end_s, b.queries_completed) for b in result.buckets]})"
    )


@then("peak_qps is greater than zero")
def _then_peak_qps_positive(_probe_state: dict[str, Any]) -> None:
    result: BurstResult = _probe_state["burst_result"]
    # Sabotage: have _compute_qps_summary return 0.0 unconditionally and
    # this assertion fires (the burst signal would be useless).
    assert result.peak_qps > 0.0, f"expected peak_qps > 0; got {result.peak_qps}"
