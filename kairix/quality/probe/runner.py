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

Test seam: :class:`ProbeDeps` (``load_suite`` + ``search``) is injectable so
tests can run fully hermetically with fakes from tests/fakes.py. Production
callers omit ``deps`` and get the bundled suite resolution +
``build_search_pipeline``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kairix.quality.probe.executor import run_concurrent
from kairix.quality.probe.sampler import sample_weighted
from kairix.quality.probe.stats import LatencyStats, latency_stats, suggest_bottleneck

DEFAULT_P95_THRESHOLD_MS = 500.0

# Attribute / envelope key for the per-stage wall-clock map carried on
# SearchResult-shaped results and on each per_query_stages record (#282
# follow-up). Extracted to a constant so the getattr fallback chain in
# ``_result_stage_map`` and the projection in ``_per_query_stages``
# can't drift apart silently if either string is renamed.
_STAGE_LATENCY_KEY = "stage_latency_ms"


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
    # One-time cold-build latency (ms) — the warm-up query that pays the
    # factory build + first-query model load, measured and reported
    # SEPARATELY so it never inflates the steady-state ``overall`` p95 the
    # gate compares against (#436 / PLA-273). ``0.0`` when ``warmup=False``.
    # ``overall`` / ``per_category`` / ``passed`` reflect steady-state only.
    cold_build_ms: float = 0.0
    # Mean per-stage wall-clock (ms) across every successful query (#282).
    # Keys: classify, resolve, dispatch, fuse, enrich, boost, budget,
    # bm25, vector, embed_http, vector_ann. Empty when no stage data was
    # returned (e.g. fake searcher that doesn't populate stage_latency_ms).
    # Reads with .get(stage, 0.0).
    stage_means_ms: dict[str, float] = field(default_factory=dict)
    # Per-query stage record list (#282 follow-up). One entry per executed
    # query with case_id, category, latency_ms, and the full stage_latency_ms
    # dict from that query's underlying SearchResult. Operators jq this list
    # to pull out the slow queries the means hide:
    #   jq '.per_query_stages | sort_by(-.latency_ms) | .[0:5]' probe.json
    # Empty when no successful results carried stage data.
    per_query_stages: list[dict[str, Any]] = field(default_factory=list)

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
            "cold_build_ms": self.cold_build_ms,
            "stage_means_ms": self.stage_means_ms,
            "per_query_stages": list(self.per_query_stages),
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


def _default_suite_loader(suite: str) -> list[Any]:
    """Resolve a suite name → list of BenchmarkCase. Production-only seam.

    Mirrors ``kairix benchmark run --suite SUITE`` resolution so the operator
    gets the same name-shortcut UX (#222). Tests inject a fake list of cases.
    """
    from kairix.quality.benchmark.suite import load_suite, resolve_suite_path

    suite_path = resolve_suite_path(suite)
    return load_suite(str(suite_path)).cases


def _default_search_fn(q: SampledQuery) -> Any:
    """Run one search via the production in-process pipeline.

    Thin shim over :class:`InProcessSearchClient` so existing callers keep
    working. The Protocol-shaped client is the documented seam (see
    :mod:`kairix.quality.probe.clients`); future MCPHttpSearchClient drops
    in by passing ``ProbeDeps(search=mcp_client.search)``.
    """
    from kairix.quality.probe.clients import InProcessSearchClient

    return InProcessSearchClient().search(q)


@dataclass
class ProbeDeps:
    """Injectable collaborators for :func:`run_probe_search` and
    :func:`kairix.quality.probe.burst.run_probe_burst`.

    Canonical Deps shape (``kairix/worker.py::WorkerDeps``). Production
    callers omit ``deps`` and get the bundled suite resolution + the
    in-process search pipeline; tests construct
    ``ProbeDeps(load_suite=..., search=...)`` with fakes.

    - ``load_suite``: ``(suite) -> list[BenchmarkCase]`` for a suite name or
      explicit path.
    - ``search``: runs one :class:`SampledQuery` through a search pipeline.
      Any ``Callable[[SampledQuery], Any]`` — e.g. a future
      ``MCPHttpSearchClient().search`` (see :mod:`kairix.quality.probe.clients`).
    """

    load_suite: Callable[[str], list[Any]] = field(default_factory=lambda: _default_suite_loader)
    search: Callable[[SampledQuery], Any] = field(default_factory=lambda: _default_search_fn)


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


def _result_stage_map(r: Any) -> dict[str, float] | None:
    """Resolve the stage_latency_ms dict for a TimedResult, if any.

    Prefers the executor-side cached ``r.stage_latency_ms`` (populated via
    ``getattr`` inside the executor wrapper). Falls back to reading
    ``getattr(r.result, "stage_latency_ms", None)`` for older TimedResult
    shapes that predate the executor cache. Returns ``None`` when neither
    surface exposes a dict.
    """
    cached = getattr(r, _STAGE_LATENCY_KEY, None)
    if isinstance(cached, dict):
        return cached
    inner = getattr(r.result, _STAGE_LATENCY_KEY, None) if r.result is not None else None
    return inner if isinstance(inner, dict) else None


