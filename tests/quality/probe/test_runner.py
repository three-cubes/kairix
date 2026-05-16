"""Unit tests for `kairix.quality.probe.runner.run_probe_search`.

Pins composition behaviour: sampler picks cases by weight, executor times
them, stats roll up overall + per-category, bottleneck heuristic fires
appropriately. Real kairix is never imported — ``suite_loader`` and
``searcher`` are injected so each test stays hermetic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from kairix.quality.probe.runner import (
    DEFAULT_P95_THRESHOLD_MS,
    ProbeResult,
    SampledQuery,
    run_probe_search,
)

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
        for i in range(20):
            out.append(_Case(id=f"{cat}-{i}", category=cat, query=f"q for {cat} {i}"))
    return out


def _suite_loader(_suite: str) -> list[_Case]:
    return _build_cases()


def _fast_search_fn(_q: SampledQuery) -> dict[str, str]:
    """A search that returns immediately — keeps tests under 1s."""
    return {"results": "fake"}


def _slow_search_fn(_q: SampledQuery) -> dict[str, str]:
    """A search that always exceeds the default p95 threshold."""
    time.sleep(0.55)  # > 500ms threshold
    return {"results": "slow"}


def test_queries_less_than_one_rejected() -> None:
    """queries=0 makes no sense; raise rather than silently return empty stats.

    Sabotage-proof: remove the guard and the empty-input branch in
    latency_stats silently passes 0-stats through.
    """
    with pytest.raises(ValueError, match="queries must be >= 1"):
        run_probe_search(suite="x", queries=0, suite_loader=_suite_loader, searcher=_fast_search_fn)


def test_concurrency_less_than_one_rejected() -> None:
    """concurrency=0 forwards to the executor's guard; the runner rejects early too.

    Sabotage-proof: remove the runner-side guard and the error surfaces
    deeper in the stack as an executor ValueError instead.
    """
    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        run_probe_search(suite="x", queries=5, concurrency=0, suite_loader=_suite_loader, searcher=_fast_search_fn)


def test_passes_when_fast_search_under_threshold() -> None:
    """Fast fake search → p95 well under 500ms → passed=True, no bottleneck.

    Sabotage-proof: flip the ``passed=`` calculation to ``>= threshold`` and
    healthy runs report failure.
    """
    result = run_probe_search(
        suite="x",
        queries=20,
        concurrency=2,
        suite_loader=_suite_loader,
        searcher=_fast_search_fn,
    )
    assert isinstance(result, ProbeResult)
    assert result.queries == 20
    assert result.passed is True
    assert result.errors == 0
    assert result.overall.p95_ms < DEFAULT_P95_THRESHOLD_MS
    assert result.bottleneck is None


def test_fails_when_p95_exceeds_threshold() -> None:
    """Slow search → p95 > 500ms → passed=False AND bottleneck recommendation set.

    Sabotage-proof: drop the bottleneck call and result.bottleneck stays None.
    """
    result = run_probe_search(
        suite="x",
        queries=4,
        concurrency=4,
        suite_loader=_suite_loader,
        searcher=_slow_search_fn,
    )
    assert result.passed is False
    assert result.overall.p95_ms >= DEFAULT_P95_THRESHOLD_MS
    assert result.bottleneck is not None


def test_per_category_stats_populated() -> None:
    """Sampling across categories produces per_category[cat] for each present cat.

    Sabotage-proof: skip ``_per_category_stats`` and the dict stays empty.
    """
    result = run_probe_search(
        suite="x",
        queries=60,  # large enough that every default-weight category lands at least one
        concurrency=4,
        suite_loader=_suite_loader,
        searcher=_fast_search_fn,
    )
    expected_cats = {"recall", "temporal", "entity", "conceptual", "multi_hop", "procedural"}
    assert set(result.per_category.keys()) == expected_cats
    for cat, stats in result.per_category.items():
        assert stats.n >= 1, f"category {cat} had no samples"
        assert stats.p50_ms >= 0


def test_seed_determinism_pins_query_order() -> None:
    """Same seed → same sampled-case sequence → same query set executed.

    Sabotage-proof: drop the seed forwarding into ``sample_weighted`` and two
    runs return different per_category distributions.
    """
    seen_ids_a: list[str] = []
    seen_ids_b: list[str] = []

    def collect_a(q: SampledQuery) -> int:
        seen_ids_a.append(q.case_id)
        return 0

    def collect_b(q: SampledQuery) -> int:
        seen_ids_b.append(q.case_id)
        return 0

    run_probe_search(suite="x", queries=20, concurrency=1, seed=99, suite_loader=_suite_loader, searcher=collect_a)
    run_probe_search(suite="x", queries=20, concurrency=1, seed=99, suite_loader=_suite_loader, searcher=collect_b)
    assert sorted(seen_ids_a) == sorted(seen_ids_b)


def test_envelope_round_trip_contains_required_keys() -> None:
    """to_envelope produces a dict CLI / MCP can serialise.

    Sabotage-proof: drop ``mean_concurrency`` from the envelope and an
    operator parsing the JSON loses the signal the bottleneck heuristic
    relied on.
    """
    result = run_probe_search(
        suite="x",
        queries=5,
        concurrency=2,
        suite_loader=_suite_loader,
        searcher=_fast_search_fn,
    )
    env = result.to_envelope()
    required = {
        "suite",
        "queries",
        "concurrency",
        "seed",
        "overall",
        "per_category",
        "mean_concurrency",
        "wallclock_s",
        "azure_429_count",
        "errors",
        "p95_threshold_ms",
        "passed",
        "bottleneck",
    }
    assert required.issubset(env.keys())
    assert env["bottleneck"] is None  # fast path → healthy → no recommendation


def test_envelope_serialises_bottleneck_as_dict_when_present() -> None:
    """When bottleneck fires, envelope contains a dict with kind + recommended_action.

    Sabotage-proof: leave bottleneck as the bare tuple and JSON serialisation
    in the CLI fails (tuples become arrays and the agent loses the field names).
    """
    result = run_probe_search(
        suite="x",
        queries=4,
        concurrency=4,
        suite_loader=_suite_loader,
        searcher=_slow_search_fn,
    )
    env = result.to_envelope()
    assert env["bottleneck"] is not None
    assert "kind" in env["bottleneck"]
    assert "recommended_action" in env["bottleneck"]


def test_errors_in_search_fn_are_counted_not_raised() -> None:
    """A raising search_fn becomes an error count, not a crash.

    Sabotage-proof: remove the executor's exception capture and one raising
    case sinks the whole run.
    """

    def raiser(_q: SampledQuery) -> int:
        raise RuntimeError("simulated backend failure")

    result = run_probe_search(
        suite="x",
        queries=5,
        concurrency=2,
        suite_loader=_suite_loader,
        searcher=raiser,
    )
    assert result.errors == 5
    assert result.passed is False  # any error blocks passing
