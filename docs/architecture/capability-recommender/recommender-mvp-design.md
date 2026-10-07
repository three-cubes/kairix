# Capability Recommender — MVP Design (Spec A)

**Status:** Shipped (v2026.6.18 line) — PRs [#569](https://github.com/three-cubes/kairix/pull/569)
(MVP) + [#570](https://github.com/three-cubes/kairix/pull/570) (review follow-ups),
both MERGED 2026-06-20. The shipped surface: `kairix/use_cases/recommend.py`
(`run_recommend` + `main`), `kairix/knowledge/capabilities/builder.py`
(`CapabilityCatalogueBuilder` + `build_capability_corpus`), the
`recommend_capabilities` MCP tool + `tool_recommend` adapter
(`kairix/agents/mcp/server.py`), the `kairix recommend` CLI subcommand
(`kairix/cli.py`), the `skills` connector (`kairix/connectors/skills/`), the
gold eval set (`kairix/data/suites/recommender.yaml`), and the `recommender` +
`connector_skills` flags (`kairix/core/features/registry.py`). This document is
retained as the design substrate Spec B builds on; the code sections below are
described in the original design tense and are accurate to the shipped surface
except where a "Shipped as:" note records a deviation.
**Scope:** Unified capability corpus + hot-path recommender. The cold-path
affordance-improvement loop is Spec B (`affordance-loop-design.md`, not yet
authored), built on top of this.

---

## 1. Goal

Give an agent one call that answers *"which tool, skill, slash-command,
sub-agent, or workflow should I reach for to do this task?"* — returning a
ranked, ready-to-invoke list, fast and deterministically, with no LLM between
the agent and its tools.

Today an agent must already know kairix's ~40 CLI subcommands and ~27 MCP
tools, plus the ~95 installed external skills/commands/agents (and 234
advertised plugins), to pick the right one. The descriptions exist but are
scattered across Python decorators, a hand-maintained markdown table, and
on-disk YAML frontmatter. The recommender unifies them into one searchable
corpus and ranks them against a task description.

## 2. Architecture in one paragraph

The recommender is a **search over the capability-bearing collections**
(`capabilities` for kairix's own caps + `skills` for the external skills
connector's output) using the existing `SearchPipeline` (BM25 + vector → RRF →
cross-encoder rerank). The retrieval/embed/fusion/rerank stack is reused
verbatim. The genuinely new code
is (a) two **feeders** that assemble the corpus into the collection, (b) a thin
`run_recommend` **use case** with **CLI + MCP adapters**, and (c) a **gold eval
set** that measures recommendation precision. Everything is gated behind a
default-OFF feature flag (`recommender`); the corpus feeder's external half is a
default-OFF connector (`connector_skills`).

```
                    ┌─────────────────────────────────────────┐
   Feeder 1 ───────▶│                                          │
   kairix caps      │   capabilities collection                │
   (CLI+MCP+_cap)   │   (documents + content + vec index)      │◀──── Feeder 2
                    │                                          │      skills connector
                    └───────────────────┬─────────────────────┘      (~/.claude/**)
                                         │
                            build_search_pipeline(config)
                                         │
   agent ──▶ recommend_capabilities(task) / kairix recommend "<task>"
                                         │
                       BM25+vector RRF + cross-encoder rerank
                                         │
                    RecommendOutput(recommendations=( … ranked … ))
                       each: name, kind, surface, when_to_use,
                             score, mcp_tool|cli  (ready to call)
```

## 3. The unified corpus (`capabilities` collection)

### 3.1 Why a real collection, not a side index

The usearch vector index does **not** store capability text inline — after ANN
it re-hydrates result metadata by joining back to the SQLite `documents` /
`content` tables on `hash` (`kairix/core/search/vec_index.py`). A capability
vector is therefore only useful if a matching `documents` row exists with the
same `hash`. So every capability must be written through the **normal doc-store
+ embed path** (`Repository.insert_or_update(path, collection, title, content,
content_hash)` + `EmbeddingService.embed_batch`). This is also a feature: writing
capabilities as documents gives BM25 keyword matching on names like `Grep`,
`Linear`, `brainstorming` for free, so the hybrid leg works without extra wiring.

### 3.2 Capability document shape

One document per capability, all in `collection="capabilities"`:

| Field | Source |
|---|---|
| `path` | stable id, e.g. `cap://kairix/search`, `cap://skill/brainstorming`, `cap://command/feature-dev`, `cap://agent/code-architect` |
| `title` | capability display name |
| `content` | the **retrieval document**: `when_to_use` trigger text + description + (for kairix caps) category + invocation; (for external) the frontmatter `description` + `keywords` |
| `content_hash` | hash of content, for change detection + the vec join |

The capability's **structured metadata** (kind, surface, mcp_tool, cli, source
version) travels via the per-source metadata channel
(`SourceMetadata` / `metadata_for`, ADR-021) so the recommender can echo a
ready-to-call invocation without re-parsing the content.

### 3.3 Feeder 1 — kairix's own surface (`CapabilityCatalogueBuilder`)

> **Shipped as:** `kairix/knowledge/capabilities/builder.py`
> (`CapabilityCatalogueBuilder` + `build_capability_corpus` +
> `build_capability_chunk`). The §11 file map's placeholder
> `kairix/core/<…>/capability_catalogue.py` was resolved to this path.

A plain builder (not a connector — it introspects the running process, not an
external source). It unifies the three in-repo capability surfaces:

- `tool_capabilities()` `_cap(...)` rows (`kairix/agents/mcp/server.py`) — the
  structured registry (name, mcp_tool, cli, category). **Extended** with a new
  `when_to_use: str` field so each row carries task-conditioned trigger text.
- The `@server.tool(description=…)` text — the richest, most intent-rich
  descriptions ("Call before answering any factual question…").
- `kairix/cli.py` `COMMANDS` + module docstring one-liners — for CLI-only
  surfaces.

It emits one capability document per use case (deduplicated by use case so a
CLI/MCP pair that maps to one `kairix/use_cases/<op>.py` appears once, per the
CLI↔MCP parity invariant), writing through the doc store + embed.

`when_to_use` is seeded from the existing hand-written
`agent-usage-guide.md:305-325` "Capabilities — which surface to use" table where
present, otherwise from the MCP description's "Call when…" sentence.

### 3.4 Feeder 2 — external skills/workflows (`kairix/connectors/skills/`)

A new connector (Obsidian-walker clone) declaring
`{SourceConnector, PollConnector, SlimConnector}` (no credentials — local
filesystem, no auth). It walks, on the host the kairix instance runs on:

- `~/.claude/plugins/cache/**/skills/<name>/SKILL.md`
- `~/.claude/plugins/cache/**/commands/<cmd>.md`
- `~/.claude/plugins/cache/**/agents/<agent>.md`
- `~/.claude/skills/*.md` (flat files — the parser handles both the
  `<dir>/SKILL.md` and flat-`.md` shapes)

For each, it parses the YAML frontmatter (`name`, `description`, and where
present `tools`, `keywords`); `description` is the retrieval document, the body
is payload. It **dedups by `name`, preferring the installed/active version**
(the cache holds multiple versions of the same plugin — e.g. superpowers 4.0.3
*and* 6.0.3 — so naive ingest would surface stale duplicates).

**Graceful degrade:** where `~/.claude` is absent (the production VM), the
connector finds nothing and the corpus is kairix-caps-only — a warn-and-continue,
never an error. This matches the deployment reality: agents with a large skills
set run on hosts that have it; the VM gets the always-present kairix half.

Connector-framework rules apply: F34/F35 (no cross-connector imports), F56
(declares SourceConnector + a capability mix), F65 (metadata_for + propagation),
F66 (per_tick_max_items + disk_watermark_min_free_bytes), F36 (BDD feature +
Examples-table row). Sensitivity: `internal` (local dev-tooling metadata, not
secret).

### 3.5 Corpus build trigger + the capability-bearing collections

Each feeder writes to its **natural** collection, and the recommender ranks
over **both**:

- **Feeder 1** (`build_capability_corpus` → `CapabilityCatalogueBuilder`) writes
  kairix's own caps into the **`capabilities`** collection. It is invoked at
  worker startup when the `recommender` flag is ON (so the corpus is fresh on
  boot), and via the existing maintenance surface for an explicit re-index.
- **Feeder 2** (the `skills` connector) writes into the **`skills`** collection.
  The connector framework routes a connector's output to a collection named
  after the connector — `run_connector_sync_pipeline` →
  `resolve_chunk_writer_for_entry(db, name="skills")` →
  `legacy_chunk_writer(db, collection="skills")` — so the skills half lands in
  `skills`, **not** `capabilities`. It refreshes on the connector's normal poll
  tick.

`run_recommend` therefore queries **both** collections
(`collections=["capabilities", "skills"]`, `agent=None`) so externally-installed
skills are reachable, not just kairix's own caps. (An earlier draft had Feeder 2
also write to `capabilities`; reconciling with the connector-framework routing,
each feeder keeps its natural collection and the *reader* unifies them — a
contained change with no connector-routing blast radius.) If both collections
are empty at query time, `run_recommend` returns an empty result (it never
raises).

