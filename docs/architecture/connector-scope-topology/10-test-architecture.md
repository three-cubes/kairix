# Test architecture — what test surface ADR v2 requires

The ADR v2 + extended BDD (~42 features) won't sustain themselves without a test architecture that pins each behaviour at the appropriate level. This doc specifies the test surface: layers, per-layer responsibilities, F-rule changes, fakes inventory, sabotage-proof discipline.

## Layered test surface (kairix convention, extended)

| Layer | Lives under | Tests what | When it runs | Cost budget |
|---|---|---|---|---|
| **Contract** | `tests/contracts/` | Each Protocol's shape (method signatures, return types, F42 frozen-dataclass) + each capability Protocol independently | Pre-commit + CI Stage 2 | <1s |
| **Unit** | `tests/<area>/test_*.py` (mirrors `kairix/` tree) | Per-function correctness with Fakes | Pre-commit + CI Stage 2 | <30s suite |
| **Integration** | `tests/integration/` | Multi-component pipeline via `kairix.core.factory.build_*` with `FakePaths(tmp_path)` | CI Stage 3 | <2min suite |
| **BDD** | `tests/bdd/features/*.feature` + `tests/bdd/steps/*_steps.py` | Outcome-shaped behaviour pinned to use cases (UC-MEM/KNW/CMP/GRP/ACS + actor-perspective from §09) | Pre-commit + CI Stage 2 | <30s suite |
| **E2E composed-path** | `tests/e2e/test_composed_*_path.py` with `@pytest.mark.e2e` | Real config → factory.build → ingest → query → assertion against composed production code | CI Stage 4.5 (`pytest -m e2e`) | <2min per file |
| **Property-based** | `tests/property/test_*.py` (NEW directory) | Composition rules, scope-profile intersection / F39-min, chunker-dispatch determinism | CI Stage 2 | <10s suite |
| **Failure-injection** | `tests/failure_injection/test_*.py` (NEW directory) | Connector raises (401/429/auth/etc.) routed through dead_letter; Resolver.reindex replay; container-revoked surface | CI Stage 3 | <1min suite |
| **Performance-smoke** | `tests/perf/test_*.py` (NEW; gated by `KAIRIX_PERF=1` env) | Per-source backfill envelope (see `05-non-functionals.md`); chunk-write throughput ≥10/s sustained | CI Stage 4.5 (opt-in, slow) | minutes per file |
| **Migration-tests** | `tests/migrations/test_*.py` (NEW directory) | Wave-A → Wave-G migrations are back-compat reversible (old shape + new shape both pass) | CI Stage 3 + per-release tag | <1min suite |

Three new top-level test directories (`property/`, `failure_injection/`, `perf/`, `migrations/`) — each gets its own README per F23 + own pytest marker per F8.

---

## Contract tests — per capability Protocol

For each capability Protocol in ADR v2 §"Connector Protocol — capability mix-ins" (9 Protocols total), a contract test under `tests/contracts/test_<capability>_protocol.py`. Pattern per F43:

```python
# tests/contracts/test_slim_connector_protocol.py

@pytest.mark.contract
def test_slim_connector_contract_canonical_fake() -> None:
    """FakeSlimConnector emits SlimDoc with id + last_modified + minimal metadata."""
    fake = FakeSlimConnector(items=[
        SlimDoc(id="a", last_modified="2026-05-23T12:00:00Z"),
        SlimDoc(id="b", last_modified="2026-05-23T12:01:00Z"),
    ])
    container = Container(cc_pair_id=1, container_id="c1", access_state="ACCESSIBLE", cursor_token=None, last_synced_at=None)
    docs = list(fake.retrieve_all_slim_docs(container, start=..., end=...))
    assert {d.id for d in docs} == {"a", "b"}

@pytest.mark.contract
def test_obsidian_is_not_a_slim_connector() -> None:
    """Obsidian is not a prune authority (PLA-553).

    A local walk cannot prove a slim enumeration complete, so Obsidian does
    not declare SlimConnector; a prune cycle selecting by isinstance skips
    it, and its deletions come only from reconciliation.
    """
    assert not isinstance(ObsidianConnector(vault_root=Path("/tmp/vault")), SlimConnector)
```

