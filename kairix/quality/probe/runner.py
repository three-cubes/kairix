"""Search-probe runner — composes sampler + executor + stats into ProbeResult.

Module API:
    from kairix.quality.probe import run_probe_search
    result = run_probe_search(suite="reflib", queries=100, concurrency=5)
    if not result.passed:
        for cat, lat in result.per_category.items():
            print(f"{cat}: p95={lat.p95_ms}ms")

The runner is the seam: it knows how to load a suite, how to build a search
function from the production factory, and how to aggregate everything into
a single ProbeResult envelope suitable for CLI / MCP / JSON output.

Test seam: ``suite_loader`` and ``searcher`` are injectable so tests can run
fully hermetically with fakes from tests/fakes.py. Production callers leave
them None and get the bundled suite resolution + ``build_search_pipeline``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kairix.quality.probe.executor import run_concurrent
from kairix.quality.probe.sampler import sample_weighted
from kairix.quality.probe.stats import LatencyStats, latency_stats, suggest_bottleneck

DEFAULT_P95_THRESHOLD_MS = 500.0


@dataclass(frozen=True)
class SampledQuery:
    """One query sampled from a suite, ready for execution.

    Kept minimal so the executor's task callable can close over it cheaply
    and the JSON envelope stays small.
    """

    case_id: str
    category: str
    query: str
    agent: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one ``run_probe_search`` call.

    Attributes:
        suite: workload identifier (suite name or path).
        queries: number of queries actually executed.
        concurrency: thread-pool size used (1 = sequential through pool).
        seed: deterministic sample/shuffle seed.
        overall: latency distribution across all queries.
        per_category: latency distribution per non-zero-weight category.
        mean_concurrency: Little's-Law mean concurrency (see executor docs).
        wallclock_s: total elapsed time across the run.
        azure_429_count: count of Azure rate-limited responses (0 until
            wired via an explicit error-classifier injection — present in
            envelope so the CLI / bottleneck heuristic can consume it now).
        errors: number of tasks that raised inside the executor.
        p95_threshold_ms: target the gate compares against.
        passed: True when overall p95 is within p95_threshold_ms AND no errors.
        bottleneck: ``(kind, recommended_action)`` from ``suggest_bottleneck``,
            or None when the run is healthy.
    """

    suite: str
    queries: int
    concurrency: int
    seed: int
    overall: LatencyStats
    per_category: dict[str, LatencyStats] = field(default_factory=dict)
    mean_concurrency: float = 0.0
    wallclock_s: float = 0.0
    azure_429_count: int = 0
    errors: int = 0
    p95_threshold_ms: float = DEFAULT_P95_THRESHOLD_MS
    passed: bool = True
    bottleneck: tuple[str, str] | None = None

    def to_envelope(self) -> dict[str, Any]:
        """Project to the JSON envelope CLI ``--json`` + MCP would emit."""
        return {
            "suite": self.suite,
            "queries": self.queries,
            "concurrency": self.concurrency,
            "seed": self.seed,
            "overall": _stats_to_envelope(self.overall),
            "per_category": {cat: _stats_to_envelope(s) for cat, s in self.per_category.items()},
            "mean_concurrency": self.mean_concurrency,
            "wallclock_s": self.wallclock_s,
            "azure_429_count": self.azure_429_count,
            "errors": self.errors,
            "p95_threshold_ms": self.p95_threshold_ms,
            "passed": self.passed,
            "bottleneck": (
                {"kind": self.bottleneck[0], "recommended_action": self.bottleneck[1]} if self.bottleneck else None
            ),
        }


def _stats_to_envelope(s: LatencyStats) -> dict[str, float | int]:
    return {
        "n": s.n,
        "p50_ms": s.p50_ms,
        "p95_ms": s.p95_ms,
        "p99_ms": s.p99_ms,
        "min_ms": s.min_ms,
        "max_ms": s.max_ms,
        "mean_ms": s.mean_ms,
    }


def _default_suite_loader(suite: str) -> list[Any]:  # pragma: no cover — production path
    """Resolve a suite name → list of BenchmarkCase. Production-only seam.

    Mirrors ``kairix benchmark run --suite SUITE`` resolution so the operator
    gets the same name-shortcut UX (#222). Tests inject a fake list of cases.
    """
    from kairix.quality.benchmark.suite import load_suite, resolve_suite_path

    suite_path = resolve_suite_path(suite)
    return load_suite(str(suite_path)).cases


