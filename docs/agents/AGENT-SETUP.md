# Agent setup: how to operate against kairix

You are an agent connected to a kairix deployment. This document tells you what to do at session start, what to call when, and how to behave when kairix degrades. Read it once at first run and again whenever your operating context changes.

The human who admins your kairix surface should also read this — it describes the contract you operate under, so they can spot when something has broken on the platform side.

The single design principle running through every section below: **every kairix output gives you maximum affordance to take your next step.** Read the envelope kairix returns. Act on what it tells you. Surface degradation to your human instead of pretending things are fine.

---

## 1. At session start: call `kairix bootstrap <your-agent-name>`

This is your orientation step. It returns a structured envelope with your role, your current `Board.md`, your last few daily memory files, your active goals, and the health of every kairix subsystem.

- **CLI form:** `kairix bootstrap <your-agent-name>`
- **MCP form:** `tool_bootstrap(agent="<your-agent-name>")`
- **JSON form:** `kairix bootstrap <your-agent-name> --json` (when you want a machine-readable envelope)

> **Forward reference:** `kairix bootstrap` ships in #246 W1. If the command is not yet available on your deployment, use the **Manual bootstrap fallback** at the bottom of this document and flag the gap to your human.

**What to do:** call it before doing anything else in a fresh session. Treat the returned envelope as your operating context — do not paraphrase or summarise the team's state from your prior memory; use the envelope kairix just gave you.

---

## 2. Before answering any factual question about prior work: call `tool_search`

`tool_search` indexes the team's knowledge store (notes, decisions, runbooks, agent memory, reference library) and returns ranked, relevant content with the most useful chunks first. You should be calling it **proactively**, not only when a human explicitly asks you to look something up.

Specific triggers — when you see any of these in the conversation, search before you answer:

- "what did we decide about ..."
- "have we seen this before"
- "what's the status of X"
- any reference to a project, client, person, or document by name
- any question of the form "how do we do Y"

**CLI form:** `kairix search "<your query>"`
**MCP form:** `tool_search(query="<your query>", agent="<your-agent-name>")`

**What to do:** search before answering. If your search returns zero results, say so explicitly to your human — do not fabricate from prior context.

---

## 3. When you need a synthesised view of a topic: call `tool_brief`

`tool_brief` runs a small research loop across the knowledge store and returns a structured briefing — what's known, what's open, what the most recent relevant material says. Use it when you would otherwise be tempted to summarise from memory.

**MCP form:** `tool_brief(agent="<your-agent-name>", topic="<topic>")`

**What to do:** call `tool_brief` when a human asks you to "brief me on X", "catch me up on Y", or "what's the state of Z". Do not write the briefing from your context window. Run the loop, return the structured briefing, and add your interpretation on top.

---

## 4. When you need facts about a specific named entity: call `tool_entity`

`tool_entity` is a direct knowledge-graph lookup — faster than search for "who is X" or "what does the team know about company Y". It returns the entity's identity, related entities, and the documents that mention it.

**MCP form:** `tool_entity(name="<entity name>")`

**What to do:** when a human names a person, company, or project, call `tool_entity` first before falling back to `tool_search`. If `tool_entity` returns `not found`, the entity may be missing from the entity allowlist — see `ADMIN-CONVERSATION.md` for what to say to your admin.

---

## 5. Health degradation contract

Every kairix tool response includes a `health` envelope:

```json
{
  "results": [...],
  "health": {
    "vector_search": "ok" | "degraded" | "offline",
    "bm25": "ok" | "offline",
    "chat": "ok" | "offline",
    "secrets_loaded": true,
    "degraded_reason": "Missing KAIRIX_LLM_API_KEY — vector results unavailable; falling back to BM25",
    "next_action": "Run `kairix onboard check` for full diagnostic, or ask your admin to re-run `kairix-fetch-secrets.service`"
  }
}
```

**Read it on every response.** If any of these are true, your kairix surface is degraded:

- `health.vector_search != "ok"` — semantic ranking is offline; you are getting BM25-only results
- `health.secrets_loaded: false` — kairix cannot reach its credentials; synthesis features are offline
- `health.bm25 == "offline"` — even keyword search is offline; results may be empty or stale
- `health.chat == "offline"` — `tool_brief` and other synthesis tools will not work

**What to do when degraded:**

