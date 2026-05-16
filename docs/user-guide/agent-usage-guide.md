---
type: reference
scope: shared
tags: [kairix, agent-knowledge, search, retrieval]
---

# Kairix Agent Usage Guide

> **First-time users:** Run `kairix setup` to configure your environment before proceeding. See [docs/getting-started/quick-start.md](../getting-started/quick-start.md) for full installation instructions.

This guide is for AI agents using kairix to search and retrieve knowledge from the shared knowledge store. Read it before your first session, and use it as a reference when queries return unexpected results.

---

## What kairix is

Kairix is the retrieval layer between you and the team's knowledge base. It indexes the document store (Obsidian markdown files), runs hybrid search (BM25 + vector), and returns ranked snippets within a token budget. It understands query intent — so a question about "what happened last week" gets different treatment than "what is the engineering pattern for retries".

You do not need to use basic keyword search. Kairix routes your query to the right retrieval strategy automatically.

---

## How to call kairix

```bash
kairix search "<your query>" --agent <your-agent-name>
```

Examples:
```bash
kairix search "what decisions were made about the Azure connector" --agent builder
kairix search "knowledge management positioning" --agent builder --budget 3000
kairix search "how do I run the embedding pipeline" --agent builder
kairix search "what happened last week" --agent builder
kairix search "tell me about Acme Corp" --agent builder
```

**If kairix is not on PATH** (you get `command not found`):
```bash
/usr/local/bin/kairix search "<query>" --agent <name>
```

---

## Flags that matter

| Flag | Default | When to use |
|---|---|---|
| `--agent <name>` | None | Always — scopes results to your agent's collections + shared |
| `--scope <value>` | `shared+agent` | Override the default scope. See "Scope" section below. |
| `--budget <tokens>` | 5000 | Reduce if context window is tight; 2000–3000 is usually enough |
| `--json` | Off | Machine-readable output — use when parsing results programmatically |

---

## Scope

Every retrieval tool (`search`, `prep`, `timeline`, `contradict`) accepts a `scope` value. It controls which document collections the search reaches.

| Scope | Reaches | When to use |
|---|---|---|
| `shared` | Shared collections only (vault content not tied to any agent) | When the agent's own memory shouldn't influence the answer — e.g. fact-checking a claim against curated knowledge. |
| `agent` | Only the calling agent's own memory | When you specifically want to recall what *this agent* has previously written — e.g. session continuity. |
| `shared+agent` (default) | Shared + the calling agent's memory | Usual case — the agent has access to organisational knowledge plus its own history. |
| `all-agents` | Every agent's memory, no shared | Cross-agent synthesis — "what has the team collectively discovered about X?" Requires `agents:` configured in `kairix.config.yaml`. |
| `everything` | Shared + every agent's memory | Maximum-recall queries; treat as a last resort because it dilutes precision. |

**MCP equivalents:** the same values (as strings) are accepted on the `scope` parameter of `mcp-kairix__search`, `mcp-kairix__prep`, `mcp-kairix__timeline`, and `mcp-kairix__contradict`.

---

## How intent routing works

Kairix classifies your query before running search. The classification changes which retrieval strategy fires:

| Intent | Triggered by | What happens |
|---|---|---|
| **keyword** | Version strings, error codes, file names, proper nouns | BM25 + vector in parallel; exact terms weighted highly |
| **entity** | "tell me about X", "what has Y been working on", person/org names | Entity graph lookup + ranked knowledge store docs |
| **temporal** | "last week", "April 2026", "decisions in March", "what happened recently" | Date-filtered retrieval; handles both absolute dates ("April 2026") and relative phrases ("last week") |
| **procedural** | "how do I", "what are the steps to", runbook queries | Path-weighted re-rank; step-relevant docs ranked above background |
| **multi_hop** | "connection between X and Y", "how does A relate to B" | Query decomposed into sub-queries, results fused; cross-encoder re-ranked if [rerank] extra installed |
| **semantic** | Abstract conceptual questions | Pure vector search with HyDE (hypothetical document embedding); cross-encoder re-ranked if [rerank] extra installed |

**You don't need to worry about this.** It's automatic. But if a query returns poor results, knowing the intent can help you rephrase it.

---

## What good results look like

A healthy search result in JSON format (`--json`) has:
```json
{
  "intent": "entity",
  "results": [...],
  "vec_count": 4,
  "bm25_count": 3,
  "vec_failed": false,
  "total_tokens": 1823
}
```

