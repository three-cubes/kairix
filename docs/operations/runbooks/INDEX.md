# Kairix — Runbooks & Procedures

Operational procedures and incident runbooks for kairix deployments.

---

## Quick Links — Something's Wrong

| Symptom | Runbook |
|---|---|
| Multiple subsystems failing, dogfood says "search returning wrong/empty results", recall canary regressed | [kairix-retrieval-health](../../runbooks/kairix-retrieval-health.md) — start here for any cross-cutting retrieval degradation |
| `kairix search` returns `vec=0, vec_failed=True` | [runbook-vector-search-failure](runbook-vector-search-failure.md) |
| `kairix entity suggest` returns junk, agents miss known entities, or reflib recall regresses | [kairix-entity-audit](kairix-entity-audit.md) |
| New documents not appearing in search after the embed cycle | [runbook-embedding-lag](runbook-embedding-lag.md) |
| Every `mcp-kairix__*` tool returns `-32602 Invalid request parameters` | [MCP-CLIENT-MIGRATION](../MCP-CLIENT-MIGRATION.md) — your client is on `/sse` and needs to move to `/mcp` |
| NDCG@10 dropped after a config or index change | [runbook-benchmark-regression](runbook-benchmark-regression.md) |
| Specific queries scoring poorly | [how-to-debug-search-ranking](how-to-debug-search-ranking.md) |
| Updating kairix on a systemd VM (package bump, unit-file change, fetch-secrets change) | [kairix-systemd-update](../../runbooks/kairix-systemd-update.md) — pre-capture, ordered restart, gate on onboard check, manual rollback |
| Setting up MCP server for the first time | [MCP-DEPLOYMENT](../MCP-DEPLOYMENT.md) |
| Migrating an existing MCP client off SSE | [MCP-CLIENT-MIGRATION](../MCP-CLIENT-MIGRATION.md) |

---

## Incident Runbooks

| Runbook | What it covers |
|---|---|
| [kairix-retrieval-health](../../runbooks/kairix-retrieval-health.md) | Cross-cutting retrieval health and recovery — `kairix onboard check --json` first, then branch on the failed subsystem; full-reset fallback |
| [kairix-systemd-update](../../runbooks/kairix-systemd-update.md) | Safe update + rollback for systemd-on-VM deployments — pre-update capture, ordered service restart, onboard-check gate, manual rollback |
| [runbook-vector-search-failure](runbook-vector-search-failure.md) | `vec=0, vec_failed=True` — embed credentials, vector index integrity, no-vectors-yet |
| [runbook-embedding-lag](runbook-embedding-lag.md) | New content not searchable — sync, embed pipeline failures, scheduled-run issues |
| [runbook-benchmark-regression](runbook-benchmark-regression.md) | NDCG degraded — before/after comparison workflow and rollback |

---

## How-To Procedures

| Procedure | What it covers |
|---|---|
| [how-to-upgrade-kairix](how-to-upgrade-kairix.md) | Install tagged release, verify, run onboard check |
| [how-to-run-benchmark](how-to-run-benchmark.md) | Run benchmark suite, interpret results, compare before/after |
| [how-to-debug-search-ranking](how-to-debug-search-ranking.md) | Query intent dispatch, RRF weights, category-specific tuning |
| [how-to-rebuild-entity-graph](how-to-rebuild-entity-graph.md) | Drop and rebuild the Neo4j entity graph from the document store |
| [kairix-entity-audit](kairix-entity-audit.md) | Audit the entity graph — junk detection, path repair, enrichment, safe purge |
| [how-to-configure-pypi-trusted-publisher](how-to-configure-pypi-trusted-publisher.md) | One-time PyPI Trusted Publisher setup so GitHub Releases auto-publish without long-lived tokens |
| [MCP-DEPLOYMENT](../MCP-DEPLOYMENT.md) | Choose a transport (stdio/http/sse), wire `/mcp` and `/sse` mounts, configure agent registry, verify with `/healthz` |
| [MCP-CLIENT-MIGRATION](../MCP-CLIENT-MIGRATION.md) | Migrate Claude Desktop / Claude Code / OpenClaw / custom Python or Node clients from `/sse` to `/mcp` |

---

## Conventions

- **Incident runbooks** (`runbook-*.md`): Symptom → fix. No preamble, direct commands.
- **How-to procedures** (`how-to-*.md`): Step-by-step tasks with prerequisites and verification.
- All bash commands are in fenced blocks with expected output where it helps.

> For deployment-specific runbooks (secrets management, service restart, entity graph, embedding lag), see your operator configuration repository.
