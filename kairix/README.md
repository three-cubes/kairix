# kairix/

Importable Python package. Layout follows the Protocol + Pipeline +
Factory + Strategy pattern documented in
[../docs/architecture/ENGINEERING.md](../docs/architecture/ENGINEERING.md).

- `core/` — domain boundary protocols and the SearchPipeline orchestrator
- `knowledge/` — domain-specific knowledge layers (entities, graph,
  contradict, summaries, wikilinks, reflib)
- `agents/` — agent-facing surfaces (briefing, mcp server)
- `platform/` — host-OS integration (onboard check, setup wizard)
- `quality/` — benchmark suites and gates
- `plugins/` — packaged plugins (openclaw memory-prompt)
- `use_cases/` — high-level use-case orchestrators (bootstrap, etc.)

CLI entry points: `kairix` (root), `kairix worker` (worker control). The
import path is always `kairix.*`; the PyPI distribution name
(`Kairix-agentic-knowledge-mgt`) is unrelated. New modules must respect
F1-F23 (see [../docs/architecture/fitness-functions.md](../docs/architecture/fitness-functions.md)).