1. **Do not silently fail.** Keep working with whatever subsystem is still up — degraded results are usually better than no results.
2. **Surface the degradation to your human in your next reply.** Quote `health.degraded_reason` verbatim and quote `health.next_action` verbatim. They are written to be actionable; do not paraphrase.
3. **Say what you fell back to.** "I searched kairix but vector ranking is offline; these results are BM25-only and may rank less well by meaning."
4. **Refer your human to `ADMIN-CONVERSATION.md`** if the remediation is an admin action (it almost always is).

> **Forward reference:** the `health` envelope ships in #246 W3. Until that lands, kairix tool responses do not carry `health` and you should treat an empty result as ambiguous. Once W3 ships, treat the envelope as the source of truth for "is kairix working right now".

---

## 6. When `kairix onboard check` returns non-zero

`kairix onboard check` is the canonical "is kairix healthy" probe. It runs 9 checks, in dependency order (PATH → wrapper → secrets → document root → vector search → Neo4j → agent memory → chunk dates → MCP service). A green run prints `9/9 passed`.

**MCP form:** if your human asks you to run it, ask them to run it on the host — you cannot run shell commands. The output is structured and includes a remediation string for every failed check.

**JSON form:** `kairix onboard check --json` returns a machine-readable envelope:

```json
{
  "passed": 7,
  "total": 9,
  "failures": [
    {
      "check": "secrets_loaded",
      "detail": "LLM credentials not found in environment or secrets file: KAIRIX_LLM_API_KEY, KAIRIX_LLM_ENDPOINT",
      "remediation": "Create /opt/kairix/secrets.env with KAIRIX_LLM_API_KEY=<value> and KAIRIX_LLM_ENDPOINT=<value> ..."
    }
  ]
}
```

> **Forward reference:** `--json` and structured exit code ship in #246 W4. Until then, the human-readable text is what you get; parse the `X/Y passed` line and read each failure block verbatim.

**What to do when the check fails:**

1. **Surface the full failure block to your human, verbatim.** Each failure includes a `remediation` string — quote it. The strings are written so the admin can act on them without translation.
2. **Do not attempt to fix systemd units or container internals yourself.** That is an admin action. You do not have shell access to the host and would not have permission to use it if you did. Refer your human to `ADMIN-CONVERSATION.md` for the right script to follow with their admin.
3. **Note which capabilities are degraded as a result.** If `secrets_loaded` failed, expect vector search and synthesis to be offline; tell your human upfront. If `neo4j_reachable` failed, entity lookup is degraded; tell your human upfront.

---

## 7. Manual bootstrap fallback

For the pre-W1 case (where `kairix bootstrap` is not yet available) or when `kairix bootstrap` fails, fall back to reading these files directly. This is the minimum context you need to operate; **flag to your human that `kairix bootstrap` is offline** so they can chase the underlying cause.

- **Your workspace memory** — `/data/workspaces/<your-agent>/MEMORY.md`
- **Your current board** — `/data/obsidian-vault/04-Agent-Knowledge/<your-agent>/Board.md`
- **Your most recent daily memory files** — the three most recent entries in `/data/obsidian-vault/04-Agent-Knowledge/<your-agent>/memory/YYYY-MM-DD.md`

If your deployment uses a different document root, replace `/data/obsidian-vault` with whatever path is set as `KAIRIX_DOCUMENT_ROOT`. Your admin can confirm with `kairix onboard check` (the `document_root_configured` check prints the path).

**What to do:** read the three sources, summarise the situation to yourself (not the human), and proceed. In your first reply, mention you bootstrapped manually and that `kairix bootstrap` is offline — that is a signal to your admin that something needs attention.

---

## 8. Quick reference card

| Situation | Call this |
|-----------|----------|
| Session start | `kairix bootstrap <your-agent-name>` (W1) |
| Factual question about prior work | `tool_search` |
| "Brief me on X" / "catch me up" | `tool_brief` |
| "Who is X" / "what's company Y" | `tool_entity` |
| Health envelope says degraded | Surface `degraded_reason` + `next_action` to your human |
| `kairix onboard check` returns non-zero | Surface the failure block + `remediation` strings; refer admin to `ADMIN-CONVERSATION.md` |
| `kairix bootstrap` not available yet | Manual fallback (section 7); flag to your human |

---

## Related

- [`ADMIN-CONVERSATION.md`](ADMIN-CONVERSATION.md) — what to say to your admin when kairix is misconfigured or degraded
- [Quick start](../getting-started/quick-start.md) — full install / configure flow for your admin
- [MCP tools reference](../user-guide/mcp-tools.md) — full parameter list for every tool
- [Agent usage guide](../user-guide/agent-usage-guide.md) — what agents can do with kairix in depth
