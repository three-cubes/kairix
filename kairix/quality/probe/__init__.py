"""Search-probe — concurrent-load latency measurement for teaming environments.

Decision context: see docs/architecture/teaming-concurrency-strategy.md.
This probe is the measurement instrument that decides which Tier 1 tuning
lever to pull (Azure embed pool, query-cache, connection-pool sizes) by
showing where p95 latency degrades under concurrency.

Module API:
    from kairix.quality.probe import run_probe_search, ProbeResult
    result = run_probe_search(suite="reflib", queries=100, concurrency=5)
    if not result.passed:
        for cat, lat in result.per_category.items():
            print(f"{cat}: p95={lat.p95_ms}ms")

Bindings (CLI + MCP land in subsequent chunks):
    CLI:  kairix probe search --suite reflib --queries 100 --concurrency 5
    MCP:  tool_probe_search (hard-capped: queries<=20, concurrency<=3)
"""

from kairix.quality.probe.executor import ConcurrentRun, TimedResult, run_concurrent
from kairix.quality.probe.runner import ProbeResult, SampledQuery, run_probe_search
from kairix.quality.probe.sampler import sample_weighted
from kairix.quality.probe.stats import LatencyStats, latency_stats, suggest_bottleneck

__all__ = [
    "ConcurrentRun",
    "LatencyStats",
    "ProbeResult",
    "SampledQuery",
    "TimedResult",
    "latency_stats",
    "run_concurrent",
    "run_probe_search",
    "sample_weighted",
    "suggest_bottleneck",
]
