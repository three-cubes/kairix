# Quick Start

Get kairix running and searching your documents in under 30 minutes.

## What you need

- **Docker and Docker Compose** (Docker Desktop, or Docker Engine + Compose plugin)
- **An LLM API key** — Azure OpenAI, standard OpenAI, or any OpenAI-compatible provider
- **A folder of documents** — markdown files, text files, or structured notes

## Steps

### 1. Get the compose file

```bash
curl -O https://raw.githubusercontent.com/three-cubes/kairix/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/three-cubes/kairix/main/.env.example
```

Or clone the full repo:

```bash
git clone https://github.com/three-cubes/kairix
cd kairix
```

### 2. Set up your credentials

```bash
cp .env.example .env
```

Open `.env` and add your LLM API key:

```bash
# For Azure OpenAI:
KAIRIX_LLM_ENDPOINT=https://your-resource.openai.azure.com
KAIRIX_LLM_API_KEY=your-key-here

# Or for standard OpenAI / OpenRouter:
# KAIRIX_LLM_ENDPOINT=https://api.openai.com/v1
# KAIRIX_LLM_API_KEY=sk-your-key-here
```

### 3. Point to your documents

```bash
ln -s ~/Documents/my-notes ./documents
```

**Don't have documents ready?** The container includes 5,800+ curated reference library documents. You can start searching immediately and add your own documents later.

### 4. Start everything

```bash
docker compose up -d
```

This starts three services:
- **kairix** — search engine and MCP server (port 8080)
- **kairix-worker** — indexes your documents automatically every hour
- **neo4j** — knowledge graph for people/company queries

### 5. Index your documents

```bash
docker compose exec kairix kairix embed
```

This indexes your documents for search. For 1,000 documents (~4,000 chunks), expect ~$0.50-1.00 with text-embedding-3-large.

### 6. Verify your setup

```bash
docker compose exec kairix kairix onboard check          # human-readable
docker compose exec kairix kairix onboard check --json   # structured — exits 0 only on 9/9, wire into your healthcheck
```

You should see:

```
kairix deployment check
──────────────────────────────────────────────────
  ✓ kairix_on_path
  ✓ wrapper_installed
  ✓ secrets_loaded
  ✓ document_root_configured — Document root: /data/documents
  ✓ vector_search_working
  ✓ neo4j_reachable
  ✓ agent_knowledge_populated
  ✓ chunk_date_populated
  ✓ mcp_service
──────────────────────────────────────────────────
  All 9 checks passed
```

If any checks fail, the output explains exactly what to fix — each failure carries an `remediation` string. The `--json` shape `{passed, total, fully_passed, failures: [{check, detail, remediation}]}` is the canonical healthcheck signal: structured, machine-readable, exit 0 only on full pass.

### 7. Verify search quality

Run the built-in benchmark against the reference library — bundled suites resolve by name:

```bash
docker compose exec kairix kairix benchmark list           # enumerate bundled suites
docker compose exec kairix kairix benchmark run reflib     # runs the reference-library gold suite
```

This indexes the reference library (5,800+ open-source documents), runs a 200-case gold suite, and reports search quality scores. Sensible defaults shipped with kairix gate the run (in `pyproject.toml` under `[tool.kairix.benchmark.gates]`): overall **≥ 0.78**, temporal **≥ 0.55**, entity **≥ 0.80**, contextual_prep **≥ 0.60**. Expected baseline:

| Metric | Expected |
|--------|----------|
| Weighted total | ≥ 0.80 |
| NDCG@10 | ≥ 0.90 |
| Hit@5 | ≥ 95% |

If scores are significantly below these, check your embedding model and LLM connection.

### 8. Search

```bash
docker compose exec kairix kairix search "your question here"
```

Your knowledge store is running.

---

## Connecting agents

The MCP server runs on port 8080. Any MCP-compatible agent can connect via SSE.

**Claude Desktop / Claude Code:**

Add to your MCP config:
```json
{
  "mcpServers": {
    "kairix": {
      "url": "http://localhost:8080/sse"
    }
  }
}
```

See [connecting-agents.md](connecting-agents.md) for OpenClaw, LangGraph, and other platforms.

---

## What happens next

- **Documents are indexed automatically** every hour by the worker service. Operator controls: `kairix worker pause` / `resume` / `status`.
- **The MCP server exposes 11 tools** — `search`, `entity`, `prep`, `timeline`, `research`, `contradict`, `usage_guide`, `brief`, `bootstrap`, `entity_suggest`, `entity_validate`. Each response carries a `health` envelope (`vector_search` / `bm25` / `chat` / `secrets_loaded`) so agents know what's online and what to surface to their human admin.
- **Agents should call `kairix bootstrap <agent>` at session start** to get a one-shot orientation envelope (role, board, recent memory, active goals, health).
- **Run `kairix onboard check --json`** any time — exit 0 means 9/9, exit 1 prints structured failures with remediation strings.
- **Run `kairix benchmark run reflib`** to benchmark search quality against the bundled reference-library gold suite. `kairix benchmark list` enumerates the bundled set.
