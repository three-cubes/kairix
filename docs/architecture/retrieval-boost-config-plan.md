---
type: plan
title: Retrieval boost config refactor plan
status: active
date: 2026-05-18
related:
  - retrieval-boost-configuration
---

# Retrieval Boost Configuration — Implementation Plan

## Problem Summary

All three retrieval boosts (entity, procedural, temporal) make hardcoded assumptions about corpus structure:

- **Entity boost** assumes Neo4j is populated and entity notes are authoritative
- **Procedural boost** assumes a specific naming convention (`how-to-*`, `/runbooks/`)
- **Temporal date-path boost** assumes date-named files are the temporal query target

These assumptions hold for some corpora and not others. Any new corpus type risks a regression. The fix is a `RetrievalConfig` dataclass that makes these assumptions explicit and overridable.

---

## Regression Lessons (TMP-7, Sprint 8)

| Lesson | Root cause | Principle |
|---|---|---|
| Date-path boost hurt temporal NDCG (0.668→0.597) | On a consulting-style corpus, temporal queries target concept notes, not YYYY-MM-DD.md logs | Corpus structure ≠ query intent |
| Boost was tested in unit tests but regressed in benchmark | Unit tests verified the boost logic; benchmark revealed the data distribution assumption was wrong | Integration testing must include disabled-boost comparison |
| Regression discovered only after deploy+benchmark | No way to disable boost in config without code change | Every boost needs an `enabled` flag testable in isolation |
| Boost factors are module-level constants | Changing them requires a code change + deploy cycle | All tunable parameters must be in config |

---

## Current State

### rrf.py — hardcoded constants and patterns

```python
# Module-level constants (lines 60–63)
ENTITY_BOOST_FACTOR: float = 0.20
ENTITY_BOOST_CAP: float = 2.0
PROCEDURAL_BOOST_FACTOR: float = 1.4
TEMPORAL_DATE_BOOST_FACTOR: float = 1.35

# Hardcoded path patterns (lines 85–90)
_PROCEDURAL_PATH_PATTERNS = [
    re.compile(r"(?:^|/)how-to-", re.IGNORECASE),
    re.compile(r"/runbooks?/", re.IGNORECASE),
    re.compile(r"(?:^|/)runbook-", re.IGNORECASE),
    re.compile(r"(?:^|/)procedure", re.IGNORECASE),
]
```

### hybrid.py — hardcoded intent gates

```python
# Entity boost: always applied (line 378)
fused = entity_boost_neo4j(fused, neo4j_client)

# Procedural boost: gated on PROCEDURAL intent (line 385)
if intent == QueryIntent.PROCEDURAL:
    fused = procedural_boost(fused)

# Temporal boost: disabled by code comment (line 392)
# if intent == QueryIntent.TEMPORAL:
#     fused = temporal_date_boost(fused, active_query)

# Collections: hardcoded list (line 62)
_SHARED_COLLECTIONS = ["vault-projects", "vault-areas", ...]
```

---

## Target State — Three Layers

### Layer 1 — `RetrievalConfig` in code (Sprint 9, ~2h)

Replace module-level constants with a `RetrievalConfig` dataclass. Instantiated with defaults. Passed into `hybrid_search()`. All boost functions accept config params instead of constants.

No YAML loading yet — just Python dataclasses with defaults. This is the testable interface.

### Layer 2 — `kairix.config.yaml` loading (Sprint 10, ~2h)

Load `RetrievalConfig` from `kairix.config.yaml` at startup. Fall back to defaults if file absent (zero breaking change for existing deploys).

Deployment-specific config (corpus-specific path patterns, boost tuning) lives in the deployer's own `kairix.config.yaml` and is never committed to the public engine repo.

### Layer 3 — Per-collection boost profiles (v1.0, ~4h)

`FusedResult` gains `source_collection: str`. Boost profiles are keyed by collection name. Allows a runbook-heavy collection to get procedural boost while a reference-library collection does not.

---

## Layer 1 — Detailed Spec

