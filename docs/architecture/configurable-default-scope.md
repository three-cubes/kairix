# Planning: Configurable default search scope

**Status:** Phase 1 + Phase 3 shipped (2026-05-07); Phase 2 design-tracked, uncommitted.
**Target version:** v2026.5.4 (Phase 1 + Phase 3 bundled in this PR)
**Owner:** maintainers
**Primary motivation:** `DefaultCollectionResolver` currently bakes one operator's vault layout into a public-project source tree (`_RESERVED_COLLECTIONS = {"reference-library"}`). Any operator who wants a collection scanned + indexed but **not** auto-included in default search has to fork the resolver. The shape needs to be config-driven and policy-free.

---

## Problem statement

Today, two concerns are conflated in `collections.shared`:

1. **What gets scanned and indexed** — scanner walks every entry in `collections.shared` and writes rows into `documents`.
2. **What participates in default search scopes** — `DefaultCollectionResolver._shared_collections()` returns every entry in `collections.shared` minus a hardcoded exclusion set.

This means a collection cannot be "indexed but not in default scope" without code changes. The `_RESERVED_COLLECTIONS` carve-out exists for exactly one collection — `reference-library` — because reflib content is an order of magnitude larger than the user's docs and would dominate default search. The same problem applies to any operator's `archive` collection or other large-but-occasionally-useful corpora; they have no path to express the intent.

A second leakage of the same anti-pattern lives in `kairix/core/search/config_loader.py:384` (`if target == "reference-library":`). It is in scope for follow-up but **out of scope for this Phase 1**.

---

## Goals & non-goals

### Goals

1. Operators control which collections are in default search scopes purely from `kairix.config.yaml`, with no source changes.
2. The `_RESERVED_COLLECTIONS` constant and its policy comments are deleted; the foot-gun comment becomes a yaml comment in `kairix.example.config.yaml`.
3. `DefaultCollectionResolver.resolve()` is refactored down from its current 5-branch elif chain into a single dispatch with helpers whose responsibilities are obvious from name alone. Cyclomatic complexity per method ≤ 3.
4. Backwards compatible: a yaml without the new field behaves exactly as today.
5. The example config ships `reference-library: in_default: false` as a hint to new operators (avoids the reflib foot-gun by default).

### Non-goals (Phase 1)

- **Composable named scopes** — `scopes.research`, `scopes.with-history`, etc. Tracked as Phase 2; do not implement until a second concrete use case lands.
- **Per-collection retrieval-config hardcode in config_loader.py:384** — removed in a separate follow-up; same anti-pattern but distinct surface.
- **Per-agent default-scope overrides** — a different agent might want `archive` in default scope; defer until requested.
- **Migrating operators’ deployed yamls automatically** — operators flip the flag themselves on their next config push.

---

## User-facing change

`CollectionDef` gains an optional `in_default: bool = True` field:

```yaml
collections:
  shared:
    - name: home
      path: 00-Home
      glob: "**/*.md"
      # in_default omitted → True (default behaviour, identical to today)

    - name: archive
      path: 06-Archive
      glob: "**/*.md"
      in_default: false   # indexed; reachable only via explicit --collection archive

    - name: reference-library
      path: reference-library
      glob: "**/*.md"
      in_default: false   # large external corpus, opt-in only
```

Affected scopes:

| Scope | Before | After |
|---|---|---|
| `SHARED` | every `collections.shared` minus reserved set | every `collections.shared` with `in_default=True` |
| `AGENT` | unchanged | unchanged |
| `SHARED_AGENT` | shared + agent | default-shared + agent |
| `ALL_AGENTS` | every agent's collections, minus reserved | every agent's collections with `in_default=True` |
| `EVERYTHING` | dedup(shared + all-agents) minus reserved | dedup(default-shared + default-all-agents) |
| Explicit `--collection X` | unchanged — works for any indexed collection | unchanged |

Explicit `--collection archive` keeps working regardless of the flag, because resolver only filters when constructing scope membership; it never blocks an explicit name.

---

## Internal design

### Data model

```python
@dataclass(frozen=True)
class CollectionDef:
    name: str
    path: str
    glob: str = "**/*.md"
    in_default: bool = True
    retrieval_overrides: dict | None = None


@dataclass(frozen=True)
class CollectionsConfig:
    shared: tuple[CollectionDef, ...]   # tuple, not list — frozen
    agent_pattern: str = "{agent}-memory"
    agent_paths: dict[str, str] = field(default_factory=dict)

    def default_collection_names(self) -> list[str]:
        """Names of collections eligible for default scopes."""
        return [c.name for c in self.shared if c.in_default]

    def all_collection_names(self) -> list[str]:
        """Names of every configured collection — for explicit lookups."""
        return [c.name for c in self.shared]
```

Predicates live on `CollectionsConfig`, not on the resolver. The resolver now consumes a typed surface (`default_collection_names()`, `all_collection_names()`) and never reaches into `CollectionDef` internals.