This satisfies F43 (every plugin has contract test against canonical fake AND real impl): the real-impl half runs against each connector that declares the capability, and a connector that must not declare it — Obsidian here — gets a negative contract instead.

**Plus a "capability inventory" test** per connector: assert the connector declares the expected capability set:

```python
@pytest.mark.contract
def test_obsidian_capability_inventory() -> None:
    """Obsidian declares: SourceConnector, PollConnector, HierarchyConnector. Not: SlimConnector (not a prune authority, PLA-553), OAuthConnector (no auth), Resolver (no remote failures)."""
    expected = {SourceConnector, PollConnector, HierarchyConnector}
    forbidden = {SlimConnector, OAuthConnector, EventConnector}
    actual = {p for p in CAPABILITY_PROTOCOLS if isinstance(ObsidianConnector(...), p)}
    assert expected.issubset(actual)
    assert not (forbidden & actual)
```

This pins the capability surface per connector. Adding a capability is a deliberate ADR + test change, not a silent drift.

---

## Chunker contract tests — per (kind, mime)

For each chunker plugin in `kairix/extractors/<chunker>/` (or wherever chunkers land per Wave F), a contract test under `tests/contracts/test_<chunker>_chunker.py`. Pattern:

```python
# tests/contracts/test_markdown_structural_chunker.py

@pytest.mark.contract
def test_markdown_structural_chunker_heading_path_preserved() -> None:
    """A markdown doc with H1/H2/H3 produces chunks whose heading_path reflects the hierarchy."""
    chunker = MarkdownStructuralChunker(v="2")
    section = TextSection(text="# A\n## B\n### C\nbody.", link=None, heading_path=())
    chunks = chunker.chunk(section, context=ChunkContext(source_uri="..."))
    assert any(c.heading_path == ("A", "B", "C") for c in chunks)
    assert all(c.chunker_version == "2" for c in chunks)

@pytest.mark.contract
def test_markdown_structural_chunker_target_chunk_size_within_envelope() -> None:
    """Chunks land between 256 and 768 tokens (per 08-chunking §"Markdown / wiki-doc")."""
    chunker = MarkdownStructuralChunker(v="2")
    section = TextSection(text="long markdown..." * 1000, link=None, heading_path=())
    chunks = chunker.chunk(section, context=...)
    sizes = [count_tokens(c.text) for c in chunks]
    assert all(256 <= s <= 768 for s in sizes)
```

**Sabotage-proof per chunker**: each chunker contract test has at least one sabotage scenario — break a heading-path inference, confirm the assertion fails, restore. Per `feedback_sabotage_must_be_executed`.

---

## Property-based tests — composition rules

`tests/property/test_scope_composition.py` (new dir, uses `hypothesis`):

```python
from hypothesis import given, strategies as st

@given(
    profiles=st.lists(scope_profile_strategy(), min_size=2, max_size=5),
)
def test_intersection_is_subset_of_each_profile_collections(profiles):
    """The intersection of N scope profiles is a subset of every profile's collections."""
    intersection = compose_scope_profiles(profiles, mode="intersection")
    for p in profiles:
        assert intersection.collection_names <= p.collection_names

@given(
    profiles=st.lists(scope_profile_strategy(), min_size=2, max_size=5),
)
def test_intersection_max_sensitivity_is_min_across_profiles(profiles):
    """For collections present in all profiles, the composed max_sensitivity is F39-min."""
    intersection = compose_scope_profiles(profiles, mode="intersection")
    for entry in intersection.entries:
        actor_caps = [p.entry_for(entry.collection_name).max_sensitivity for p in profiles if p.has(entry.collection_name)]
        assert entry.max_sensitivity == f39_min(*actor_caps)
```