### New file: `kairix/search/config.py`

```python
"""
RetrievalConfig — corpus-adaptive search configuration.

All boost behaviour is controlled through this dataclass. Pass an instance
to hybrid_search() to override defaults. Use RetrievalConfig.defaults() for
the baseline tuned configuration.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EntityBoostConfig:
    """Configuration for Neo4j entity in-degree boosting."""
    enabled: bool = True
    factor: float = 0.20          # log-scale weight applied to MENTIONS in-degree
    cap: float = 2.0              # maximum boosted_score / rrf_score ratio
    # When False, entity boost silently no-ops (safe for non-Neo4j deployments)


@dataclass(frozen=True)
class ProceduralBoostConfig:
    """Configuration for procedural content boosting."""
    enabled: bool = True
    factor: float = 1.4           # score multiplier for matching paths
    path_patterns: tuple[str, ...] = (
        r"(?:^|/)how-to-",
        r"/runbooks?/",
        r"(?:^|/)runbook-",
        r"(?:^|/)procedure",
        r"(?:^|/)sop-",           # standard operating procedure convention
        r"(?:^|/)guide-",         # common alternative naming
        r"(?:^|/)playbook-",      # common alternative naming
    )
    # path_patterns: tuple of regex strings matched against result.path
    # Extend with deployment-specific naming conventions

    def compiled_patterns(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.path_patterns]


@dataclass(frozen=True)
class TemporalBoostConfig:
    """Configuration for temporal scoring boosts."""
    # date_path_boost: boosts docs whose path contains a date matching the query
    # Useful only for date-named file corpora (daily journals, meeting logs).
    # DISABLED by default — regressive for concept-note corpora (see retrieval-boost-configuration).
    date_path_boost_enabled: bool = False
    date_path_boost_factor: float = 1.35
    date_path_recency_window_days: int = 90   # for relative terms ("recent", "last month")

    # chunk_date_filter: filter/boost by chunk_date metadata column (TMP-7B)
    # Correct approach for semantic temporal scoring. Requires chunk_date populated.
    chunk_date_boost_enabled: bool = False
    chunk_date_decay_halflife_days: int = 30


@dataclass(frozen=True)
class RetrievalConfig:
    """
    Controls all corpus-adaptive behaviour in hybrid_search().

    Instantiate with defaults for a consulting-style knowledge base.
    Override fields for different corpus types. See docs/architecture/retrieval-boost-config-plan.md.

    Usage:
        # Default (consulting-tuned)
        config = RetrievalConfig.defaults()
        results = hybrid_search(query, agent=agent, config=config)

        # Date-named file corpus (daily journals)
        config = RetrievalConfig.for_daily_log_corpus()

        # Minimal / unknown corpus
        config = RetrievalConfig.minimal()
    """
    entity: EntityBoostConfig = field(default_factory=EntityBoostConfig)
    procedural: ProceduralBoostConfig = field(default_factory=ProceduralBoostConfig)
    temporal: TemporalBoostConfig = field(default_factory=TemporalBoostConfig)

    @classmethod
    def defaults(cls) -> RetrievalConfig:
        """Consulting-style knowledge base defaults."""
        return cls()

    @classmethod
    def minimal(cls) -> RetrievalConfig:
        """All boosts disabled. Baseline RRF only. Use for benchmarking boost impact."""
        return cls(
            entity=EntityBoostConfig(enabled=False),
            procedural=ProceduralBoostConfig(enabled=False),
            temporal=TemporalBoostConfig(
                date_path_boost_enabled=False,
                chunk_date_boost_enabled=False,
            ),
        )

    @classmethod
    def for_daily_log_corpus(cls) -> RetrievalConfig:
        """Corpus where temporal queries target YYYY-MM-DD.md dated files."""
        return cls(
            temporal=TemporalBoostConfig(
                date_path_boost_enabled=True,
                date_path_boost_factor=1.35,
            ),
        )

    @classmethod
    def for_technical_documentation(cls) -> RetrievalConfig:
        """Technical docs corpus: procedural boost with extended patterns, no entity boost."""
        return cls(
            entity=EntityBoostConfig(enabled=False),
            procedural=ProceduralBoostConfig(
                enabled=True,
                factor=1.5,
                path_patterns=(
                    r"(?:^|/)how-to-",
                    r"/runbooks?/",
                    r"(?:^|/)runbook-",
                    r"(?:^|/)procedure",
                    r"(?:^|/)sop-",
                    r"(?:^|/)guide-",
                    r"(?:^|/)playbook-",
                    r"(?:^|/)tutorial-",
                    r"/docs?/",
                    r"/reference/",
                ),
            ),
        )
```