### Resolver refactor

`DefaultCollectionResolver.resolve()` collapses to a `match` dispatch. Each helper is single-purpose; cyclomatic complexity per method drops to ≤ 3.

```python
def resolve(self, agent: str | None, scope: object) -> list[str] | None:
    scope_enum = scope if isinstance(scope, Scope) else Scope.parse(str(scope))

    match scope_enum:
        case Scope.SHARED:
            cols = self._defaults()
        case Scope.AGENT:
            cols = self._agent_collections(agent)
        case Scope.SHARED_AGENT:
            cols = self._defaults() + self._agent_collections(agent)
        case Scope.ALL_AGENTS:
            cols = self._all_agent_defaults()
        case Scope.EVERYTHING:
            cols = _dedupe(self._defaults() + self._all_agent_defaults())

    return cols or None
```

Notes:
- `_defaults()` returns `self._config.default_collection_names() + self._extras_default_filtered()`. Single source of truth.
- `_agent_collections(agent)` returns `[]` when `agent is None` (existing behaviour: AGENT with no agent → `None` from `resolve()`).
- `_all_agent_defaults()` calls registry once, filters via `agent_def.in_default_collection_names()` (added to AgentDef in tandem), raises a clear error if no registry is wired — same as today, with a message that names the yaml field to set.
- `_dedupe` is a 3-line module-level helper, not a method.

### What dies

- `_RESERVED_COLLECTIONS: frozenset[str] = frozenset({"reference-library"})` — gone.
- The 22-line block-comment defending the constant — gone.
- The four `if c.name not in self._RESERVED_COLLECTIONS` filters scattered through resolver methods — gone (consolidated into the predicate on `CollectionsConfig`).

---

## Code-smell audit (upfront)

| Smell | Where it lives today | Mitigation in Phase 1 |
|---|---|---|
| **Cyclomatic complexity** | `resolve()` 5-elif chain; `_collections_for_agent` reads registry, falls back to pattern, re-filters reserved | `match` dispatch reduces `resolve()` to CC=1 + helpers each CC≤3. Reserve filtering moves out of `_collections_for_agent` entirely (predicate on data class). |
| **Feature envy ("jealousy")** | Resolver iterates `self._config.shared` and reads `.name`; resolver iterates `agent.collection_names()`; resolver knows reserved-set policy | Predicates move to `CollectionsConfig.default_collection_names()` and `AgentDef.in_default_collection_names()`. Resolver consumes lists. |
| **Inappropriate intimacy ("familiarity")** | Resolver knows that `"reference-library"` is special; `config_loader.py:384` knows it twice | Phase 1 deletes the resolver hardcode. Phase 1 does **not** touch the config_loader hardcode (separate follow-up — same anti-pattern, different surface). |
| **Encapsulation** | `CollectionsConfig.shared: list[...]` is mutable and exposed; callers can mutate or read internals freely | `CollectionsConfig` becomes frozen (`tuple` not `list`); the only public surface is the two `*_collection_names()` predicates. |
| **Obfuscation in messaging** | `NotImplementedError("scope=all-agents / everything requires an AgentRegistry. Configure agents: …")` — names the cause but not the action | New message names the yaml block (`agents: …`) and tells the operator the minimum viable example. Same pattern applied to `Scope.parse()` errors and config validation errors for `in_default`. |
| **Test-shaped APIs** | `extra_collections` ctor param exists primarily so tests can inject extras without faking the env-var pathway; no production caller uses it for non-env-var content | Document the ctor param as "operator extras (env-var-resolved at boundary)"; tests inject `FakeCollectionResolver` from `tests/fakes.py` rather than constructing `DefaultCollectionResolver`. No new test-shaped surface added. |
| **Basic security** | yaml truthy-string coercion: `bool("false") is True`; today this isn't a vector because no bool fields existed in `CollectionDef` | Add `_coerce_bool()` to `config_loader.py` that accepts `True`, `False`, and a closed set of canonical strings (`"true"/"false"`). Anything else raises `ConfigValidationError` with a message naming the offending key + value. Apply only to `in_default` to avoid a sweeping refactor. |
| **Missing BDD/E2E** | No BDD scenario covers "operator marks a collection as opt-in only" | Add a BDD scenario (`tests/bdd/features/configurable_default_scope.feature`) covering: (a) collection with `in_default: false` is absent from default search results; (b) explicit `--collection archive` returns archived docs; (c) yaml without the field behaves identically to today. |

---

## Phased delivery

### Phase 1 (this sprint, roadmap-tracked)

Single PR into `develop`, structured as three reviewable commits:

