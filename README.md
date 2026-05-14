# Kairix

**Give your agents the same knowledge as your team — without giving it away.**

[![Apache 2.0](https://img.shields.io/badge/licence-Apache%202.0-blue)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-3966_passing-brightgreen)]()
[![Hit@5](https://img.shields.io/badge/Hit%405-98.5%25-orange)]()

---

## The pain

You're a small team using AI agents to scale your impact. But agents are messy by default. They dump files wherever. They write things that contradict what you decided last month. They don't know who your clients are or how your projects connect. Every agent starts from zero — no memory, no structure, no shared context.

So you end up doing the work yourself: pasting documents into prompts, writing detailed instructions about where things live, fixing files agents put in the wrong place, and managing yet another platform to hold it all together.

The usual options: send your private knowledge to a vendor's cloud, burn tokens stuffing documents into context windows, or spend weeks building a RAG pipeline you'll have to maintain forever. None of that makes your agents smarter — it just gives you more to manage.

## What changes with kairix

Kairix gives your agents a shared knowledge layer they can search, write to, and manage — without you becoming a platform team. Your files stay on your machine. Your agents and your team work from the same knowledge.

**Your agents find answers instead of guessing.** One tool call returns ranked, relevant content — ~1,500 tokens instead of dumping 10,000–50,000 tokens of full documents into the prompt. In a 200K context window, that's 58 searches per session instead of 5. Your agents can actually research a topic instead of running out of room after the first question.

**Agents stop putting documents in the wrong place.** The classifier knows the difference between a decision, a runbook, a meeting note, and a research output. When an agent writes something new, kairix routes it to the right location in your knowledge store — no filing instructions needed.

**Agents stop contradicting what you've already decided.** Before writing a new fact or decision, agents can check it against existing knowledge. Kairix flags conflicts: "this contradicts what was agreed in Q1" — before it gets saved, not after you discover the mess.

**Agents know who people are without being told.** Kairix discovers people, companies, and relationships from your document structure and builds a knowledge graph automatically. When an agent asks about a client, they get the full picture — related contacts, recent decisions, open work — not just documents that mention the name.

**One tool call replaces pages of prompt instructions.** Without kairix, your agent's system prompt describes file paths, folder structures, and search strategies. With kairix, the instruction is: "search kairix before answering." The retrieval logic, entity awareness, and budget management happen behind the scenes.

**The knowledge layer maintains itself.** A background worker keeps the search index current. Wikilinks between documents stay up to date. Evaluation tools test whether search is working well on your content and tell you which approach works best. The system improves over time without you tuning it.

---

## What agents say

> *"Before kairix, I had to ask Dan for context on every client. Now I search before every task and I already know who the client is, what we've decided, and what's still open. I run contradiction checks before writing decisions — kairix catches conflicts before they become problems."*
> — **Shape**, chief of staff agent at Three Cubes

> *"I used to get a wall of instructions about where to find things and where to put things. Now my prompt just says 'search kairix.' When I write something new, the classifier handles where it goes. I spend my context window on the actual work."*
> — **Builder**, engineering agent at Three Cubes

---

## Measured: tokens saved on a real deployment

Tested on a knowledge store with 4,000+ documents (notes, decisions, client records, technical docs). Five representative queries, measured on the production VM.

| Method | Tokens per query | Searches per session (200K window) | Cost per 1,000 queries |
|--------|-----------------|-----------------------------------|----------------------|
| **Paste all relevant docs** | 9,000–50,000 | 5 before the window fills up | $27–150 |
| **Kairix search** | 1,200–1,700 | 58 searches with room to spare | $3.60–5.10 |

That's **4–30x fewer tokens** per query. Your agents read less noise and give better answers, because they're working from ranked, relevant chunks — not entire documents.

**What does a search look like?**

```
Agent asks: "brief me on the kairix project before my meeting"
  → 24 ranked results, 1,222 tokens, top result: KAIRIX-POSITIONING.md

Agent asks: "who works at Three Cubes and what are they responsible for?"
  → 23 ranked results, 1,171 tokens, top result: entities/concept/builder.md

Agent asks: "what is the process for deploying a new version?"
  → 26 ranked results, 1,392 tokens, top result: deployment runbook
```

Each search returns ranked content with the most relevant material first. The agent gets what it needs without reading entire documents.

**Search quality on a real deployment (200 queries, independently judged):**

| What we measured | Score |
|-----------------|-------|
| Right doc in top 5 | 98.5% of queries |
| First relevant result | Position 1.1 on average |
| All categories above quality floor | recall, entity, temporal, conceptual, procedural |

Tested on the production knowledge store with 4,000+ documents. Scores generated by an independent LLM judge using graded relevance — not self-reported.

---

## Where kairix fits

Kairix is the **knowledge layer** — it sits between your agents and your documents.

```
Your agents (Claude, OpenClaw, LangGraph, CrewAI, or custom)
    ↓ ask questions via MCP tools
Kairix (searches, ranks, and returns right-sized answers)
    ↓ reads from
Your documents (notes, markdown, PDFs, exports — whatever you have)
```

It works with any agent platform that supports [MCP](https://modelcontextprotocol.io/) (Model Context Protocol). One tool call, one question — kairix handles the searching, ranking, entity lookup, and budget management behind the scenes.

### Connects to

- **[Claude Code](https://claude.ai/claude-code)** / **Claude Desktop** — add kairix to your MCP config. Claude searches your knowledge store during conversations and coding sessions.
- **[OpenClaw](https://openclaw.ai)** — register kairix as an MCP server and every agent gets search tools automatically. Runs on the same VM — adds ~200MB RAM.
- **[LangGraph](https://github.com/langchain-ai/langgraph)** / **[CrewAI](https://github.com/crewAIInc/crewAI)** — the `research` tool does iterative multi-turn search, refining its own queries until it finds a good answer.
- **Any MCP-compatible agent** — stdio or SSE transport, no custom integration code.

---

## What it costs

| Component | Monthly cost | What you get |
|-----------|-------------|--------------|
| VM (4 vCPU, 16GB) | ~$20 | Runs everything — search, indexing, knowledge graph, agents |
| LLM API (embedding) | ~$3-5 | Index 4,000 documents, hourly incremental updates |
| LLM API (search) | ~$2-5 | Depends on query volume |
| **Total** | **~$25-30** | Full private knowledge layer for your team |

No GPU. No per-seat licensing. One VM serves your entire team of agents and humans. Runs on hardware you already own, or about $25/month on any cloud provider.

---

## Quick start

Kairix is the memory + context layer your agent uses to stay oriented across sessions and stay aligned with its human / agent team. The flow below is written for an agent (or its admin) reading this cold: install, point at credentials, point at documents, verify, wire into your agent runtime.

**Prereqs:** Python 3.10+ or Docker, an LLM API key for embeddings (Azure OpenAI or any OpenAI-compatible API), a folder of documents.

### 1. Install

```bash
# pip
pip install "kairix-agentic-knowledge-mgt[agents,neo4j]" && kairix setup

# Docker
curl -O https://raw.githubusercontent.com/three-cubes/kairix/main/docker-compose.yml \
  && curl -O https://raw.githubusercontent.com/three-cubes/kairix/main/.env.example \
  && cp .env.example .env && docker compose up -d
```

### 2. Configure secrets

Two supported paths — pick whichever matches your deployment.

**Production (Azure Key Vault):** set `KAIRIX_KV_NAME=<vault-name>` in `/opt/kairix/service.env`. Kairix resolves secrets via `az keyvault secret show` at first use. On Docker / VM deployments a `kairix-fetch-secrets.service` systemd unit can pre-populate `/run/secrets/kairix.env` from the vault so the kairix process never touches the Azure CLI at runtime.

**Local dev / CI:** set the env vars directly. The four that matter:

| Env var | Purpose |
|---------|---------|
| `KAIRIX_LLM_API_KEY` | API key for the LLM / embedding provider |
| `KAIRIX_LLM_ENDPOINT` | LLM / embedding endpoint URL |
| `KAIRIX_AZURE_API_KEY` | Azure OpenAI API key (when using Azure) |
| `KAIRIX_AZURE_API_VERSION` | Azure OpenAI API version (e.g. `2024-08-01-preview`) |

For the full secret map (Neo4j password, embed-specific overrides, etc.) see [`kairix/secrets.py`](kairix/secrets.py). Resolution order is: direct env vars → per-file secrets → `kairix.env` bundle → Azure Key Vault CLI fallback.

### 3. Configure document collections

Kairix organises your documents into named **collections** declared in `kairix.config.yaml`. Each collection has an `in_default: true|false` flag that controls whether your agent sees it in default searches:

- **`in_default: true`** — collection joins the default search mix every time your agent calls `tool_search` without an explicit scope. Good for your **user library** (the team's working knowledge — projects, decisions, runbooks).
- **`in_default: false`** — collection is still indexed and reachable via `--collection <name>` (CLI) or the `agent` parameter on `tool_search`, but it does not auto-join default scopes. Good for your **reference library** (large external corpora that would otherwise dominate result mix).

Concrete example — a personal knowledge base alongside a shared reference library:

```yaml
collections:
  shared:
    - name: projects             # team's working knowledge
      path: "01-Projects"
      glob: "**/*.md"
      # in_default defaults to true
    - name: reference-library    # 5,000+ external docs
      path: "reference-library"
      glob: "**/*.md"
      in_default: false          # opt-in scope only
```

See [`kairix.example.config.yaml`](kairix.example.config.yaml) for the full schema (per-agent paths, retrieval overrides per collection, fusion strategy).

### 4. Verify

```bash
kairix onboard check          # human-readable: runs 9 checks (PATH → wrapper → secrets → docs → vector → Neo4j → agent memory → chunk dates → MCP)
kairix onboard check --json   # structured: same checks, machine-readable, exits 0 only on 9/9 — wire into your docker-compose healthcheck or external monitor
```

Green output looks like `9/9 passed`. The `--json` shape is `{passed, total, fully_passed, failures: [{check, detail, remediation}]}` — each failure carries an operator-actionable `remediation` string verbatim. Common failures and the canonical fix for each:

| Check | Means | Canonical fix |
|-------|-------|---------------|
| `kairix_on_path` | `kairix` binary not on `$PATH` | `bash scripts/deploy-vm.sh` (installs the wrapper + symlink) |
| `wrapper_installed` | Symlink points at raw Python binary, not the shell wrapper | Run the deploy script to reinstall the wrapper |
| `secrets_loaded` | `KAIRIX_LLM_API_KEY` / `KAIRIX_LLM_ENDPOINT` not in env or in `/run/secrets/kairix.env` | Add them to `/opt/kairix/secrets.env` or enable `kairix-fetch-secrets.service` |
| `document_root_configured` | `KAIRIX_DOCUMENT_ROOT` unset or directory missing | `export KAIRIX_DOCUMENT_ROOT=/data/documents` (or your path) |
| `vector_search_working` | Vector search returned 0 results or failed | Run `kairix embed`; if credentials are missing fix `secrets_loaded` first |
| `neo4j_reachable` | Neo4j unreachable or empty | `bash scripts/install-neo4j.sh`; then `kairix store crawl --document-root $KAIRIX_DOCUMENT_ROOT` |
| `agent_knowledge_populated` | No agent memory logs found | Create `<doc-root>/04-Agent-Knowledge/<agent>/memory/`; agents write daily logs there |
| `chunk_date_populated` | `chunk_date` column unpopulated (temporal boost inert) | Run `kairix embed` (migration is automatic) |
| `mcp_service` | No MCP consumer registered | See step 5 below |

Each failed check prints its own remediation string verbatim — agents should surface those strings to their admin without paraphrasing. The full diagnostic lives in [`kairix/platform/onboard/check.py`](kairix/platform/onboard/check.py).

### 5. Wire into your agent

**OpenClaw** — register the kairix MCP server and load the kairix-memory-prompt plugin so the agent gets bootstrap context at session start:

```jsonc
{
  "mcp": {
    "servers": {
      "mcp-kairix": {
        "command": "kairix",
        "args": ["mcp", "serve"],
        "description": "Knowledge base search, research, entity lookup"
      }
    }
  },
  "plugins": {
    "load": {
      "paths": ["/opt/kairix/plugins/openclaw"]
    },
    "allow": ["kairix-memory-prompt"],
    "entries": {
      "kairix-memory-prompt": {
        "hooks": {
          "allowPromptInjection": true
        }
      }
    }
  }
}
```

The `kairix-memory-prompt` plugin ships with kairix (since #246 W5) at `/opt/kairix/plugins/openclaw/memory-prompt/` in the container image, and at `<site-packages>/kairix/plugins/openclaw/memory-prompt/` for non-Docker installs. Full operator notes — including verification, fallback behaviour, and the openclaw plugin API the plugin relies on — live in [`kairix/plugins/openclaw/memory-prompt/README.md`](kairix/plugins/openclaw/memory-prompt/README.md).

**Claude Desktop / Claude Code:** add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kairix": {
      "command": "kairix",
      "args": ["mcp", "serve"]
    }
  }
}
```

**Tell your agent what to do with kairix:** the canonical operating contract is in [`docs/agents/AGENT-SETUP.md`](docs/agents/AGENT-SETUP.md) — when to call `tool_search`, when to call `tool_brief`, how to read the `health` envelope, and what to do when kairix degrades. Point your agent at that file first.

**At session start, agents call `kairix bootstrap <agent>`** to get a one-shot orientation envelope: role, current `Board.md`, last N daily memory entries, active goals, and a `health` field showing what's online (`vector_search`, `bm25`, `chat`, `secrets_loaded`). Markdown by default, `--json` for tooling. The MCP equivalent is `tool_bootstrap(agent, max_memory_days=3)`. The openclaw plugin shipped at `/opt/kairix/plugins/openclaw/memory-prompt/` runs this automatically and injects the result into the session prompt — agents start oriented, not reactive.

**Every MCP tool response carries a `health` field** (`vector_search` / `bm25` / `chat` / `secrets_loaded` / `degraded_reason` / `next_action`). When kairix is partially down, agents still get whatever subsystem works, plus a concrete instruction to surface to the admin — they never silently fail.

See the [full quick-start guide](docs/getting-started/quick-start.md) for the detailed install path, and [connecting agents](docs/getting-started/connecting-agents.md) for LangGraph / CrewAI / VS Code integrations.

**Ships with:** 5,800+ reference library documents and a 200-case gold suite for immediate quality verification.

---

## How your data is handled

Your documents stay on your machine. The only outbound call is to generate search embeddings (a mathematical fingerprint of your text) — and even that can run locally with an Ollama adapter (coming soon).

All indexes, vectors, and knowledge graph data live in SQLite and Neo4j on your own infrastructure. Nothing is stored externally.

See [SECURITY.md](SECURITY.md) for detail.

---

## Going deeper

| Topic | Where to look |
|-------|--------------|
| Agent setup (operating contract) | [docs/agents/AGENT-SETUP.md](docs/agents/AGENT-SETUP.md) |
| Admin conversation scripts | [docs/agents/ADMIN-CONVERSATION.md](docs/agents/ADMIN-CONVERSATION.md) |
| Connecting your agents | [docs/getting-started/connecting-agents.md](docs/getting-started/connecting-agents.md) |
| What agents can do with kairix | [docs/user-guide/agent-usage-guide.md](docs/user-guide/agent-usage-guide.md) |
| MCP tools reference | [docs/user-guide/mcp-tools.md](docs/user-guide/mcp-tools.md) |
| Running and maintaining kairix | [docs/operations/OPERATIONS.md](docs/operations/OPERATIONS.md) |
| Measuring search quality on your data | [docs/user-guide/eval-guide.md](docs/user-guide/eval-guide.md) |
| Architecture and design decisions | [docs/architecture/ENGINEERING.md](docs/architecture/ENGINEERING.md) |
| What's coming next | [docs/project/ROADMAP.md](docs/project/ROADMAP.md) |

---

## Development

```bash
git clone https://github.com/three-cubes/kairix
cd kairix
pip install -e ".[dev,neo4j,agents,rerank]"
bash scripts/safe-commit.sh "msg"  # canonical commit gate: lint, format, mypy, ~3,966 tests, security, fitness
pytest tests/                      # bare test run
ruff check kairix/ tests/          # lint only
```

`scripts/safe-commit.sh` is the single entry point — it runs every gate the CI runs in the same order before letting the commit through; failing gates print the exact fix command. See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture and PR process, and [docs/architecture/fitness-functions.md](docs/architecture/fitness-functions.md) for the F1–F24 architecture fitness functions that enforce structural invariants.

---

## Licence

Apache 2.0 — see [LICENSE](LICENSE).

Built on: [usearch](https://github.com/unum-cloud/usearch) (Unum Cloud), [SQLite FTS5](https://www.sqlite.org/fts5.html), [Neo4j Community Edition](https://neo4j.com/).
