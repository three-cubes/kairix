"""
kairix — private knowledge retrieval for AI agents and teams.

Subcommands:
  bootstrap   Agent orientation envelope: role, board, recent memory, goals, health
  embed       Embed documents into the kairix vector index
  search      Hybrid search: BM25 + vector via RRF
  entity      Entity management: suggest (NER), validate (Wikidata), audit, purge
  curator     Curator agent: entity health monitoring and enrichment (CA-1)
  contradict  Contradiction detection: check new content against existing knowledge
  store       Document store operations: crawl entities into Neo4j, health check
  mcp         MCP server: expose search/entity/prep/timeline as MCP tools
  onboard     Deployment diagnostics and agent onboarding (check, guide, verify)
  timeline    Temporal query rewriting + date-aware retrieval
  summarise   L0/L1 tiered context generation
  classify    Auto-classify memory writes
  brief       Session briefing synthesis
  prep        Tiered L0/L1 context summary for a topic
  research    Iterative research over the knowledge store with LLM synthesis
  usage-guide Read the kairix agent usage guide (full text or topic-filtered)
  benchmark   Run retrieval quality benchmark
  probe       Concurrent-load latency probe (decide which Tier 1 tuning lever to pull)
  probe-config  Probe the configured provider for health + tuning recommendations
  soak        Repeat a workload and assert it holds together (memory, log volume, fd, determinism)
  warm        Pre-load caches + pay factory-init costs (run at container start, before /healthz/ready=200)
  wikilinks   Inject [[wikilinks]] on first mention in agent-written document store files
  reference-library  Reference library: install entities, check status, run extraction
  eval        Evaluation harness: gold suite build, judge, sweep, monitor, gate
  setup       First-time onboarding wizard for credentials and paths
  worker      Background worker: run loop, pause/resume operator controls
  config      Validate kairix.config.yaml against the schema and print errors

See KAIRIX-ARCHITECTURE.md for architecture, ADRs, and roadmap.
"""

import sys

# Dispatch table: command name → (module_path, function_name, accepts_args)
# Lazy imports keep startup fast — only the selected command is imported.
COMMANDS: dict[str, tuple[str, str, bool]] = {
    "bootstrap": ("kairix.bootstrap_cli", "main", True),
    "embed": ("kairix.core.embed.cli", "main", False),
    "entity": ("kairix.knowledge.entities.cli", "main", True),
    "curator": ("kairix.agents.curator.cli", "main", True),
    "search": ("kairix.core.search.cli", "main", True),
    "benchmark": ("kairix.quality.benchmark.cli", "main", True),
    "probe": ("kairix.quality.probe.cli", "main", True),
    "probe-config": ("kairix.quality.probe.config_cli", "main", True),
    "soak": ("kairix.quality.soak.cli", "main", True),
    "warm": ("kairix.platform.warm.cli", "main", True),
    "summarise": ("kairix.knowledge.summaries.cli", "main", True),
    "timeline": ("kairix.core.temporal.cli", "main", True),
    "wikilinks": ("kairix.knowledge.wikilinks.cli", "main", True),
    "classify": ("kairix.core.classify.cli", "main", True),
    "brief": ("kairix.agents.briefing.cli", "main", True),
    "prep": ("kairix.agents.prep.cli", "main", True),
    "research": ("kairix.agents.research.cli", "main", True),
    "usage-guide": ("kairix.agents.usage_guide.cli", "main", True),
    "contradict": ("kairix.knowledge.contradict.cli", "main", True),
    "store": ("kairix.knowledge.store.cli", "main", True),
    "vault": ("kairix.knowledge.store.cli", "main", True),  # backwards-compat alias
    "mcp": ("kairix.agents.mcp.cli", "main", True),
    "onboard": ("kairix.platform.onboard.cli", "main", True),
    "eval": ("kairix.quality.eval.cli", "main", True),
    "reference-library": ("kairix.knowledge.reflib.cli", "main", True),
    "setup": ("kairix.platform.setup.cli", "main", True),
    "worker": ("kairix.worker_cli", "main", True),
    "config": ("kairix.core.search.config_validator", "main", True),
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) >= 2 else 1)

    cmd = sys.argv[1]

    if cmd in ("--version", "-V", "version"):
        from kairix import __version__

        print(f"kairix {__version__}")
        sys.exit(0)

    entry = COMMANDS.get(cmd)
    if entry is None:
        print(f"Unknown command: {cmd}\n{__doc__}", file=sys.stderr)
        sys.exit(1)

    module_path, func_name, accepts_args = entry
    import importlib

    mod = importlib.import_module(module_path)
    fn = getattr(mod, func_name)

    if accepts_args:
        result = fn(sys.argv[2:])
        if result is not None:
            sys.exit(result)
    else:
        fn()


if __name__ == "__main__":
    main()
