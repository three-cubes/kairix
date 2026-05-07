# Planning: Paths dependency-injection refactor

**Status:** Phase 0 in progress (2026-05-07)
**Target version:** v2026.6.x rolling — phases ship as separate PRs into `develop`
**Owner:** maintainers (high-risk surfaces); delegated agents (Ralph pattern, low-risk surfaces)
**Primary motivation:** Eliminate `monkeypatch.setenv("KAIRIX_*")` and `_resolve_cached.cache_clear()` from the test suite. Production code reads paths via module-level `document_root()` / `db_path()` / `workspace_root()` / `agent_memory_path()` functions backed by `_resolve_cached`, an `lru_cache`-d env-var reader. Tests have no DI seam, so the only way to override paths is monkeypatching env vars and clearing the cache. ~30 test files do this; the convention is described in `tests/wikilinks/conftest.py` as "the only mutation primitive kairix tests should use" — that comment is wrong, and the user has explicitly disowned it.

---

## Problem statement

Three concrete smells in the current shape:

1. **Hidden global state.** Production reads paths via module-level functions that call `KairixPaths.resolve()` → `_resolve_cached()` → env vars. Every callsite is implicitly coupled to process-wide state.
2. **Test-shaped API in production.** Tests have to mutate that state via `monkeypatch.setenv` + `_resolve_cached.cache_clear()`. Production code didn't expose any DI seam, so the test surface is the env-var surface.
3. **Pattern inconsistency.** `DefaultCollectionResolver` and `ConfigDrivenAgentRegistry` are already constructed at the boundary (factory.py) per the "G4: config at boundary" rule. Paths are the lone holdout.

The aim is to make paths follow the same boundary pattern: construct `KairixPaths` once at each entry point (CLI, factory, MCP server, embed worker), pass it into pipelines that need it, and let tests inject `FakePaths` from `tests/fakes.py` directly.

---

## Goals & non-goals

### Goals (this initiative)

1. Every production function/class that needs paths takes a `paths: KairixPaths` argument explicitly. No new code reads paths from module-level state.
2. Every test that overrode paths via `monkeypatch.setenv("KAIRIX_*")` constructs `FakePaths` explicitly and injects it.
3. CI grep gate that **fails** on `monkeypatch.setenv("KAIRIX_` once Phase 4 is complete.
4. Module-level convenience functions removed (or kept as deprecated shims for ad-hoc scripts).

### Non-goals (this initiative)

- **Credentials DI** (`KAIRIX_AZURE_API_KEY`, `KAIRIX_LLM_API_KEY`) — same anti-pattern, separate Phase 5 follow-up.
- **Embed-backend DI** (`KAIRIX_EMBED_BACKEND=fake` autouse fixture in `tests/conftest.py`) — same anti-pattern, Phase 5.
- **Removing `@patch` and `monkeypatch.setattr` usage** — different smell (substituting imports / functions vs env vars). Tracked separately under "fakes-only test isolation".
- **Refactoring `KAIRIX_AGENT_MEMORY_ROOT`** — `agent_memory_path()` reads it directly outside `_resolve_cached`. Cleaned up incidentally during the briefing-domain refactor (Phase 3).

---

## Surface inventory

### Production callsites of `kairix.paths` (16 files)

```
kairix/agents/briefing/pipeline.py
kairix/agents/briefing/sources.py
kairix/core/classify/router.py
kairix/core/embed/cli.py
kairix/core/embed/deps.py
kairix/core/embed/embed.py
kairix/core/search/budget.py
kairix/core/search/hybrid.py
kairix/core/temporal/index.py
kairix/knowledge/summaries/cli.py
kairix/knowledge/summaries/staleness.py
kairix/knowledge/wikilinks/audit.py
kairix/knowledge/wikilinks/cli.py
kairix/knowledge/wikilinks/injector.py
kairix/platform/onboard/check.py
kairix/platform/setup/wizard.py
```

