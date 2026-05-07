# Paths dependency-injection refactor

**Goal:** Eliminate hidden-global-state path resolution from kairix. Production code should take `KairixPaths` as an explicit dependency at the boundary; tests should construct `FakePaths` directly rather than mutating env vars.

## Problem

Production reads paths via module-level functions — `document_root()`, `db_path()`, `workspace_root()`, `agent_memory_path()` — backed by `_resolve_cached()`, an `lru_cache`-d env-var reader. Three concrete smells:

1. **Hidden global state.** Every callsite is implicitly coupled to process-wide state. Two pipelines in the same process cannot operate against different paths.
2. **Test-shaped API in production.** Tests have no DI seam, so the only way to override paths is `monkeypatch.setenv("KAIRIX_*")` + `_resolve_cached.cache_clear()`. ~30 test files do this. The kairix test convention (per `feedback_no_monkeypatch` rule) is to inject fakes from `tests/fakes.py` — env-var monkeypatching is a workaround for a missing seam, not a deliberate design.
3. **Pattern inconsistency.** `DefaultCollectionResolver` and `ConfigDrivenAgentRegistry` already follow the boundary pattern (G4: config at boundary). Paths are the lone holdout.

## The seam

`KairixPaths` (already a frozen dataclass) becomes the only paths surface.

```python
# Production callsite (function form)
def inject_wikilinks(content: str, entities: list[WikiEntity], *, paths: KairixPaths | None = None) -> tuple[str, list[str]]:
    paths = paths or KairixPaths.resolve()
    # ... use paths.document_root, paths.workspace_root, etc.

# Production callsite (class form — preferred when path config is durable)
class WikiLinksAuditor:
    def __init__(self, *, paths: KairixPaths) -> None:
        self._paths = paths

# Entry point (factory / CLI / MCP startup) — the *only* place .resolve() is called
def build_pipeline() -> SearchPipeline:
    paths = KairixPaths.resolve()
    return SearchPipeline(..., paths=paths)

# Test
from tests.fakes import FakePaths

def test_eligibility(tmp_path: Path) -> None:
    paths = FakePaths(document_root=tmp_path / "vault", workspace_root=tmp_path / "ws")
    result = inject_wikilinks("...", [...], paths=paths)
```

`FakePaths` is a constructor helper, not a separate type — returns a real `KairixPaths` from explicit arguments. The production type surface stays narrow.

Module-level `document_root()` / `db_path()` etc. remain as deprecated shims for one release window so each domain can refactor independently. Once every domain has migrated they're removed.

## Production callsites of `kairix.paths`

```
kairix/agents/briefing/{pipeline,sources}.py
kairix/core/classify/router.py
kairix/core/embed/{cli,deps,embed}.py
kairix/core/search/{budget,hybrid}.py
kairix/core/temporal/index.py
kairix/knowledge/summaries/{cli,staleness}.py
kairix/knowledge/wikilinks/{audit,cli,injector}.py
kairix/platform/onboard/check.py
kairix/platform/setup/wizard.py
```

16 files — each takes `paths: KairixPaths` either via constructor (class form, preferred) or as a keyword argument (function form).

## Code-smell mitigations

| Smell | How the seam addresses it |
|---|---|
| Hidden global state | `paths` is an explicit constructor arg or function parameter. No module-level reads in business logic. |
| Test-shaped API | `FakePaths` matches the production type. Tests don't need to know about `_resolve_cached` or env vars. |
| Pattern inconsistency | Mirrors `DefaultCollectionResolver(collections_config=...)` and `ConfigDrivenAgentRegistry(agents=...)` — same boundary pattern, same constructor-injection shape. |
| `_resolve_cached` lru_cache | Removed once the deprecated module-level functions are removed; production no longer needs caching because each entry point resolves once and passes the result. |
| `KAIRIX_AGENT_MEMORY_ROOT` direct read | `agent_memory_path()` reads this env var outside `_resolve_cached`. The briefing-domain refactor folds it into the `paths` parameter. |

## Out of scope

- **Credentials DI** (`KAIRIX_AZURE_API_KEY`, `KAIRIX_LLM_API_KEY`) — same anti-pattern, separate follow-up.
- **Embed-backend DI** (`KAIRIX_EMBED_BACKEND=fake` autouse fixture) — same anti-pattern, separate follow-up.
- **`@patch` / `monkeypatch.setattr` cleanup** — different smell (substituting imports / functions vs env vars). Tracked separately.

## CI

A grep gate counts `monkeypatch.setenv("KAIRIX_*")` occurrences in the test tree and prints a deprecation summary. Initially warn-only; flips to fail-blocking once the count is expected to be zero.

```bash
COUNT=$(grep -rln 'monkeypatch.setenv("KAIRIX_' tests/ --include='*.py' | wc -l)
echo "::warning::paths-di refactor: $COUNT files still use monkeypatch.setenv(KAIRIX_*)"
```

## Tracking

Umbrella issue: [#139](https://github.com/quanyeomans/kairix/issues/139). Each surface refactor is its own PR linked from there.