Key fields to check:
- `vec_failed: false` — vector search is working. If `true`, you're on BM25-only.
- `vec_count > 0` — vectors returned. If 0 with `vec_failed: false`, the query had no semantic matches.
- `results` — list of ranked documents with `path`, `score`, and `snippet`

---

## What to do when results are poor

### `-32602 Invalid request parameters` on every MCP call (post-v2026.5.3 only)

You're hitting the legacy `/sse` endpoint and the gateway is dropping the idle connection. Update your MCP client config to point at `/mcp` instead — see [MCP-CLIENT-MIGRATION.md](../operations/MCP-CLIENT-MIGRATION.md). The migration is a one-line URL change in your client config; the old `/sse` path stays mounted, so this is a fix for your client, not a kairix change.

### vec_failed=true (vector search broken)
This means Azure credentials aren't loaded. Every search falls back to BM25-only, which misses semantic matches.

**Do not proceed with a session on BM25-only retrieval.** Flag it and run:
```bash
kairix onboard check
```

This will tell you exactly which credential is missing and how to fix it.

### 0 results
Try rephrasing more specifically, or check if the relevant document store section has been embedded:
```bash
kairix search "the exact title of a document you know exists" --agent builder
```
If known documents don't appear, the document store may need a re-embed.

### Results seem off-topic
The intent classifier may have routed incorrectly. Try rephrasing:
- For entity queries: "tell me about [name]" or "what do we know about [organisation]"
- For temporal queries: include explicit relative time language ("last week", "this month", "recent")
- For procedural queries: start with "how do I" or "what are the steps"

---

## All subcommands

### search — the main tool
```bash
kairix search "<query>" --agent <name> [--budget N] [--json]
```

### brief — session briefing synthesis
Generates a ~800-token briefing synthesising relevant knowledge store content for the start of a session.
```bash
kairix brief <agent-name>
kairix brief shape --budget 5000
```
Output written to `$KAIRIX_DATA_DIR/briefing/<agent>-latest.md`.

### entity — entity graph lookup
```bash
kairix entity lookup "Jordan Blake"
kairix entity lookup "Acme"
```
Returns entity summary, type, vault_path, and related documents.

### curator health — entity graph health check
```bash
kairix curator health
kairix curator health --json
```
Reports: entity counts, synthesis failures (no summary), missing vault_paths.

### vault crawl — populate entity graph from document store
```bash
kairix vault crawl --vault-root /path/to/vault
kairix vault crawl --vault-root /path/to/vault --dry-run
```
Run after adding new organisation or person stubs to the document store.

### classify — route new knowledge to the right document store location
```bash
kairix classify "We decided to use PostgreSQL for the jobs table"
# → type: decision, destination: decisions.md, confidence: 0.95
```

### contradict — check new content against knowledge store
```bash
kairix contradict check "We use PostgreSQL for all persistence" --top-k 5
```
Returns contradicting knowledge store documents with conflict scores. Also exposed as the `tool_contradict` MCP tool (see below).

### contradict (MCP: tool_contradict) — check facts before writing
```bash
kairix contradict check "We use PostgreSQL for all persistence" --top-k 5
```
Also available as the `tool_contradict` MCP tool. Agents can call it before writing new content to verify it does not conflict with existing knowledge. Returns contradicting documents with conflict scores.

### onboard check — deployment diagnostics
```bash
kairix onboard check
kairix onboard check --json
```
Run this if search is behaving unexpectedly. Reports: PATH, wrapper, secrets, document store root, vector search, Neo4j.

### timeline — temporal query tools
```bash
kairix timeline query "decisions last week"
```

### wikilinks — inject entity links
```bash
kairix wikilinks inject --vault /path/to/vault
```

### benchmark — retrieval quality testing
```bash
kairix benchmark run --suite suites/example.yaml
```

---

## Common agent session patterns

### Session start (standard)
```bash
# Pull a briefing for context before the session
kairix brief shape

# Then search for session-specific context
kairix search "current status of [project]" --agent builder
kairix search "outstanding items from last week" --agent builder
```

### Researching an entity
```bash
# Start with entity lookup for curated summary
kairix entity lookup "Acme"

# Follow up with related knowledge store docs
kairix search "Acme engagement history and decisions" --agent builder
```