def _stage_means(results: list[Any]) -> dict[str, float]:
    """Average per-stage wall-clock across successful results.

    Reads ``stage_latency_ms`` off each result (via executor cache, with
    ``.result`` fallback). Other searcher shapes (e.g. fakes that return
    dicts without stage timings) contribute nothing and yield an empty
    dict, which is a valid envelope shape.
    """
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for r in results:
        if not r.succeeded or r.result is None:
            continue
        stage_map = _result_stage_map(r)
        if stage_map is None:
            continue
        for stage, ms in stage_map.items():
            sums[stage] = sums.get(stage, 0.0) + float(ms)
            counts[stage] = counts.get(stage, 0) + 1
    return {stage: round(sums[stage] / counts[stage], 2) for stage in sums if counts[stage] > 0}


def _per_query_stages(
    sampled: list[SampledQuery],
    results: list[Any],
) -> list[dict[str, Any]]:
    """Project per-query stage records keyed by sampled-query metadata.

    Each record carries case_id, category, latency_ms, and stage_latency_ms.
    Uses ``r.task_index`` to map completion-order results back to their
    submission-order SampledQuery — required because the pool returns
    results in completion order, not submission order, so a naïve zip
    misattributes latency under concurrency > 1.

    Failed tasks still surface (with an empty stage_latency_ms) so
    operators can spot patterns in which queries error out.
    """
    out: list[dict[str, Any]] = []
    for r in results:
        idx = getattr(r, "task_index", -1)
        if idx < 0 or idx >= len(sampled):
            continue
        sq = sampled[idx]
        stage_map = _result_stage_map(r) or {}
        out.append(
            {
                "case_id": sq.case_id,
                "category": sq.category,
                "latency_ms": round(r.duration_ms, 2),
                _STAGE_LATENCY_KEY: dict(stage_map),
            }
        )
    return out


def _run_warmup(
    fn: Callable[[SampledQuery], Any],
    warm_query: SampledQuery,
    clock: Callable[[], float],
) -> float:
    """Run one warm-up query and return its latency (ms).

    The warm-up pays the one-time factory build + first-query model load so
    the measured steady-state sample reflects WARM latency, not a
    cold-build-contaminated first query (#436). Errors are captured by the
    executor (never raised) — a cold build that errors still yields a
    measured duration, so the cold/steady separation holds either way.
    """
    warm_run = run_concurrent([lambda: fn(warm_query)], concurrency=1, clock=clock)
    return round(warm_run.results[0].duration_ms, 2) if warm_run.results else 0.0


def run_probe_search(
    suite: str,
    queries: int = 100,
    concurrency: int = 1,
    seed: int = 0,
    p95_threshold_ms: float = DEFAULT_P95_THRESHOLD_MS,
    *,
    deps: ProbeDeps | None = None,
    warmup: bool = True,
    clock: Callable[[], float] = time.perf_counter,
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
        deps: :class:`ProbeDeps` carrying the suite loader + per-query
            search callable. ``None`` (production) binds the defaults.
        warmup: when True (default) run one extra warm-up query BEFORE the
            measured sample to absorb the one-time factory build + model
            load, recording its latency separately in
            ``ProbeResult.cold_build_ms``. The steady-state ``overall`` /
            ``per_category`` / ``passed`` then reflect warm latency only —
            the cold build no longer inflates the p95 the gate reads (#436).
            False reproduces the legacy single-pass behaviour
            (``cold_build_ms == 0.0``).
        clock: monotonic clock (seconds) every query latency and the run
            wallclock are measured on. Defaults to :func:`time.perf_counter`;
            deterministic callers inject a controlled virtual timeline
            (mirrors ``run_probe_burst``'s ``clock``).

    Returns:
        ProbeResult with steady-state overall + per-category latency stats,
        the separated ``cold_build_ms``, mean concurrency, error count, and
        a bottleneck recommendation.

    Raises:
        ValueError: when queries<1 or concurrency<1.
    """
    if queries < 1:
        raise ValueError(f"queries must be >= 1; got {queries}")
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1; got {concurrency}")

    deps = deps if deps is not None else ProbeDeps()
    fn = deps.search

    cases = deps.load_suite(suite)
    sampled = _build_sampled_queries(cases, queries, seed)

    # Cold-build warm-up (PLA-273): one query before the measured sample so
    # the factory build + first-query model load is paid (and reported)
    # separately rather than folded into the steady-state p95.
    cold_build_ms = _run_warmup(fn, sampled[0], clock) if (warmup and sampled) else 0.0

    tasks = [(lambda q=sq: fn(q)) for sq in sampled]
    run = run_concurrent(tasks, concurrency=concurrency, clock=clock)

    durations_ms = [r.duration_ms for r in run.results]
    overall = latency_stats(durations_ms)
    per_category = _per_category_stats(sampled, durations_ms)
    stage_means_ms = _stage_means(run.results)
    per_query_stages = _per_query_stages(sampled, run.results)

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
        cold_build_ms=cold_build_ms,
        stage_means_ms=stage_means_ms,
        per_query_stages=per_query_stages,
    )
