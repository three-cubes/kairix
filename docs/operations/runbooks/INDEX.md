# Kairix — Runbooks & Procedures

Operational procedures and incident runbooks for kairix deployments.

---

## Quick Links — Something's Wrong

| Symptom | Runbook |
|---|---|
| Every `mcp-kairix__*` tool returns `-32602 Invalid request parameters` | [MCP-CLIENT-MIGRATION](../MCP-CLIENT-MIGRATION.md) — your client is on `/sse` and needs to move to `/mcp` |
| NDCG@10 dropped after a config or index change | [runbook-benchmark-regression](runbook-benchmark-regression.md) |
| Specific queries scoring poorly | [how-to-debug-search-ranking](how-to-debug-search-ranking.md) |
| Setting up MCP server for the first time | [MCP-DEPLOYMENT](../MCP-DEPLOYMENT.md) |
| Migrating an existing MCP client off SSE | [MCP-CLIENT-MIGRATION](../MCP-CLIENT-MIGRATION.md) |

---

## Incident Runbooks

| Runbook | What it covers |
|---|---|
| [runbook-benchmark-regression](runbook-benchmark-regression.md) | NDCG degraded — before/after comparison workflow and rollback |

---

## How-To Procedures

| Procedure | What it covers |
|---|---|
| [how-to-upgrade-kairix](how-to-upgrade-kairix.md) | Install tagged release, verify, run onboard check |
| [how-to-run-benchmark](how-to-run-benchmark.md) | Run benchmark suite, interpret results, compare before/after |
| [how-to-debug-search-ranking](how-to-debug-search-ranking.md) | Query intent dispatch, RRF weights, category-specific tuning |
| [MCP-DEPLOYMENT](../MCP-DEPLOYMENT.md) | Choose a transport (stdio/http/sse), wire `/mcp` and `/sse` mounts, configure agent registry, verify with `/healthz` |
| [MCP-CLIENT-MIGRATION](../MCP-CLIENT-MIGRATION.md) | Migrate Claude Desktop / Claude Code / OpenClaw / custom Python or Node clients from `/sse` to `/mcp` |

---

## Conventions

- **Incident runbooks** (`runbook-*.md`): Symptom → fix. No preamble, direct commands.
- **How-to procedures** (`how-to-*.md`): Step-by-step tasks with prerequisites and verification.
- All bash commands are in fenced blocks with expected output where it helps.

> For deployment-specific runbooks (secrets management, service restart, entity graph, embedding lag), see your operator configuration repository.