### Checking a decision or pattern
```bash
kairix search "how we decided to handle [topic]" --agent builder
kairix search "engineering pattern for [approach]" --agent builder
```

### Temporal research
```bash
kairix search "what decisions were made last month" --agent builder
kairix search "recent activity on the Azure connector" --agent builder
# Use explicit relative time language for best results
```

### Multi-hop / cross-entity
```bash
kairix search "connection between Acme and TechCorp on the platform project" --agent builder
```

### Research (MCP: tool_research)
The `tool_research` MCP tool runs iterative multi-turn search, refining queries until it finds a good answer. It always returns a synthesis — if no relevant documents are found, it synthesises what it can from the best available results rather than returning a failure message.

---

## Token budget guidance

| Use case | Recommended budget |
|---|---|
| Session-start context | 5000 (default) |
| Quick fact lookup | 2000 |
| Deep research | 8000–10000 |
| Briefing synthesis context | 5000 |

Set with `--budget N`. The budget caps total tokens returned, not the number of documents. Kairix ranks documents and returns as many as fit.

---

## Troubleshooting quick reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `command not found` | kairix not on PATH | Use `/usr/local/bin/kairix` or run `scripts/install.sh` |
| `vec_failed: true` | Azure credentials not loaded | Run `kairix onboard check`; fix secrets_loaded issue |
| 0 results, no error | Document store not embedded | Run `kairix embed --limit 20` to test |
| Results are all from one section | Scope issue | Check `--agent` flag is correct |
| Entity lookup returns nothing | Entity not in Neo4j | Run `kairix vault crawl --vault-root $KAIRIX_VAULT_ROOT` |
| Temporal query returns non-temporal docs | Time phrase not detected | Use relative ("last N days/weeks", "this month") or absolute ("April 2026", "March 15") date references |
| BM25-only (vec_count=0) with valid creds | usearch vector index not built | Run `kairix embed` to build the index |

---

## Capabilities — which surface to use

Every kairix capability has one Python implementation with one or more bindings (CLI, MCP). This table is the index for agents — search it for "diagnostics", "soak", "health", or any capability you're looking for and it tells you which surface to use.

| capability | when to use | how to invoke | surface |
|---|---|---|---|
| `tool_search` / `kairix search` | retrieve content from the knowledge store | MCP — direct | both |
| `tool_entity` / `kairix entity` | named-entity lookup (person, org, project) | MCP — direct | both |
| `tool_prep` / `kairix prep` | tiered L0/L1 context summary | MCP — direct | both |
| `tool_contradict` / `kairix contradict` | check new content for contradictions | MCP — direct | both |
| `tool_brief` / `kairix brief` | session briefing synthesis | MCP — direct | both |
| `tool_bootstrap` / `kairix bootstrap` | session-start orientation envelope | MCP — direct | both |
| `tool_onboard_check` / `kairix onboard check` | "is kairix healthy?" — read-only 9-probe envelope | MCP — direct | both |
| `tool_worker_status` / `kairix worker status` | "is the worker running?" — state file envelope | MCP — direct | both |
| `tool_soak_run` / `kairix soak run` | repeat-and-assert (memory, log volume, fd, determinism) | MCP returns escalation envelope; operator runs CLI | CLI |
| `tool_benchmark_run` / `kairix benchmark run` | retrieval quality measurement | MCP returns escalation envelope; operator runs CLI | CLI |
| `tool_embed` / `kairix embed` | embed documents into the vector index | MCP returns escalation envelope; operator runs CLI | CLI |
| `tool_store_crawl` / `kairix store crawl` | rebuild the Neo4j entity graph | MCP returns escalation envelope; operator runs CLI | CLI |
| `tool_embed_rebuild_fts` / `kairix embed rebuild-fts` | drop + re-create the FTS5 table | MCP returns escalation envelope; operator runs CLI | CLI |

The operator-only rows return an `OperatorOnlyCapability` envelope via MCP — surface the `operator_command` field to your admin if you need the work done.

---

## Getting help

```bash
kairix onboard check           # full deployment diagnostics
kairix --help                  # subcommand list
kairix search --help           # search-specific flags
```

If the diagnostics pass but results are still poor, run a benchmark to establish a baseline:
```bash
kairix benchmark run --suite suites/example.yaml --agent <name>
```

This guide is installed at `04-Agent-Knowledge/shared/kairix-usage.md` in the knowledge store and is searchable via `kairix search "how do I use kairix"`.
