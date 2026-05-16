"""Search-probe — concurrent-load latency measurement for teaming environments.

Decision context: see docs/architecture/teaming-concurrency-strategy.md.
This probe is the measurement instrument that decides which Tier 1 tuning
lever to pull (Azure embed pool, query-cache, connection-pool sizes) by
showing where p95 latency degrades under concurrency.

Module API (in progress — chunks landing across multiple commits):
    from kairix.quality.probe import sample_weighted, latency_stats
    # run_probe_search lands with the executor+runner chunk.

Bindings (forward references — land in later chunks):
    CLI:  kairix probe search --suite reflib --queries 100 --concurrency 5
    MCP:  tool_probe_search (hard-capped: queries<=20, concurrency<=3)
"""

from kairix.quality.probe.sampler import sample_weighted
from kairix.quality.probe.stats import LatencyStats, latency_stats, suggest_bottleneck

__all__ = [
    "LatencyStats",
    "latency_stats",
    "sample_weighted",
    "suggest_bottleneck",
]