### Changes to `rrf.py`

**Remove:** module-level constants `ENTITY_BOOST_FACTOR`, `ENTITY_BOOST_CAP`, `PROCEDURAL_BOOST_FACTOR`, `TEMPORAL_DATE_BOOST_FACTOR`, `_PROCEDURAL_PATH_PATTERNS` (private module-level patterns).

**Update signatures:**

```python
# Before:
def entity_boost_neo4j(
    results: list[FusedResult],
    neo4j_client: Neo4jClient | None,
    boost_factor: float = ENTITY_BOOST_FACTOR,
    cap: float = ENTITY_BOOST_CAP,
) -> list[FusedResult]:

# After:
def entity_boost_neo4j(
    results: list[FusedResult],
    neo4j_client: Neo4jClient | None,
    config: EntityBoostConfig | None = None,
) -> list[FusedResult]:
    cfg = config or EntityBoostConfig()
    if not cfg.enabled:
        for r in results:
            r.boosted_score = r.rrf_score
        return results
    # ... rest of implementation uses cfg.factor, cfg.cap


# Before:
def procedural_boost(
    results: list[FusedResult],
    boost_factor: float = PROCEDURAL_BOOST_FACTOR,
) -> list[FusedResult]:

# After:
def procedural_boost(
    results: list[FusedResult],
    config: ProceduralBoostConfig | None = None,
) -> list[FusedResult]:
    cfg = config or ProceduralBoostConfig()
    if not cfg.enabled:
        return results
    # ... uses cfg.factor, cfg.compiled_patterns()


# Before:
def temporal_date_boost(
    results: list[FusedResult],
    query: str,
    boost_factor: float = TEMPORAL_DATE_BOOST_FACTOR,
) -> list[FusedResult]:

# After:
def temporal_date_boost(
    results: list[FusedResult],
    query: str,
    config: TemporalBoostConfig | None = None,
) -> list[FusedResult]:
    cfg = config or TemporalBoostConfig()
    if not cfg.date_path_boost_enabled:
        return results
    # ... uses cfg.date_path_boost_factor, cfg.date_path_recency_window_days
```

### Changes to `hybrid.py`

**Add parameter:**

```python
def hybrid_search(
    query: str,
    *,
    agent: str = "shape",
    config: RetrievalConfig | None = None,   # NEW
    # ... existing params
) -> HybridResult:
    cfg = config or RetrievalConfig.defaults()
```

**Update boost call sites:**

```python
# Entity boost — now checks cfg.entity.enabled
try:
    fused = entity_boost_neo4j(fused, neo4j_client, config=cfg.entity)
except Exception as _eb_e:
    ...

# Procedural boost — enabled flag now in config, not just intent gate
if intent == QueryIntent.PROCEDURAL:
    fused = procedural_boost(fused, config=cfg.procedural)

# Temporal date-path boost — now re-enabled but defaults to disabled via config
if intent == QueryIntent.TEMPORAL:
    fused = temporal_date_boost(fused, active_query, config=cfg.temporal)
    # Note: temporal_date_boost is a no-op when cfg.temporal.date_path_boost_enabled=False
    # This replaces the code comment hack currently used to disable it
```

**Key insight:** Removing the `# if intent == QueryIntent.TEMPORAL:` comment hack. The function itself respects the config flag. The call site is clean.