## 4. Hot path — the recommender

### 4.1 Use case (`kairix/use_cases/recommend.py`)

Mirrors `kairix/use_cases/usage_guide.py` (the cleanest read-only template):

```python
@dataclass(frozen=True)
class CapabilityRecommendation:
    name: str
    kind: str          # "tool" | "skill" | "command" | "agent" | "workflow"
    surface: str       # "mcp" | "cli" | "both" | "external"
    when_to_use: str
    score: float
    mcp_tool: str = ""    # ready-to-call binding (empty if N/A)
    cli: str = ""         # e.g. "kairix search"
    source: str = ""      # plugin/version provenance for external caps

@dataclass(frozen=True)
class RecommendOutput:
    task: str = ""
    recommendations: tuple[CapabilityRecommendation, ...] = ()
    correlation_id: str = ""   # forward-compat: Spec B's log keys on this
    error: str = ""

@dataclass(frozen=True)
class RecommendDeps:
    search: Callable[..., SearchResult] = field(default_factory=_default_search)
    # default_factory wires build_search_pipeline(load_config()).search,
    # never Optional[Callable] (the #204 frozen-deps pattern)

def run_recommend(task: str, *, agent: str | None = None,
                  limit: int = 5, deps: RecommendDeps | None = None) -> RecommendOutput: ...

def recommend_output_to_envelope(out: RecommendOutput) -> dict[str, Any]: ...
```