### Test files with `monkeypatch.setenv("KAIRIX_*")` (~30)

Across `tests/wikilinks/`, `tests/embed/`, `tests/search/`, `tests/core/search/`, `tests/mcp/`, `tests/agents/mcp/`, `tests/eval/`, `tests/integration/`, `tests/e2e/`, `tests/setup/`, `tests/db/`, plus root `tests/conftest.py` and individual files like `tests/test_paths.py`, `tests/test_secrets.py`, `tests/test_agent_memory_path_regression.py`.

### Back-pressure map (BDD + integration coverage by domain)

| Domain | Production | BDD | Integration | Delegation safety |
|---|---|---|---|---|
| Search/scope/hybrid | search/hybrid.py, search/budget.py | search_intents, search_dedup, mcp_agent_search, configurable_default_scope | search_pipeline, collections | **delegate** |
| Embed/scanner | embed/embed.py, embed/cli.py, embed/deps.py | recall_check | embed_scan_dedup, db_roundtrip | **delegate** |
| Wikilinks | wikilinks/{injector,resolver,audit,cli}.py | (none — Phase 0 adds) | (none — Phase 0 adds) | **owner — pilot** |
| Briefing | briefing/{sources,pipeline}.py | (none) | briefing_pipeline, timeline_retrieval | **delegate** |
| Temporal | temporal/index.py | chunk_date_fallback, timeline_absolute | timeline_retrieval | **delegate** |
| MCP tools | (server entry) | mcp_agent_{search,entity,timeline,prep,contradict} | mcp_tool_contracts | **delegate** |
| Eval | (cli) | eval_{auto_gold,gate,tune} | eval_gate_cli | **delegate** |
| Reflib/normalise | reflib loader/normalise | reference_library, normalisation | (contract tests) | **delegate** |
| Curator/onboard | onboard/check.py | curator_health, onboard_check | (none) | **delegate** |
| Summaries | summaries/{cli,staleness}.py | (none) | (none — unit only) | **owner** |
| Classify router | classify/router.py | (none — used inside intent pipeline) | indirect via search_pipeline | **owner** |
| Setup wizard | setup/wizard.py | (none — interactive) | (none) | **owner** |

---

## The seam

`KairixPaths` (already a frozen dataclass) becomes the only paths surface. The boundary discipline:

```python
# Production callsite (function form)
def inject_wikilinks(content: str, entities: list[WikiEntity], *, paths: KairixPaths | None = None) -> tuple[str, list[str]]:
    paths = paths or KairixPaths.resolve()
    # ... use paths.document_root, paths.workspace_root, etc.

# Production callsite (class form — preferred when path config is durable)
class WikiLinksAuditor:
    def __init__(self, *, paths: KairixPaths) -> None:
        self._paths = paths
    def audit(self) -> AuditReport:
        # ... use self._paths.document_root

# Entry point (factory/CLI/MCP startup)
def build_pipeline() -> SearchPipeline:
    paths = KairixPaths.resolve()  # the *only* place .resolve() is called in production
    return SearchPipeline(..., paths=paths)

# Test
from tests.fakes import FakePaths

def test_inject_eligibility(tmp_path: Path) -> None:
    paths = FakePaths(document_root=tmp_path / "vault", workspace_root=tmp_path / "ws")
    result = inject_wikilinks("...", [...], paths=paths)
    assert ...
```

**Module-level functions** (`document_root()`, `db_path()`, etc.) stay as deprecated shims for one release window so the diff stays bounded per PR. Phase 4 deletes them.

**`FakePaths`** is a constructor helper — not a separate type. Returns a real `KairixPaths` built from explicit arguments. This keeps the production surface narrow (only one paths type exists).