---

## Layer 2 — YAML Loading Spec

### `kairix.config.yaml` schema (public example)

```yaml
# kairix.config.yaml — retrieval configuration
# See: https://github.com/three-cubes/kairix/blob/develop/docs/architecture/retrieval-boost-configuration.md

retrieval:
  boosts:
    entity:
      enabled: true
      factor: 0.20
      cap: 2.0

    procedural:
      enabled: true
      factor: 1.4
      path_patterns:
        - "(?:^|/)how-to-"
        - "/runbooks?/"
        - "(?:^|/)runbook-"
        - "(?:^|/)procedure"
        - "(?:^|/)sop-"
        - "(?:^|/)guide-"
        - "(?:^|/)playbook-"
      # Add your corpus-specific patterns here:
      # - "(?:^|/)my-custom-pattern"

    temporal:
      date_path_boost:
        enabled: false      # Enable only for date-named file corpora
        factor: 1.35
        recency_window_days: 90
      chunk_date_boost:
        enabled: false      # Enable when chunk_date metadata is populated (TMP-7B)
        decay_halflife_days: 30
```

### Deployment-specific config (illustrative)

Deployers typically commit a `kairix.config.yaml` to their own private deployment repo (NOT to the public kairix engine repo). The shape below shows a consulting-style deployment that narrows the procedural patterns to its naming convention:

```yaml
retrieval:
  boosts:
    entity:
      enabled: true
      factor: 0.20
      cap: 2.0
    procedural:
      enabled: true
      factor: 1.4
      # Deployment-specific path patterns matching this corpus's conventions
      path_patterns:
        - "(?:^|/)how-to-"
        - "/[Rr]unbooks?/"
        - "(?:^|/)runbook-"
        - "(?:^|/)procedure"
    temporal:
      date_path_boost:
        enabled: false   # Temporal queries target concept notes, not daily logs
      chunk_date_boost:
        enabled: false   # Enable once TMP-7B is implemented
```

### New file: `kairix/search/config_loader.py`

```python
"""
Load RetrievalConfig from kairix.config.yaml.

Resolution order:
  1. Explicit path: KAIRIX_CONFIG_PATH env var
  2. Working directory: ./kairix.config.yaml
  3. Default: RetrievalConfig.defaults() (no file required)
"""
import os
from pathlib import Path
from kairix.search.config import RetrievalConfig

_SENTINEL = object()
_cached: RetrievalConfig | None = None


def load_retrieval_config(path: Path | None = None) -> RetrievalConfig:
    """Load RetrievalConfig from YAML. Returns defaults if no file found."""
    global _cached
    if _cached is not None:
        return _cached

    config_path = (
        path
        or (Path(os.environ["KAIRIX_CONFIG_PATH"]) if "KAIRIX_CONFIG_PATH" in os.environ else None)
        or Path("kairix.config.yaml")
    )

    if not config_path.exists():
        _cached = RetrievalConfig.defaults()
        return _cached

    try:
        import yaml
        raw = yaml.safe_load(config_path.read_text())
        _cached = _parse_config(raw)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to load %s — using defaults: %s", config_path, e
        )
        _cached = RetrievalConfig.defaults()

    return _cached
```

---

## Layer 3 — Per-Collection Boost Profiles (v1.0 Spec)

**Requires:** `FusedResult.source_collection: str` field (schema change)

### Design

```yaml
# kairix.config.yaml
retrieval:
  collection_profiles:
    runbook-collection:
      boost_profile: runbook_heavy
    project-notes:
      boost_profile: entity_and_concept
    area-notes:
      boost_profile: entity_and_concept

  boost_profiles:
    runbook_heavy:
      procedural: {enabled: true, factor: 1.6}
      entity: {enabled: false}
    entity_and_concept:
      entity: {enabled: true, factor: 0.25}
      procedural: {enabled: false}
    default:
      entity: {enabled: true, factor: 0.20}
      procedural: {enabled: true, factor: 1.4}
```