`run_recommend`:
1. resolves deps (default → real search pipeline pointed at the
   capability-bearing collections `["capabilities", "skills"]`),
2. calls `search(task, collections=["capabilities", "skills"], agent=None)` —
   the explicit collection list with `agent=None` short-circuits scope
   resolution (both are globally readable); `capabilities` carries kairix's own
   caps and `skills` carries the external skills connector's output,
3. maps each `SearchResult` hit to a `CapabilityRecommendation`, reading the
   structured invocation from per-source metadata,
4. excludes the recommender itself (self-reference guard),
5. mints a `correlation_id` (passed in via deps for determinism in tests),
6. **never raises** — populates `error` on any failure.

Cross-encoder rerank is force-enabled for this collection via a
recommender-specific `RetrievalConfig` (precision@1-3 over a small corpus matters
more than recall; the per-config pipeline cache keeps it isolated from the main
search pipeline). No LLM call.

`correlation_id` is included now so the data shape is stable; Spec B builds the
`recommend_log` table + outcome capture + offline analysis keyed on it. Spec A
does **not** create the log table (no reader exists yet — that's deliberately a
Spec B concern so the schema reflects this real output shape). It is the join
key Spec B uses to correlate against the eval-feedback-loop EPIC #465 call-logs.

### 4.2 Adapters (one use case, two thin adapters)

- **CLI:** add `"recommend": ("kairix.use_cases.recommend", "main", True)` to
  `kairix/cli.py:COMMANDS` + a docstring line. The adapter parses argv, calls
  `run_recommend`, prints a human table (and `--json` → envelope).
