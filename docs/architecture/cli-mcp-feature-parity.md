# CLI / MCP feature parity

## Goal

Every kairix feature is exposed via **both** the CLI and the MCP server with
uniform UX. Operators using the CLI and agents using MCP get the same
operations, the same parameters, the same output shapes, and the same
documentation — minimising agent turns and operator surprise.

## Principle

For every feature operation:

- **One use case** lives in `kairix/use_cases/<operation>.py`. It accepts
  the superset of parameters needed by either surface and returns a
  uniform result dataclass.
- **Two thin adapters** call the use case:
  - **CLI**: parses argv, calls the use case, formats stdout (and supports
    `--json` for the structured envelope).
  - **MCP**: validates JSON, calls the use case, returns the JSON envelope.
- **Adapters never own business logic.** Time-window extraction, query
  rewriting, agent/scope resolution, fall-through fall to alternate
  backends, error envelope construction — all live in the use case.
- **Uniform parameters.** Same names, same defaults, same enums across
  surfaces. `agent: str | None`, `scope: Scope = SHARED_AGENT` everywhere
  they apply.
- **Uniform output shapes.** A use case returns a dataclass; the CLI
  renders it for humans (or emits JSON with `--json`); the MCP envelope
  carries the same data.
- **Single source of truth for documentation.** The use case docstring
  drives both `--help` output and the MCP tool description.
- **Contract test per pair.** Same input → same output through both
  adapters. Catches drift before it ships.

## Audit (current state)

| Feature | Operations | CLI | MCP | Gap |
|---|---|---|---|---|
| **search** | search | `kairix search` | `mcp__search` | UX parity to verify |
| **contradict** | check | `kairix contradict check` | `mcp__contradict` | UX parity to verify |
| **timeline** | timeline | `kairix timeline` (uses temporal-chunks index) | `mcp__timeline` (uses BM25+vec hybrid) | Same op, different code paths — closes #163 on convergence |
| **entity** | suggest (NER from text) | `kairix entity suggest` | — | MCP missing |
| **entity** | validate (against Wikidata) | `kairix entity validate` | — | MCP missing |
| **entity** | get (lookup by name) | — | `mcp__entity` | CLI missing |
| **entity** | seed (discover from index) | `kairix entity seed` | — | Operator-only? Decide whether agent-facing |
| **prep** | prep | — | `mcp__prep` | CLI missing |
| **research** | research | — | `mcp__research` | CLI missing |
| **brief** | brief | `kairix brief` | — | MCP missing |
| **usage_guide** | get | — | `mcp__usage_guide` | CLI missing (also dogfood CONN-2 deployment-step gap) |

**Summary:** 8 surface-parity gaps + 1 code-path divergence + UX-parity
audit work for the two already-converged features.

## Phasing

Each phase ships independently on `develop`. Each Phase-1+ deliverable
includes a contract test pinning CLI ↔ MCP equivalence for the operation
it touches.

### Phase 1 — timeline (template + #163 closure)

- Build `kairix/use_cases/timeline.py` with `run_timeline(query, *, anchor_date=None, agent=None, scope=Scope.SHARED_AGENT, since=None, until=None, chunk_types=None, limit=10)` returning `TimelineResult`.
- Logic: extract time window → if window non-empty *and* the temporal-chunks index has matches, return temporal-chunks results; else fall through to `SearchPipeline.search` on the (possibly rewritten) query.
- `TimelineResult` is a dataclass with: `original_query`, `rewritten_query`, `is_temporal`, `fell_back`, `time_window` (dict), `results: list[TimelineHit]`, `error`.
- `TimelineHit` has `path`, `title`, `snippet`, `score`, `date` — superset of fields either surface produces today.
- Refactor `kairix/core/temporal/cli.py:main()` and `kairix/agents/mcp/server.py:tool_timeline` to be thin adapters around `run_timeline`.
- Contract test: same query → same paths through both adapters.
- Remove the diagnostic warning at `kairix/agents/mcp/server.py` (commit `e972b23`) — becomes obsolete once MCP uses the same code path as CLI.
- Closes #163.

### Phase 2 — UX parity audit on already-converged tools

- search and contradict: compare parameter signatures, defaults, output shapes. Pin parity in contract tests. Fix any drift.
- Decide on `--json` flag presence/default across all CLIs.
- Decide on the canonical agent/scope default presence across all tools that accept them.

### Phase 3 — fill missing surfaces, in operator priority order

Each is one use case + two thin adapters + contract test (~150–300 LOC of net change).

- **a. `mcp__brief`** — brief is dogfooded heavily; agents need it without shelling out to the CLI.
- **b. `mcp__entity_suggest` + `mcp__entity_validate`** — closes the agent-side entity gap surfaced in dogfood G-2; agents currently can't extract entities from prose without a CLI bridge.
- **c. `kairix prep` CLI** — debugging parity for prep failures; operators currently can't reproduce MCP prep output from a shell.
- **d. `kairix research` CLI** — operator can run research without booting the MCP server.
- **e. `kairix entity get` CLI** — lookup-by-name parity.
- **f. `kairix usage-guide` CLI** — also resolves the CONN-2 deployment-step gap (the usage guide must be onboarded; a CLI surface makes that more discoverable).

### Phase 4 — uniformity polish

- Single source of truth for help text — use case docstrings drive both `--help` and MCP tool descriptions (likely a small generator or shared constant).
- Final contract-test sweep: every (feature, operation) pair has CLI ↔ MCP equivalence pinned.
- Remove now-redundant per-surface tests where the pair contract covers them.

## Architectural decisions

These are confirmed once Phase 1 lands; later phases follow the pattern.

1. **Directory**: `kairix/use_cases/<operation>.py`. New top-level package
   for use cases. Co-locating with domain modules
   (`kairix/core/temporal/use_case.py`) was considered but rejected —
   with 8+ use cases incoming, a deliberate use-case layer is more
   durable than ad-hoc colocation.
2. **Result shape**: each use case returns its own dataclass
   (`TimelineResult`, `BriefResult`, `EntitySuggestion`, …). The
   dataclass is the contract; CLI and MCP serialise from it.
3. **Logic precedence in fall-through use cases** (timeline as the
   archetype): primary code path first (temporal-chunks index for
   timeline); fall through to secondary (search pipeline) only when the
   primary returns nothing useful. Both adapters consume the same
   precedence rule.
4. **Error handling**: use cases never raise to adapters. They return a
   result with a populated `error` field. Adapters surface that field
   verbatim — CLI as stderr or `error:` stdout entry; MCP as the
   envelope's `error` field. This subsumes #165 (MCP error envelope
   shape) — once the use case returns `{"error": "<class>: <message>"}`,
   the MCP envelope is automatically the right shape.

## Out of scope for this initiative

- **Replacing the per-CLI `argparse` + per-MCP `@server.tool()` boilerplate
  with a single registration framework.** Possible follow-up; not in
  Phase 4.
- **Cross-language parity** (e.g. exposing tools via gRPC, REST, or
  language-specific SDKs). Two surfaces (CLI + MCP) is the current
  scope.
- **Operator-only commands** (e.g. `kairix entity seed`, `kairix store
  crawl`, `kairix embed`). Decide per-feature whether an MCP surface
  exists in Phase 3 — defaults to no unless an agent use case is
  identified.

## Tracking

Umbrella issue: [#168](https://github.com/quanyeomans/kairix/issues/168).
Each phase is its own PR linked from the umbrella; each Phase-3 sub-item
is its own commit chain so they can be cherry-picked independently if
priorities shift.
