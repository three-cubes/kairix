# Kairix

**Give your agents the same knowledge as your team — without giving it away.**

[![Apache 2.0](https://img.shields.io/badge/licence-Apache%202.0-blue)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-1647_passing-brightgreen)]()
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

### Option A: pip install

```bash
pip install "kairix[agents,neo4j]"
kairix setup                   # interactive wizard — picks your paths, ports, collections
kairix embed                   # index your documents
kairix search "your question"  # find answers
kairix mcp serve               # start MCP server for agent integration
```

### Option B: Docker Compose

```bash
curl -O https://raw.githubusercontent.com/quanyeomans/kairix/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/quanyeomans/kairix/main/.env.example
cp .env.example .env        # add your LLM API key
ln -s ~/my-notes ./documents # point to your documents
docker compose up -d         # starts kairix + worker + Neo4j
```

**Verify it works** — the container includes a reference library and gold suite. After setup:
```bash
docker compose exec kairix kairix eval    # runs 200-case benchmark, prints scores
docker compose exec kairix kairix onboard check   # verifies all 9 deployment checks pass
```

See the [full quick-start guide](docs/getting-started/quick-start.md) for detailed setup.

**What you need:**
- Python 3.10+ (Option A) or Docker (Option B)
- An LLM API key for embeddings (Azure OpenAI or any OpenAI-compatible API)
- A folder of documents

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
git clone https://github.com/quanyeomans/kairix
cd kairix
pip install -e ".[dev,neo4j,agents,rerank]"
pytest tests/                    # 1,675 tests
ruff check kairix/ tests/        # lint
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture, PR process, and versioning.

---

## Licence

Apache 2.0 — see [LICENSE](LICENSE).

Built on: [usearch](https://github.com/unum-cloud/usearch) (Unum Cloud), [SQLite FTS5](https://www.sqlite.org/fts5.html), [Neo4j Community Edition](https://neo4j.com/).