- **MCP:** module-level `tool_recommend(task, *, agent=None, deps=None) ->
  dict[str, Any]` thin adapter + registration:

  ```python
  @server.tool(description=(
      "Call when you are unsure which kairix tool, skill, slash-command, "
      "sub-agent, or workflow fits a task. Describe the task; get a ranked "
      "list of capabilities, each with why-it-fits and a ready-to-call "
      "invocation. Expected p99: 1s warm. Recommended client timeout: 10s."))
  @async_tool_handler
  def recommend_capabilities(task: str, agent: str = "") -> dict[str, Any]:
      return tool_recommend(task=task, agent=agent or None)
  ```
- Add a `_cap(name="recommend", mcp_tool="recommend_capabilities",
  cli="kairix recommend", category=CAP_CATEGORY_AGENT,
  when_to_use="…")` row to `tool_capabilities()` so the recommender is itself
  discoverable.

`agent` is accepted on both surfaces for parity and forward use (an agent's
available toolset may differ); v1 logs it but does not personalise ranking.

## 5. Feature flags (default-safe cutover)

| Flag | Default | Gates |
|---|---|---|
| `recommender` | OFF | the `recommend`/`recommend_capabilities` surfaces + corpus build of kairix caps |
| `connector_skills` | OFF | Feeder 2 (external skills connector) |

Both follow the canonical cutover: default-safe (§2.1), both-branch tested (F54:
OFF + ON BDD scenarios, integration both branches, E2E composed-path), mechanical
retirement (F51: `target_retire_in`). With both OFF, installing this code is a
no-op for operators.

## 6. Data flow

**Ingest (corpus build):** Feeder 1 (`build_capability_corpus`) introspects
kairix surfaces → doc store (`insert_or_update`) + `embed_batch` →
**`capabilities`** collection; Feeder 2 (the `skills` connector) walks
`~/.claude` → the standard ConnectorPipeline → **`skills`** collection (the
connector-named collection). Both land FTS rows + vec index entries joined on
hash.

**Query (recommend):** agent → `recommend_capabilities(task)` →
`run_recommend` → `search(task, collections=["capabilities", "skills"])` →
BM25+vector parallel → RRF fuse → cross-encoder rerank → top-k → map to
`CapabilityRecommendation` (with invocation from metadata) → envelope → agent's
next turn is a direct call to the top result's `mcp_tool`/`cli`.

## 7. Error handling

- `run_recommend` never raises; all failure modes populate `error` and return an
  empty/partial `recommendations` tuple (the use-cases-never-raise contract).
- Empty corpus → `error="capability corpus not built yet — run a re-index"`.
- Vector leg failure → degrade to BM25-only (the pipeline already does this;
  `SearchResult.vec_failed` surfaces it).
- Skills connector on a host with no `~/.claude` → warn + continue (degrade to
  kairix-caps-only), never fail the tick.
- Malformed skill frontmatter → log + skip that one item (per-item isolation,
  the rest of the tick lands).

## 8. Testing strategy

Mechanical gates that fire and the tests that satisfy them:

| Rule | Test |
|---|---|
| F45 (new-capability BDD) | `tests/bdd/features/cli_recommend.feature`, `tests/bdd/features/mcp_recommend_capabilities.feature`, `tests/bdd/features/connector_skills.feature` — each with a happy-path scenario (F12), no implementation symbols (F13) |
| F30 (outcome tests) | `tests/integration/test_outcome_recommend.py` — direct `tool_recommend(task=…)` call asserting on envelope `recommendations` content (not return code / fake call-counts), with an **executed** sabotage-proof; CLI outcome via `subprocess.run([sys.executable, "-m", "kairix.cli", "recommend", …])` |
| F46 (BDD composes via factory) | step impls go through CLI/MCP/`build_*` with `FakePaths(...)` |
| F47 (integration via factory) | corpus + pipeline constructed via `kairix.core.factory.build_*` |
| F48 (E2E composed path) | `tests/e2e/test_composed_recommender_path.py` (`@pytest.mark.e2e`): config → factory.build → corpus build → recommend → assert top result matches a seeded capability |
| F54 (flag both-branch) | OFF + ON scenarios for `recommender` and `connector_skills` |
| CLI↔MCP parity | `tests/contracts/test_cli_mcp_parity_recommend.py` — same task → same recommendations through both adapters |
| F36 (connector plugin) | `connector_skills.feature` + Examples-table row in the E2E connector features |
| F1/F2/F5/F6 | inject `RecommendDeps` / fakes from `tests/fakes.py`; no `@patch`, no `KAIRIX_*` setenv, public surface only, no `*_fn=None` |
| F7/F9 | ≥90% per-file coverage on the new files |
| Full-tree gate | new files born clean — every F-rule runs over the whole tree with no baseline |