Each `FusedResult` carries its source collection. The RRF fusion loop assigns the profile before boosting. Boost functions receive the appropriate `BoostConfig` per result, not per batch.

**Note:** This requires moving from batch boost (all results same config) to per-result boost. Entity boost already iterates per-result. Procedural and temporal need minor refactor.

---

## Files to Create / Modify

| File | Change | Layer |
|---|---|---|
| `kairix/search/config.py` | **CREATE** — `RetrievalConfig`, `EntityBoostConfig`, `ProceduralBoostConfig`, `TemporalBoostConfig` | 1 |
| `kairix/search/rrf.py` | Update boost function signatures to accept `*Config` dataclasses; remove module-level constants | 1 |
| `kairix/search/hybrid.py` | Add `config: RetrievalConfig \| None` param to `hybrid_search()`; re-enable `temporal_date_boost` call (disabled via config, not comment) | 1 |
| `tests/search/test_retrieval_config.py` | **CREATE** — unit tests for each `RetrievalConfig` variant, boost enabled/disabled, factor overrides | 1 |
| `docs/architecture/retrieval-boost-config-plan.md` | **CREATE** — this document | 1 |
| `kairix/search/config_loader.py` | **CREATE** — YAML load + cache | 2 |
| `kairix.example.config.yaml` | **CREATE** — public example config | 2 |
| `kairix/search/rrf.py` | Add `source_collection` to `FusedResult` | 3 |
| `kairix/search/hybrid.py` | Pass collection metadata through RRF to FusedResult | 3 |

---

## Acceptance Criteria (Layer 1)

- [ ] `RetrievalConfig.minimal()` produces identical results to running with all boosts disabled
- [ ] Benchmark with `RetrievalConfig.minimal()` gives baseline RRF score (no boost regression)
- [ ] Benchmark with `RetrievalConfig.defaults()` matches or exceeds the previous benchmark baseline (0.721 weighted)
- [ ] `temporal_date_boost` re-enabled via config flag, no longer disabled by code comment
- [ ] All existing 979 tests pass
- [ ] New tests: `test_retrieval_config.py` covers all three boost `enabled=False` paths
- [ ] `hybrid_search()` `config` param is optional (no breaking change to existing callers)
- [ ] Procedural path patterns configurable — can extend with `sop-`, `guide-`, `playbook-`

## Acceptance Criteria (Layer 2)

- [ ] `KAIRIX_CONFIG_PATH` env var loads config from arbitrary path
- [ ] Missing config file silently falls back to defaults (logged at DEBUG)
- [ ] Invalid YAML silently falls back to defaults (logged at WARNING)
- [ ] `kairix.example.config.yaml` committed to public repo

---

## Benchmark Testing Protocol for Boosts

After this refactor, every boost change should follow this protocol:

1. Run benchmark with `RetrievalConfig.minimal()` — record as "RRF baseline"
2. Enable one boost at a time — record incremental NDCG delta
3. Compare against previous sprint's baseline in the same category
4. If delta is negative in any category: **do not enable by default**

This replaces the current approach of enabling boosts by default and discovering regressions post-deploy.

```bash
# Example: test entity boost in isolation
kairix benchmark run --suite suites/v2-real-world.yaml \
  --config-override '{"retrieval": {"boosts": {"entity": {"enabled": false}}}}'
```

(Requires Layer 2 config loading to support inline overrides.)

---

## Sprint 9 Scope

**Layer 1 only** — Python dataclasses, no YAML loading.

Estimated effort: ~2h agentic (one agent, one worktree).

Scope boundary:
- OUT: YAML loading (Layer 2) — defer to Sprint 10
- OUT: Per-collection profiles (Layer 3) — defer to v1.0
- OUT: `chunk_date_boost` implementation (TMP-7B) — separate card, same sprint

The Layer 1 refactor is the prerequisite for both TMP-7B (needs `TemporalBoostConfig.chunk_date_boost_enabled`) and MHQ-1 (needs `RetrievalConfig.minimal()` for baseline isolation).
