# Configurable default search scope

**Goal:** operators control which collections participate in default search scopes from `kairix.config.yaml`, without source changes. Replaces a hardcoded `_RESERVED_COLLECTIONS = {"reference-library"}` carve-out that was specific to one operator's vault layout.

## Problem

Two concerns are conflated in `collections.shared`:

1. **What gets scanned and indexed** — the scanner walks every entry in `collections.shared` and writes rows into `documents`.
2. **What participates in default search scopes** — the resolver returns every entry in `collections.shared` minus a hardcoded exclusion set.

That means a collection cannot be "indexed but not in default scope" without code changes. The hardcoded carve-out exists for `reference-library` because reflib content is an order of magnitude larger than the user's docs and would otherwise dominate default search. The same problem applies to any operator's `archive` collection or other large-but-occasionally-useful corpora.

A second leakage of the same anti-pattern lives in `kairix/core/search/config_loader.py` (`if target == "reference-library":` for retrieval-config defaults).

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

| Scope | Behaviour |
|---|---|
| `SHARED` | every `collections.shared` with `in_default=True` |
| `AGENT` | unchanged |
| `SHARED_AGENT` | default-shared + agent |
| `ALL_AGENTS` | every agent's collections with `in_default=True` |
| `EVERYTHING` | dedup(default-shared + default-all-agents) |
| Explicit `--collection X` | unchanged — works for any indexed collection |

Explicit `--collection archive` keeps working regardless of the flag, because the resolver only filters when constructing scope membership; it never blocks an explicit name.

## Internal design

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
    shared: tuple[CollectionDef, ...]
    agent_pattern: str = "{agent}-memory"
    agent_paths: dict[str, str] = field(default_factory=dict)

    def default_collection_names(self) -> list[str]:
        """Names of collections eligible for default scopes."""
        return [c.name for c in self.shared if c.in_default]

    def all_collection_names(self) -> list[str]:
        """Names of every configured collection — for explicit lookups."""
        return [c.name for c in self.shared]
```

Predicates live on `CollectionsConfig`, not on the resolver. The resolver consumes a typed surface and never reaches into `CollectionDef` internals.

`DefaultCollectionResolver.resolve()` collapses to a `match` dispatch:

```python
def resolve(self, agent: str | None, scope: object) -> list[str] | None:
    scope_enum = scope if isinstance(scope, Scope) else Scope.parse(str(scope))
    match scope_enum:
        case Scope.SHARED:        cols = self._defaults()
        case Scope.AGENT:         cols = self._agent_collections(agent)
        case Scope.SHARED_AGENT:  cols = self._defaults() + self._agent_collections(agent)
        case Scope.ALL_AGENTS:    cols = self._all_agent_defaults()
        case Scope.EVERYTHING:    cols = _dedupe(self._defaults() + self._all_agent_defaults())
    return cols or None
```

Cyclomatic complexity per method ≤ 3. The 22-line block-comment defending the previous hardcoded constant is gone, along with four scattered `if c.name not in self._RESERVED_COLLECTIONS` filters — all consolidated into the predicate on `CollectionsConfig`.

## Code-smell mitigations

| Smell | How the design addresses it |
|---|---|
| Cyclomatic complexity | `match` dispatch reduces `resolve()` to CC=1 + helpers each CC≤3 |
| Feature envy | Predicates moved to `CollectionsConfig` and `AgentDef`; resolver consumes lists, never iterates internals |
| Inappropriate intimacy | Resolver no longer knows `"reference-library"` is special; that knowledge is in operator yaml |
| Encapsulation | `CollectionsConfig` is frozen (`tuple`, not `list`); the only public surface is the two `*_collection_names()` predicates |
| Obfuscation in messaging | `NotImplementedError` for `ALL_AGENTS` / `EVERYTHING` without a registry now names the `agents:` yaml block as the action to take |
| Test-shaped APIs | No new test-shaped surface added; tests inject `FakeCollectionResolver` from `tests/fakes.py` |
| Basic security | `_coerce_bool()` rejects non-boolean values with a `ConfigValidationError` naming the offending key — guards against `bool("false") is True` silently flipping the scope |
| Missing BDD | New `tests/bdd/features/configurable_default_scope.feature` covers the operator-visible behaviour |

## Migration

Zero work for operators on upgrade — the absent flag preserves today's behaviour. Operators flip the flag when they want opt-in semantics. The yaml field is additive; older code reading a yaml that has `in_default` silently ignores the unknown field.

## Out of scope

- **Composable named scopes** — `scopes.research`, `scopes.with-history`. Tracked separately; do not implement until a second concrete use case lands.
- **Per-agent default-scope overrides** — a different agent might want `archive` in default scope; defer until requested.