Similar property tests for:
- Most-specific filter wins in `CollectionRouter`
- HierarchyNode parent-before-child invariant (no orphan emissions)
- `F39_min(a, b, c) == F39_min(F39_min(a, b), c)` (associativity)
- ChunkerRegistry dispatch determinism (same `(kind, mime, section.kind)` always picks same chunker)

---

## Failure-injection tests — per failure mode

`tests/failure_injection/` (new dir). Each failure mode from ADR v2 §"Failure-mode catalogue" gets a test:

```python
# tests/failure_injection/test_credential_expired.py

@pytest.mark.failure_injection
def test_credential_expired_routes_cc_pair_to_INVALID() -> None:
    """A CredentialExpiredError raised by the connector during list_changes routes the cc_pair to INVALID without crashing the worker."""
    fake_connector = FakeSourceConnector(raise_on_list_changes=CredentialExpiredError("token expired"))
    cc_pair = make_cc_pair(connector=fake_connector, ...)
    runner = ConnectorRunner(cc_pair=cc_pair, ...)
    runner.run_one_batch()
    # Assert cc_pair status now INVALID + the error is surfaced
    assert cc_pair.status == CCPairStatus.INVALID
    assert cc_pair.in_repeated_error_state
    # Other cc_pairs in the worker continue normally
    other = make_cc_pair(connector=FakeSourceConnector(items=[...]), ...)
    runner.run_one_batch_for(other)
    assert other.status == CCPairStatus.ACTIVE

# similar tests for:
# - ContainerAccessDenied (one container revoked; others continue)
# - ContainerTransient with retry_after (rate-limit honoured)
# - ChunkerError (per-doc dead_letter; batch continues)
# - InsufficientPermissionsError on perm-sync
# - Subscription revocation mid-soak (push fails; poll catches up)
```

Each failure-injection test names the production behaviour it verifies (the cc_pair status transition, the dead_letter row, the result envelope flag).

---

## Migration tests — per Wave

`tests/migrations/` (new dir). Each ADR v2 wave is back-compat per `feature-flag-architecture.md`; the migration tests verify both branches:

```python
# tests/migrations/test_wave_a_schema_back_compat.py

@pytest.mark.migration
def test_wave_a_with_flag_off_preserves_v1_shape() -> None:
    """topology_schema=off: existing connector_cursors row works exactly as v1."""
    with flag_off("topology_schema"):
        cursor = read_cursor("obsidian")
        assert cursor.cursor_token == "..."

@pytest.mark.migration
def test_wave_a_with_flag_on_populates_new_tables() -> None:
    """topology_schema=on: connector_containers table is populated for single-container connectors with container_id=cc_pair_name."""
    with flag_on("topology_schema"):
        run_one_sync_cycle("obsidian")
        containers = list(read_containers(cc_pair_name="obsidian"))
        assert len(containers) == 1
        assert containers[0].container_id == "obsidian"
        assert containers[0].cursor_token == read_cursor("obsidian").cursor_token

@pytest.mark.migration
def test_wave_a_to_wave_g_progression() -> None:
    """Walking through flags A → B → C → … → G end-state never breaks an existing actor's queries."""
    seed_dogfood_corpus()
    baseline_results = query_canonical_set(actor="agent-shape")
    for wave in ["A", "B", "C", "D", "E", "F", "G"]:
        promote_flag(f"topology_{wave.lower()}")
        results = query_canonical_set(actor="agent-shape")
        assert results.match(baseline_results, tolerance=0.05)  # 5% drift acceptable
```

This catches the worst class of bug: silent regression as flags promote.

---

## E2E composed-path per skill

`tests/e2e/test_composed_<skill>_path.py` per F48. For each skill in the registry, one E2E that walks config → factory.build → ingest → skill-invocation → assertion.