def _default_search_fn(q: SampledQuery) -> Any:  # pragma: no cover — production path
    """Run one search via the production pipeline. Production-only seam.

    Uses the memoised factory so concurrent workers share one pipeline
    (per the factory cache fix that turned the bench-path cache misses
    into hits; see kairix.core.factory).
    """
    from kairix.core.factory import build_search_pipeline

    pipeline = build_search_pipeline()
    return pipeline.search(query=q.query, agent=q.agent)


def _build_sampled_queries(cases: list[Any], queries: int, seed: int) -> list[SampledQuery]:
    sampled_cases = sample_weighted(cases, n=queries, seed=seed)
    return [
        SampledQuery(
            case_id=getattr(c, "id", f"case_{i}"),
            category=c.category,
            query=c.query,
            agent=getattr(c, "agent", None),
        )
        for i, c in enumerate(sampled_cases)
    ]


def _per_category_stats(
    sampled: list[SampledQuery],
    durations_ms: list[float],
) -> dict[str, LatencyStats]:
    """Group durations by sampled-query category and compute per-category stats."""
    by_cat: dict[str, list[float]] = {}
    for sq, dur in zip(sampled, durations_ms, strict=True):
        by_cat.setdefault(sq.category, []).append(dur)
    return {cat: latency_stats(durs) for cat, durs in by_cat.items()}


def run_probe_search(
    suite: str,
    queries: int = 100,
    concurrency: int = 1,
    seed: int = 0,
    p95_threshold_ms: float = DEFAULT_P95_THRESHOLD_MS,
    *,
    suite_loader: Callable[[str], list[Any]] | None = None,
    searcher: Callable[[SampledQuery], Any] | None = None,
) -> ProbeResult:
    """Run a weighted sample of suite queries at the requested concurrency.

    Args:
        suite: benchmark suite name (e.g. ``reflib``) or explicit path.
        queries: total queries to sample and run (>=1).
        concurrency: thread-pool size (>=1; 1 = sequential through pool).
        seed: deterministic sample + shuffle seed.
        p95_threshold_ms: gate target for the overall p95 (default 500 ms,
            matching the architectural target in
            docs/architecture/teaming-concurrency-strategy.md).
        suite_loader: test seam — returns list[BenchmarkCase] for a suite name.
        searcher: test seam — runs one SampledQuery through a search pipeline.

    Returns:
        ProbeResult with overall + per-category latency stats, mean
        concurrency, error count, and a bottleneck recommendation.

    Raises:
        ValueError: when queries<1 or concurrency<1.
    """
    if queries < 1:
        raise ValueError(f"queries must be >= 1; got {queries}")
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1; got {concurrency}")

    loader = suite_loader or _default_suite_loader
    fn = searcher or _default_search_fn

    cases = loader(suite)
    sampled = _build_sampled_queries(cases, queries, seed)
    tasks = [(lambda q=sq: fn(q)) for sq in sampled]
    run = run_concurrent(tasks, concurrency=concurrency)

    durations_ms = [r.duration_ms for r in run.results]
    overall = latency_stats(durations_ms)
    per_category = _per_category_stats(sampled, durations_ms)

    azure_429_count = 0  # See ProbeResult.azure_429_count docstring for wire-up status.
    bottleneck = suggest_bottleneck(
        overall=overall,
        mean_concurrency=run.mean_concurrency,
        requested_concurrency=concurrency,
        p95_threshold_ms=p95_threshold_ms,
        azure_429_count=azure_429_count,
    )
    passed = overall.p95_ms <= p95_threshold_ms and run.errors == 0

    return ProbeResult(
        suite=suite,
        queries=len(sampled),
        concurrency=concurrency,
        seed=seed,
        overall=overall,
        per_category=per_category,
        mean_concurrency=run.mean_concurrency,
        wallclock_s=run.wallclock_s,
        azure_429_count=azure_429_count,
        errors=run.errors,
        p95_threshold_ms=p95_threshold_ms,
        passed=passed,
        bottleneck=bottleneck,
    )