**Commit 1 — refactor with no behaviour change:**
- Add `default_collection_names()` and `all_collection_names()` to `CollectionsConfig` (returning today's behaviour: every shared collection name except hardcoded reflib).
- Add `in_default_collection_names()` to `AgentDef` (today: same as `collection_names()`).
- Replace the elif chain in `resolve()` with `match`. Delete the four scattered `if … not in self._RESERVED_COLLECTIONS` filters; the predicate methods now own that filter.
- All existing tests stay green — behaviour identical.
- This commit also makes `CollectionsConfig.shared` a `tuple`, not `list`. Internal callers don't mutate it; check via grep.

**Commit 2 — introduce the `in_default` flag:**
- Add `in_default: bool = True` to `CollectionDef`.
- `_coerce_bool()` helper in `config_loader.py`; reject non-canonical values with `ConfigValidationError`.
- The hardcoded reflib filter inside `default_collection_names()` is removed; the method now relies on each `CollectionDef.in_default`.
- Default behaviour preserved for yamls that don't set the field (since the constructor default is `True`). Operators who relied on the hardcode see no change until they flip the flag in their yaml.
- Add contract test: `tests/contracts/test_collection_defaults.py` — predicates respect `in_default` for both shared and agent paths.
- Add BDD scenario above.

**Commit 3 — docs and example yaml:**
- `kairix.example.config.yaml` ships `reference-library: in_default: false` with an explanatory comment.
- `docs/architecture/ENGINEERING.md` collection-resolution section updated.
- `docs/architecture/configurable-default-scope.md` (this doc) status changed from Planned to Shipped on merge.
- `CHANGELOG.md` entry under Unreleased.

**Operational rollout (post-merge):**
1. Merge to `develop`; `:develop` image rebuilds.
2. UAT on the dogfood VM: confirm `archive` rows still indexed, no longer in default scope; explicit `kairix search --collection archive "x"` still returns archive docs.
3. Edit `/opt/kairix/app/kairix.config.yaml` on VM: set `archive: in_default: false` and `reference-library: in_default: false`. Restart `app-kairix-1` and `app-kairix-worker-1`.
4. Re-verify: `kairix search "team"` no longer returns `06-Archive/...` paths in default scope.

### Phase 2 (roadmap, not committed to a sprint)

Composable named scopes:

```yaml
scopes:
  default:        # implicit; equals every collection with in_default=true
  with-history:   [home, projects, areas, resources, agent-knowledge, knowledge, archive]
  research:       [reference-library]
```

Plus one new `Scope.NAMED` value (or extend `Scope.parse()` to recognise config-defined names alongside the closed enum). Hold until a second concrete use case appears; building it now is speculative.

### Phase 3 (separate, smaller — same anti-pattern at a different surface)

Lift the `if target == "reference-library":` hardcode in `config_loader.py:384` into per-collection `retrieval:` overrides. Mechanically:
- `CollectionDef.retrieval_overrides` already exists and supports per-collection overrides.
- The `REFLIB_RETRIEVAL_CONFIG` baseline becomes the default `retrieval:` block in the example yaml's `reference-library` entry.
- Hardcode deleted; same backwards-compatibility shape as Phase 1.

---

## Test plan

| Layer | Tests added |
|---|---|
| Unit (config) | `parse_collections` with `in_default: true`, `in_default: false`, missing field, non-bool string, list (rejected), null (rejected) |
| Unit (resolver) | `_defaults()` excludes opt-in collections; `_agent_collections()` excludes opt-in for that agent; `EVERYTHING` dedupes correctly across opt-in boundaries |
| Contract | `tests/contracts/test_collection_defaults.py` — production resolver and `FakeCollectionResolver` agree on the predicates' semantics |
| BDD | `configurable_default_scope.feature` — three scenarios above |
| Regression | `tests/contracts/test_resolver_no_reflib_hardcode.py` — asserts the string `"reference-library"` does not appear in `kairix/core/search/resolver.py` (encoded protection so the hardcode can't silently come back) |

Coverage gate: existing thresholds, no relaxation. No mocks/monkeypatch — fakes from `tests/fakes.py`.

---

## Migration & rollback

**Migration:** zero work for operators on upgrade — the absent flag preserves today's behaviour. Operators choose to flip the flag when they want opt-in semantics.

**Rollback:** revert the PR. No DB migration, no data shape change. The yaml field is additive; older code reading a yaml that has `in_default` will silently ignore the unknown field (already the case for unknown fields in the parser).

---

## Open questions for review

1. **Frozen vs. mutable `CollectionsConfig`.** Making it frozen tightens encapsulation but breaks any caller that mutates `.shared`. I haven't found such a caller; want to confirm before committing to `tuple`.
2. **Should `KAIRIX_EXTRA_COLLECTIONS` extras default to `in_default=true`?** The env var ships a list of collection *names*, no flag. Today they're treated like shared. I'd default them to `in_default=true` (no change) and defer richer extras semantics to Phase 2. Confirm.
3. **Phase 3 placement.** I'd prefer Phase 3 follows Phase 1 immediately while the surface is hot, even though it's a separate roadmap line. Confirm whether to bundle scoping or keep them serial.