```python
# tests/e2e/test_composed_prepare_sow_path.py

@pytest.mark.e2e
def test_composed_prepare_sow_path(e2e_db: KairixPaths) -> None:
    """Real config + 4 cc_pairs + prepare-sow skill → result envelope shape is well-formed."""
    config = load_yaml(fixtures / "prepare_sow_config.yaml")  # has 4 cc_pairs across obsidian + sharepoint + dex_crm + reference-library
    deps = build_factory_deps(paths=e2e_db, config=config)
    seed_corpus(deps, fixtures / "client_x_corpus/")
    sync_all_cc_pairs(deps)
    envelope = invoke_skill(deps, actor="agent-shape", skill="prepare-sow", task="SoW for Client-X AI TOM")
    assert envelope.included_collections == ("client-x-engagement", "reference-superannuation-au", "ai-operating-model-pattern", "team-engagement-lessons")
    assert all(c.result_count >= 1 for c in envelope.included_collections)
    assert envelope.excluded_collections == ()  # agent-shape has access to all 4
```

Per F45 every new top-level capability (skill, MCP tool, connector kind, extractor, chunker) ships its own E2E in the same commit.

---

## New F-rules required

| F-rule | What it enforces | Lives in |
|---|---|---|
| **F55** | Every Chunker plugin declares `version: str` AND writes it to `documents_media.chunker_version` on every emitted chunk | `scripts/checks/check_f55_chunker_version.py` |
| **F56** | Every connector under `kairix/connectors/<name>/` satisfies at least `SourceConnector` + one of `{PollConnector, CheckpointedConnector, EventConnector}` | `scripts/checks/check_f56_connector_capability_declaration.py` |
| **F57** | Every cc_pair lifecycle transition matches the declared state-machine (`SCHEDULED → INITIAL_INDEXING → ACTIVE → PAUSED ↔ ACTIVE / DELETING / INVALID`) — illegal transitions are blocked at the cc_pair-update layer | `scripts/checks/check_f57_ccpair_lifecycle_integrity.py` (runtime invariant, exercised by unit + integration tests) |
| **F58** | Every `HierarchyNode` emission has `raw_parent_id` either None (root) or referencing a previously-emitted node within the same `iter_containers()` call (parent-before-child invariant) | runtime invariant + contract test in `tests/contracts/test_hierarchy_emission.py` |
| **F59** | Every `Collection.sources[*].cc_pair` references a declared cc_pair (config-validation rule) | already in Wave D operator-config validator; F-rule enforces via `scripts/checks/check_f59_collection_source_refs.py` |
| **F60** | Every `ScopeProfile.entries[*].collection_name` references a declared collection (config-validation rule) | parallel to F59; same script family |
| **F61** | Every chunk write goes through `CollectionRouter` (no direct `_SqliteChunkWriter(collection=name)` outside the router) | `scripts/checks/check_f61_collection_router_singleton.py` — extends F38 |

Modified existing F-rules:

| F-rule | Change | Rationale |
|---|---|---|
| F36 | Add: each chunker plugin has matching `tests/bdd/features/chunker_<name>.feature` | parallel to connector/extractor F36 |
| F39 | Clarify: `sensitivity` may come from per-item hint OR per-collection-source override OR collection default OR cc_pair access_type→F39-map OR connector default (5-step chain) | per ADR v2 §1 |
| F43 | Extend to chunkers: `tests/contracts/test_<chunker>_chunker.py` required for each chunker plugin | parallel to connector/extractor F43 |
| F46 | Allow chunker plugins as BDD step entry points (alongside CLI / MCP / factory) | per Wave F |

---

## Fakes inventory (post-ADR-v2)

Per `feedback_canonical_fakes_first`: reach for `tests/fakes.py` Fake* classes before defining inline stubs.