```python
# tests/fakes.py
def FakePaths(
    *,
    document_root: Path | str = "/fake/document_root",
    db_path: Path | str = "/fake/index.sqlite",
    log_dir: Path | str = "/fake/logs",
    workspace_root: Path | str = "/fake/workspaces",
) -> KairixPaths:
    """Construct a KairixPaths for tests without reading env vars.
    Replaces every `monkeypatch.setenv("KAIRIX_*")` + `_resolve_cached.cache_clear()` pattern."""
    return KairixPaths(
        document_root=Path(document_root),
        db_path=Path(db_path),
        log_dir=Path(log_dir),
        workspace_root=Path(workspace_root),
    )
```

---

## Phases

### Phase 0 — Wikilinks pilot (OWNER)

**Why wikilinks first:** zero BDD/integration coverage today. Means the pilot has to (a) write the back-pressure for a domain that lacks it (so we discover what "sufficient back-pressure" looks like in the worst case), and (b) prove the pattern end-to-end. Once wikilinks lands, every domain with stronger coverage is strictly safer to delegate.

Scope:
- `tests/fakes.py` — add `FakePaths` factory
- `kairix/knowledge/wikilinks/{injector,resolver,audit,cli}.py` — accept `paths: KairixPaths | None = None`
- `tests/wikilinks/{conftest,test_injector,test_resolver}.py` — strip `monkeypatch.setenv` + `_resolve_cached.cache_clear()`; construct `FakePaths` and inject
- `tests/bdd/features/wikilinks_injection.feature` + steps + plugin registration — new BDD covering path eligibility, first-mention rule, code-block + frontmatter skip
- CI grep gate: warn-only step that counts `monkeypatch.setenv("KAIRIX_*")` occurrences and prints a deprecation summary

One PR into `develop`. Acceptance: zero `monkeypatch.setenv("KAIRIX_` in `tests/wikilinks/`; full safe-commit green; new BDD scenarios pass.

### Phase 1 — Production paths surface (OWNER)

After Phase 0:
- `kairix/paths.py` — finalise the API. `KairixPaths.from_env()` factory (was `.resolve()` — kept as alias). `KairixPaths.from_dict(d: dict)` for config-driven construction. Document deprecation of module-level functions.
- `tests/fakes.py` — finalise `FakePaths` shape based on what Phase 0 needed.
- `docs/architecture/ENGINEERING.md` — document the boundary pattern alongside the existing CollectionResolver / AgentRegistry sections.

### Phase 2 — Entry-point construction (OWNER)

Touches the boundary:
- `kairix/core/factory.py` — `KairixPaths.resolve()` constructed once, passed into pipeline constructors
- `kairix/cli.py` — same at CLI entry
- `kairix/mcp/server.py` — same at MCP startup
- `kairix/core/embed/cli.py` — same in worker entry

Owner-only because if the boundary construction is wrong, every downstream agent's PR is wrong.

### Phase 3 — Delegated surface refactors (RALPH AGENTS)

After Phase 2 lands, parallel agents — one per domain. Each gets the **agent prompt template** below.

Agents:
1. **agent-search** — `search/hybrid.py`, `search/budget.py` + `tests/search/`, `tests/core/search/` (excluding configurable_default_scope which is already clean)
2. **agent-embed** — `embed/{embed,cli,deps}.py` + `tests/embed/`, `tests/db/`
3. **agent-temporal** — `temporal/index.py` + `tests/mcp/test_timeline_*.py`, `tests/integration/test_timeline_retrieval.py`
4. **agent-mcp** — MCP wiring + `tests/mcp/`, `tests/agents/mcp/`
5. **agent-eval** — eval CLI + `tests/eval/`
6. **agent-briefing** — `briefing/{sources,pipeline}.py` + `tests/integration/test_briefing_pipeline.py` (also folds in the `KAIRIX_AGENT_MEMORY_ROOT` direct-read in `agent_memory_path()`)
7. **agent-reflib** — reflib loader/normalise + `tests/reflib/`, `tests/knowledge/`
8. **agent-curator-onboard** — `onboard/check.py` + `tests/onboard/`, `tests/curator/`

Each PR file-scoped, gated by BDD + integration on its domain.