**Recommendation-quality eval (F75-forward):** a gold set of `task → expected
capability` cases under `kairix/data/suites/` (re-using the `SuiteRunner`
harness with `score_method: exact` = "expected capability in top-k"). This is
the recommender's accuracy gate and seeds the F75 capability↔question mapping if
that rule is later implemented.

**Fakes:** `FakeSearchPipeline` returning a fixed `SearchResult` for the hot
path; a `tmp_path`-rooted fake skills tree for the connector (frontmatter
fixtures using generic names — F32). No real `~/.claude` reads in tests.

## 9. What this spec deliberately excludes (→ Spec B)

- The `recommend_log` table, the `recommend_feedback` MCP tool, and the outcome
  signal (`invoked` / `useful`).
- The offline LLM analysis job and the `affordance-report` surface.
- Correlation with the eval-feedback-loop EPIC #465's downstream call-logs
  (#465 and its layers #538–#542 have since shipped; Spec B is the consumer).

Spec A only emits the stable output shape (incl. `correlation_id`) those build
on.

## 10. Open questions resolved during brainstorming

| Question | Resolution |
|---|---|
| Corpus scope | Unified — kairix caps + external skills/workflows |
| External-corpus reachability | Local-host connector, graceful-degrade where `~/.claude` absent |
| Matching engine | Pure retrieval + cross-encoder rerank, no query-time LLM |
| Plugin/cap descriptions | Extend `_cap` with `when_to_use` (kairix); frontmatter `description` (external) |
| Static vs runtime corpus | Built into a real collection via the doc-store+embed path; refreshed on flag-on boot, connector tick, and explicit re-index |
| Recommend granularity | Over use cases (deduped CLI/MCP pairs) + external skills/commands/agents |
| Self-reference | Recommender excludes itself from results |

## 11. Component / file map

| File | Responsibility | New/modify |
|---|---|---|
| `kairix/connectors/skills/connector.py` | Feeder 2 — walk `~/.claude/**`, parse frontmatter, dedup, degrade | new |
| `kairix/connectors/skills/{__init__.py,fs.py,render.py,py.typed,README.md,DEPENDENCIES.md}` | connector package (Obsidian-clone shape) | new |
| `kairix/knowledge/capabilities/builder.py` | Feeder 1 — `CapabilityCatalogueBuilder` + `build_capability_corpus` | new |
| `kairix/agents/mcp/server.py` | extend `_cap(...)` with `when_to_use`; add `tool_recommend` + `recommend_capabilities`; add recommender `_cap` row | modify |
| `kairix/use_cases/recommend.py` | `run_recommend`, dataclasses, envelope | new |
| `kairix/cli.py` | `recommend` in `COMMANDS` + docstring | modify |
| `pyproject.toml` | `kairix.connectors` entry-point `skills` | modify |
| `kairix/core/features/registry.py` (REGISTRY) | `recommender`, `connector_skills` flags | modify |
| `kairix/data/suites/recommender.yaml` | gold task→capability eval set | new |
| `tests/bdd/features/*`, `tests/integration/*`, `tests/e2e/*`, `tests/contracts/*` | per §8 | new |

(As shipped: Feeder 1 lives at `kairix/knowledge/capabilities/builder.py` and the
flag REGISTRY at `kairix/core/features/registry.py`.)