| Fake | Implements (Protocol set) | Used by |
|---|---|---|
| `FakeSourceConnector` | `SourceConnector` (base only) | minimal connector contract |
| `FakePollConnector` | `SourceConnector + PollConnector` | obsidian-shaped tests |
| `FakeCheckpointedConnector` | `SourceConnector + CheckpointedConnector` | sharepoint/notion/slack-shaped tests |
| `FakeSlimConnector` | `SourceConnector + SlimConnector` | prune-cycle tests |
| `FakeSlimConnectorWithPermSync` | `SourceConnector + SlimConnectorWithPermSync` | perm-sync tests |
| `FakeEventConnector` | `SourceConnector + EventConnector` | webhook-driven tests |
| `FakeResolver` | `SourceConnector + Resolver` | failure-replay tests |
| `FakeHierarchyConnector` | `SourceConnector + HierarchyConnector` | hierarchy-emission tests |
| `FakeOAuthConnector` | `SourceConnector + OAuthConnector` | OAuth-flow tests |
| `FakeCredentialsProvider` | `CredentialsProviderInterface` | dynamic credential rotation tests |
| `FakeChunker` | `Chunker` | chunker-dispatch tests |
| `FakeCollectionRouter` | `CollectionRouter` interface | per-mapping routing tests |
| `FakeScopeProfileResolver` | `ScopeProfileResolver` | scope-resolution tests |
| `FakeFederatedConnector` | `FederatedConnector` interface | federation tests |
| `FakeFeatureFlagResolver` | (existing) | feature-flag tests |

Each Fake is constructor-injected per F1 / F2 / F6 (no monkey-patches, no env mutation, no test-only kwargs in production).

---

## CI Stage matrix (post-ADR-v2)

| Stage | Adds | Runtime budget |
|---|---|---|
| Stage 0 (arch-fitness) | F55 + F56 + F58 + F59 + F60 + F61 | +5s |
| Stage 2 (unit + bdd + contract) | property tests + new BDD scenarios | +20s |
| Stage 3 (integration) | failure-injection + migration tests | +1min |
| Stage 4 (security) | unchanged | — |
| Stage 4.5 (e2e) | per-skill E2E + per-cc_pair-shape E2E | +2min |
| Stage 5 (F9 coverage union) | covers new chunker + cc_pair + scope-resolver code paths | unchanged |

Total CI runtime delta: ~4 minutes added to the gate. Acceptable for the design complexity.

---

## Sabotage-proof discipline (extended)

Per `feedback_sabotage_must_be_executed`: every new test gets a sabotage proof (mutate → fail → restore) before commit.

For the test families above, the sabotage matrix:

| Test family | Sabotage target | Expected failure mode |
|---|---|---|
| Capability inventory | Add `retrieve_all_slim_docs` back to `ObsidianConnector` (re-advertising `SlimConnector`) | negative capability contract and capability-inventory test fail |
| Chunker contract | Change `MarkdownStructuralChunker.version` from "2" to "1" while keeping behaviour | F55 fires (version regression detected) |
| Property — intersection | Replace `compose_scope_profiles` with a union | hypothesis finds a profile pair where intersection isn't a subset |
| Failure-injection | Replace `CredentialExpiredError` handling with `pass` | cc_pair stays ACTIVE instead of moving to INVALID; test fails |
| Migration | Skip the Wave-A schema add | flag-on test fails because `connector_containers` table doesn't exist |
| HierarchyNode invariant | Emit a child before its parent | F58 contract test fails on missing-parent-reference |

Sabotage results documented in commit body per `feedback_sabotage_must_be_executed`.

---

## What this test architecture commits us to

Net-new test surface that lands across Waves A–G:

- 3 new test directories (`property/`, `failure_injection/`, `perf/`, `migrations/`) + READMEs (F23)
- 9 new capability-Protocol contract tests + per-connector capability-inventory tests
- ~10 new chunker contract tests (one per chunker plugin per Wave F)
- ~12 new property tests (composition rules, F39-min, dispatch determinism, hierarchy invariant)
- ~12 new failure-injection tests (one per failure mode in ADR v2 catalogue)
- ~7 new migration tests (one per Wave A–G)
- ~22 net-new BDD features (from §09)
- ~10 new E2E composed-path tests (one per skill + one per cc_pair-shape variant)

Total: ~85 net-new test files. Big surface, but it's the cost of moving from a single-Protocol single-cursor connector framework to a 9-capability multi-container topology that can drive 48+ connector kinds. The §11 implementation-gap analysis enumerates which of these is achievable per wave.