### Phase 3b — Owner-only surfaces (OWNER)

Three small PRs in series. These domains lack BDD/integration coverage; before refactoring I write the back-pressure as a separate prep PR:

1. **summaries** — write a smoke test against a real summaries DB (small `subprocess.run` invocation of `kairix summaries staleness`); refactor; verify
2. **classify router** — add a router-level BDD scenario or contract test before touching it
3. **setup wizard** — interactive flow; add a CLI smoke test (`subprocess.run`) covering the happy path before refactoring; manual verification on dogfood VM after merge

### Phase 4 — Removal (OWNER)

After every Phase-3 / Phase-3b agent has merged:
- Make `paths: KairixPaths` non-optional everywhere (no more `paths or KairixPaths.resolve()` fallbacks in business code)
- Delete `_resolve_cached`, `clear_cache()`
- Decide: keep module-level `document_root()` etc. as `KairixPaths.resolve().X` shims for ad-hoc scripts (with `DeprecationWarning`), or delete entirely
- CI grep gate flips from **warn → fail** on `monkeypatch.setenv("KAIRIX_`

### Phase 5 — Follow-ups (separate roadmap entries)

- Credentials DI (same shape, scopes `KAIRIX_AZURE_API_KEY`, `KAIRIX_LLM_API_KEY`)
- Embed-backend DI (eliminates `KAIRIX_EMBED_BACKEND=fake` autouse fixture)
- `@patch` / `monkeypatch.setattr` cleanup — separate effort; replace with fakes from `tests/fakes.py`

---

## Agent prompt template (Phase 3)

```
You are refactoring <DOMAIN> to remove env-var monkeypatching.

In scope (edit only these files):
  Production: <list>
  Tests:      <list>

Reference PR (the pilot pattern): <wikilinks PR URL>

What to do:
  1. Each production function/class that calls document_root() / db_path() / log_dir()
     / workspace_root() takes a `paths: KairixPaths | None = None` argument
     (function form) or accepts paths via constructor (class form).
     Internal: paths = paths or KairixPaths.resolve(). Use paths.X everywhere
     in the body. Use KairixPaths.resolve() ONLY at construction boundaries
     (factory.py, CLI entry) — never inside business logic.

  2. Each test that has monkeypatch.setenv("KAIRIX_*") + _resolve_cached.cache_clear():
     replace with FakePaths(document_root=tmp_path, db_path=...) construction
     and inject through the production code's new parameter. Import FakePaths
     from tests.fakes (existing pattern).

  3. Do NOT remove the module-level function fallback yet (Phase 4 does that).
  4. Do NOT touch files outside the lists above.

Back-pressure (run until green, loop on failure):
  pytest <DOMAIN test command — BDD + integration + unit for this domain>
  bash scripts/safe-commit.sh "refactor(paths-di): <DOMAIN> takes KairixPaths via constructor"

Acceptance:
  - grep -r 'monkeypatch.setenv("KAIRIX_' <domain test files> returns no results
  - All tests in scope pass via safe-commit
  - No production callsite outside the listed files is touched

Open one PR per agent into develop. Reference umbrella issue: <#>.
```

---

## CI grep gate

A new CI step (likely in Stage 1 or pre-commit) runs:

```bash
COUNT=$(grep -rln 'monkeypatch.setenv("KAIRIX_' tests/ --include='*.py' | wc -l)
echo "::warning::paths-di refactor: $COUNT files still use monkeypatch.setenv(KAIRIX_*)"
# Phase 4 flips to: exit 1 if [ "$COUNT" -gt 0 ]
```

Gives every PR a visible deprecation count + filename list while migration is in flight; flips to fail-blocking once the count is expected to be zero.

---

## Tracking

- One umbrella issue on GitHub linking every phase PR
- Roadmap entry on `docs/project/ROADMAP.md` Near-term, above the configurable-default-scope entry
- Each agent PR references the umbrella issue
