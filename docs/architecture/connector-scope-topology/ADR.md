# ADR — Connector / Collection / Scope Topology (v2)

**Status**: Proposed (2026-05-23; revised 2026-05-23 after Onyx framework deep-dive + chunking-strategy research). Pending review during IM-6 soak window.

**Context**: Current connector framework (Wave 0–5) hardwires `name = entry-point key = SQL collection = cursor scope = source identity`. That overload is only honest for the simplest case (one credential per logical store, one collection per credential, no per-actor scope enforcement). Onyx ships 48 connectors and the source-kind diversity it exposes (wiki-doc-store, ticketing, chat, cloud-drive, code, email, CRM, meeting transcripts, database, web, file-system) requires a richer topology. The chunking-strategy research (`08-chunking-and-entity-strategies.md`) further demands that the Silver layer dispatch into per-`(kind, mime)` chunkers rather than apply uniform paragraph chunking.

Companion analysis:
- `00-overview.md` — package nav
- `01-source-analysis.md` — per-source dimensional analysis (8 firsthand + 48-connector Onyx catalog)
- `02-use-cases.md` — 14 use cases across 5 modalities
- `03-bdd-scenarios.md` — 30+ Gherkin scenarios pinning behaviour
- `04-simulation.md` — 12 break points in the naive layered model + resolutions
- `05-non-functionals.md` — storage / freshness / latency / cost envelopes
- `06-onyx-comparative-analysis.md` — Onyx framework (`cd7c86e7`) patterns; 10 adoptions, 7 preserve-ours
- `07-research-closeout.md` — 5 open questions resolved (Dex poll-only, M365 body as flag, Notion teamspace policy, GitHub App-first, M365 calendar sensitivity wire-through)
- `08-chunking-and-entity-strategies.md` — 12 source kinds × chunking + entity-extraction + libraries + dispatch shape

## Decision

Adopt a **6-concern model** with explicit interface boundaries. Each concern has its own data shape, its own lifecycle, its own audit surface.

### 1. Connector (kind + config)

A `Connector` is the **configured target** — what we're talking to and how it's parameterised, distinct from credentials and from operational state.

```python
@dataclass(frozen=True)
class Connector:
    id: int                                       # surrogate primary key
    kind: str                                     # entry-point key (obsidian, sharepoint, notion, …)
    name: str                                     # operator-facing label, unique within deployment
    connector_specific_config: Mapping[str, Any]  # kind-specific config (vault_root, site_filter, …)
    refresh_freq: timedelta | None                # ingest cadence default
    prune_freq: timedelta | None                  # slim-doc reconcile cadence default
    perm_sync_freq: timedelta | None              # ACL refresh cadence default
    default_sensitivity: F39Tier                  # fallback per F39 chain
```

YAML shape:

```yaml
connectors:
  - kind: sharepoint
    name: sharepoint-corp
    connector_specific_config:
      tenant_id: <tenant-uuid>
      site_filter: ["site:engineering", "site:research"]
      sensitivity_label_map: { "abc-public": public, "abc-internal": internal }
    refresh_freq: 5m
    prune_freq: 24h
    perm_sync_freq: 1h
    default_sensitivity: internal
```

### 2. Credential (auth shape, encrypted)

A `Credential` is the **secret material** — decoupled from connector so the same auth blob can drive multiple scoped connectors, AND a connector's credential can rotate without losing operational state.

```python
@dataclass(frozen=True)
class Credential:
    id: int
    kind: str                                     # must equal a Connector.kind it can drive
    credential_json: SensitiveValue[dict]         # encrypted at rest (EncryptedJson column type)
    user_id: UUID | None                          # owner (delegated OAuth shapes); None for app-only
    admin_public: bool                            # operator-grant scope — can be assigned by admin to non-owner cc_pairs
```

YAML shape (operator-side; the encrypted material lives in KV / sidecar):

```yaml
credentials:
  - kind: sharepoint
    name: sharepoint-app-tenant
    credential_ref: kv://kairix/sharepoint-app-tenant-credentials
    admin_public: yes
```

### 3. ConnectorCredentialPair — the operational unit

The **binding** of one Connector + one Credential, with its own cursor scope, its own status, its own audit timestamps, its own access mode. This is the unit operators reason about ("the kairix-team SharePoint connector is paused"). The cc_pair_id is the cursor + deadletter scope key, replacing the overloaded `name` field of the v1 ADR.

```python
@dataclass(frozen=True)
class ConnectorCredentialPair:
    id: int
    connector_id: int
    credential_id: int
    name: str                                     # operator-facing ("kairix-team SharePoint")
    access_type: AccessType                       # PUBLIC | PRIVATE | SYNC (see §6)
    status: CCPairStatus                          # SCHEDULED | INITIAL_INDEXING | ACTIVE | PAUSED | DELETING | INVALID
    last_successful_index_time: datetime | None
    last_time_perm_sync: datetime | None
    last_time_external_group_sync: datetime | None
    last_time_hierarchy_fetch: datetime | None
    in_repeated_error_state: bool
    total_docs_indexed: int
    refresh_freq_override: timedelta | None       # overrides connector default
    prune_freq_override: timedelta | None
```

Key Onyx-derived insight: the cc_pair binds Connector × Credential and owns the timestamps. Same credential can drive two cc_pairs (one ingesting site:X, one ingesting site:Y) with independent cursors. Same Connector can rotate credentials over time without losing cursor state.

### 4. Container + HierarchyNode (per-cc_pair internal scope tree)

A cc_pair enumerates `Container`s within its scope (per-drive, per-channel, per-mailbox, per-repo, etc.). The Container is the **cursor scope unit** — each has its own delta token. Containers form a tree via `HierarchyNode`s — emitted parent-before-child by the connector during ingest so retrieval can answer "files in this folder / siblings of this doc / all docs under site:X" without re-deriving structure from `source_uri` prefixes.

```python
@dataclass(frozen=True)
class Container:
    cc_pair_id: int
    container_id: str                             # kind-specific opaque (drive_id, mailbox_id, channel_id, …)
    access_state: ContainerAccessState            # ACCESSIBLE | REVOKED | NOT_YET_GRANTED | TRANSIENT_ERROR
    cursor_token: str | None
    last_synced_at: datetime | None

@dataclass(frozen=True)
class HierarchyNode:
    cc_pair_id: int
    raw_node_id: str                              # source-stable id
    raw_parent_id: str | None                     # for parent-before-child traversal
    display_name: str
    link: str | None                              # source URL for navigation
    node_type: HierarchyNodeType                  # 12-value enum below
    external_access: ExternalAccess | None        # ACL inheritance start
    sensitivity_hint: F39Tier | None              # per-node sensitivity (e.g. private channel)

class HierarchyNodeType(Enum):
    FOLDER, SOURCE, SHARED_DRIVE, MY_DRIVE, SPACE, PAGE, PROJECT,
    DATABASE, WORKSPACE, SITE, DRIVE, CHANNEL
```

Schema: `connector_containers` and `connector_hierarchy_nodes` tables; primary keys `(cc_pair_id, container_id)` and `(cc_pair_id, raw_node_id)` respectively. The v1 ADR's `connector_cursors` table is **deprecated** — migration writes `(cc_pair_id, container_id=cc_pair_name, cursor_token=existing)` for single-container connectors.

### 5. Collection (retrieval bucket, decoupled from connectors)

A `Collection` aggregates one or more cc_pairs via filters, plus optional federated members (external search indices we want to compose without re-ingesting). Search ranks within / over the Collection's chunks.

```python
@dataclass(frozen=True)
class Collection:
    id: int
    name: str
    default_sensitivity: F39Tier
    on_unmapped_item: Literal["land_in_default_collection", "drop"]
    visibility: Literal["public", "engagement", "team", "private"]
    sources: tuple[CollectionSource, ...]
    federated_members: tuple[FederatedConnector, ...]
    group_grants: tuple[GroupGrant, ...]          # per-group access (was per-actor in v1)

@dataclass(frozen=True)
class CollectionSource:
    cc_pair_id: int
    source_path_filter: str                       # glob on item_id within cc_pair's hierarchy
    sensitivity_override: F39Tier | None

@dataclass(frozen=True)
class FederatedConnector:
    kind: str                                     # vespa | elastic | external-mcp
    endpoint: str
    query_strategy: str                           # operator-defined adapter

@dataclass(frozen=True)
class GroupGrant:
    group_id: str
    read: bool
    write: bool
    max_sensitivity: F39Tier
```

YAML shape:

```yaml
collections:
  - name: client-x-engagement
    default_sensitivity: confidential
    sources:
      - { cc_pair: kairix-team-sharepoint, source_path_filter: "site:client-x/**" }
      - { cc_pair: obsidian-personal,      source_path_filter: "01-Projects/Client-X/**" }
      - { cc_pair: dex-crm-personal,       source_path_filter: "orgs/client-x" }
    federated_members:
      - { kind: external-mcp, endpoint: https://other-search.tld/mcp, query_strategy: pass_through }
    group_grants:
      - { group_id: team-engagement, read: yes, write: no, max_sensitivity: confidential }
```

**Two material additions vs v1**: (a) `federated_members` — external search indices as collection members (Onyx pattern; useful for "compose existing operator searches without re-indexing"); (b) `group_grants` — per-group access alongside per-actor (scales better as teams grow).

Runtime layer: `CollectionRouter` consumes `(cc_pair_id, item_id)` and routes chunks to the matching collection's chunk-writer. Most-specific filter wins.

### 6. Scope profile + Skill + Chunker registry

Three runtime concerns folded into one decision because they all sit between the ingest layer and the search surface.

**Scope profile** — per-actor OR per-group bundle:

```python
@dataclass(frozen=True)
class ScopeProfile:
    actor_id: str
    actor_kind: Literal["agent", "human", "team", "group"]
    inherits_from: tuple[str, ...]                # group membership for transitive grants
    entries: tuple[ScopeEntry, ...]

@dataclass(frozen=True)
class ScopeEntry:
    collection_name: str
    read: bool
    write: bool
    max_sensitivity: F39Tier
```

Composition rules: collections by intersection across requesting principals; `max_sensitivity` by F39-min (least permissive); write rights by AND. Caller can opt into union via authorised `scope_composition: "union"` token.

**Skill** — composable search strategy:

```python
@dataclass(frozen=True)
class Skill:
    name: str
    task_collections: tuple[TaskCollection, ...]
    ranking: str                                  # named strategy ("fuse_then_rerank_by_skill_priors", …)
    iteration: Literal["one_shot", "sequential_per_task_collection", "graph_anchored"]

@dataclass(frozen=True)
class TaskCollection:
    name: str                                     # may match a real Collection or be virtual
    sources: tuple[CollectionSource, ...]
    weight: float
```

**Chunker registry** — per-`(kind, mime)` dispatch behind `SilverProcessor` (preserves F38: silver stays the single chunking surface; dispatch lives inside it):

```python
class Chunker(Protocol):
    version: str                                  # F40-equivalent: chunker version on every chunk
    def chunk(self, section: Section, *, context: ChunkContext) -> tuple[Chunk, ...]: ...

# Registry keyed by (kind, mime); default fallback per kind; default fallback overall is paragraph.
CHUNKER_REGISTRY: dict[tuple[str, str], Chunker] = {
    ("code", "text/x-python"):                TreeSitterChunker(language="python", v="1"),
    ("code", "text/x-go"):                    TreeSitterChunker(language="go", v="1"),
    ("ticketing", "application/json"):        PerTicketChunker(v="1"),
    ("chat", "application/json"):             ThreadAwareChunker(v="1"),
    ("wiki-doc-store", "text/markdown"):      MarkdownStructuralChunker(v="2"),
    ("office", "application/vnd.openxmlformats-officedocument.presentationml.presentation"): SlideChunker(v="1"),
    ("office", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): TabularRowChunker(v="1"),
    # … per 08-chunking-and-entity-strategies.md
}
```

Chunkers are versioned (`Chunker.version`), written to `documents_media.chunker_version` (alongside `extractor_version`). Bumping a chunker version triggers re-chunk on next sync for that kind; old chunks tombstoned. New F-rule: **F55 — every `Chunker` plugin declares `version: str` and writes it to `documents_media.chunker_version`** (parallel to F40).

### 7. Search strategy (runtime resolver)

```python
class SearchStrategy(Protocol):
    def scope_resolve(self, actors: list[str], skill: str | None, task: str) -> ResolvedScope: ...
    def execute(self, scope: ResolvedScope, query: str) -> ResultEnvelope: ...

@dataclass(frozen=True)
class ResolvedScope:
    collections: tuple[ResolvedCollection, ...]
    excluded_collections: tuple[ExcludedCollection, ...]

@dataclass(frozen=True)
class ResolvedCollection:
    name: str
    max_sensitivity: F39Tier
    weight: float

@dataclass(frozen=True)
class ExcludedCollection:
    name: str
    reason: Literal["actor_lacks_read", "sensitivity_cap_too_high", "container_revoked", "container_transient", "perm_sync_stale"]
    escalation_hint: str | None
```

`ResultEnvelope` carries per-source freshness, included / excluded collections (NO silent loss of context), per-collection result contribution counts, per-chunk source ACL state, per-chunk sensitivity tier.

---

## Connector Protocol — capability mix-ins (Onyx-derived)

The single flat `SourceConnector` Protocol from v1 splits into a **base + optional capabilities**. A connector implementation advertises capabilities by satisfying the relevant Protocols. The framework's runner uses `isinstance` checks against each capability Protocol to dispatch.

```python
# Required base
class SourceConnector(Protocol):
    kind: str
    def load_credentials(self, credentials: dict) -> dict | None: ...
    def iter_containers(self) -> Iterator[Container]: ...
    def fetch(self, item_id: str) -> RawArtefact: ...
    def source_link(self, item_id: str) -> str: ...
    def sensitivity_for(self, item_id: str) -> F39Tier: ...

# Optional capabilities (composed by inheritance / structural typing)
class PollConnector(Protocol):
    def list_changes(self, container: Container) -> Iterator[ChangeEvent]: ...

class CheckpointedConnector(Protocol):
    def load_from_checkpoint(self, container: Container, checkpoint: Checkpoint) -> CheckpointOutput: ...

class SlimConnector(Protocol):
    """Cheap ID-only enumeration for prune cycles. Returns SlimDoc with id + last_modified + maybe minimal metadata."""
    def retrieve_all_slim_docs(self, container: Container, start: datetime, end: datetime) -> Iterator[SlimDoc]: ...

class SlimConnectorWithPermSync(SlimConnector, Protocol):
    """Slim retrieval that also reports per-doc ACL — drives perm-sync cycles."""
    def retrieve_all_slim_docs_with_perms(self, container: Container, start: datetime, end: datetime) -> Iterator[SlimDocWithPerms]: ...

class EventConnector(Protocol):
    """Webhook-driven push surface."""
    def subscribe(self, callback_url: str) -> Subscription | None: ...
    def renew_subscription(self, subscription: Subscription) -> Subscription: ...
    def unsubscribe(self, subscription: Subscription) -> None: ...
    def handle_event(self, event: dict) -> Iterator[ChangeEvent]: ...

class Resolver(Protocol):
    """Per-document failure replay — re-pulls only docs that previously failed."""
    def reindex(self, failures: tuple[ConnectorFailure, ...], *, include_permissions: bool = False) -> Iterator[ChangeEvent]: ...

class HierarchyConnector(Protocol):
    """Emits the source's own folder/space/site tree as HierarchyNodes."""
    def load_hierarchy(self, cc_pair: ConnectorCredentialPair) -> Iterator[HierarchyNode]: ...

class OAuthConnector(Protocol):
    """For source kinds that need three-legged OAuth flow."""
    @classmethod
    def oauth_authorization_url(cls, state: str) -> str: ...
    @classmethod
    def oauth_code_to_token(cls, code: str) -> dict: ...
```

A `ConfluenceConnector` would declare:
```python
class ConfluenceConnector(SourceConnector, CheckpointedConnector, SlimConnector,
                         SlimConnectorWithPermSync, Resolver, HierarchyConnector,
                         OAuthConnector, CredentialsConnector):
    ...
```

An `ObsidianConnector` declares only:
```python
class ObsidianConnector(SourceConnector, PollConnector, HierarchyConnector):
    ...
```
It deliberately does **not** declare `SlimConnector` (PLA-553): a prune cycle deletes every indexed item missing from a slim enumeration, and a local vault walk cannot prove that enumeration complete (`Path.glob` silently skips a directory it cannot list). Obsidian's deletions come only from reconciliation, which compares the walk against known state; a prune cycle selecting by `isinstance(c, SlimConnector)` skips it.

This is more explicit than v1's flat Protocol — capability is declarative at the connector class, not buried in optional methods returning `None`. New F-rule: **F56 — every connector under `kairix/connectors/<name>/` declares at least `SourceConnector` + one of `{PollConnector, CheckpointedConnector, EventConnector}`**.

---

## RawArtefact + Section (per 08-chunking)

`RawArtefact` carries the raw bytes; the Extractor (per F38, F40) returns typed `Section`s:

```python
@dataclass(frozen=True)
class RawArtefact:
    raw: bytes
    mime: str
    sensitivity_hint: F39Tier | None              # per-item override (Purview label, Slack channel privacy, GitHub repo visibility, Drive sharing tier)
    source_modified_at: datetime
    metadata: Mapping[str, Any]

# Typed sections — discriminated union output of Extractor
@dataclass(frozen=True)
class TextSection:
    kind: Literal["text"] = "text"
    text: str
    link: str | None                              # per-section source URL
    heading_path: tuple[str, ...]                 # breadcrumbs

@dataclass(frozen=True)
class TabularSection:
    kind: Literal["tabular"] = "tabular"
    rows: tuple[tuple[str, ...], ...]
    headers: tuple[str, ...]
    sheet_name: str | None
    link: str | None

@dataclass(frozen=True)
class ImageSection:
    kind: Literal["image"] = "image"
    image_bytes: bytes
    alt_text: str | None
    ocr_text: str | None
    link: str | None

Section = TextSection | TabularSection | ImageSection
```

The chunker registry dispatches per `(kind, mime, section.kind)`. A pptx slide → ImageSection(s) + TextSection(s) per slide; the SlideChunker emits one chunk per slide pulling both. A docx → TextSection(s) with heading_path; the MarkdownStructuralChunker emits per-section chunks. A xlsx sheet → TabularSection; the TabularRowChunker emits per-row-group chunks.

---

## ChangeEvent + ConnectorFailure (typed first-class)

```python
class ChangeOp(Enum):
    CREATED, MODIFIED, ARCHIVED, ACCESS_LOST, DELETED

@dataclass(frozen=True)
class ChangeEvent:
    op: ChangeOp
    item_id: str
    modified_at: datetime
    container_id: str                             # the Container that emitted this
    parent_node_id: str | None                    # for hierarchy back-link
    sensitivity_hint: F39Tier | None
    metadata: Mapping[str, Any]                   # archived=True, etc.

@dataclass(frozen=True)
class ConnectorFailure:
    """Emitted mid-stream by the connector. Runner collects and Resolver.reindex() replays."""
    failed_document_id: str
    failure_kind: Literal["fetch", "extract", "silver", "writer", "sink", "rate_limit", "auth"]
    failure_message: str
    retry_after: float | None
```

Typed exception hierarchy at the framework boundary (Onyx-derived):

```python
class ConnectorValidationError(Exception): ...
class CredentialInvalidError(ConnectorValidationError): ...
class CredentialExpiredError(ConnectorValidationError): ...
class InsufficientPermissionsError(ConnectorValidationError): ...
class ContainerAccessDenied(Exception):
    """A specific container is no longer reachable; cc_pair stays alive for other containers."""
class ContainerTransient(Exception):
    """Rate limit / 503 / connection error; retry per retry_after."""
class UnexpectedValidationError(ConnectorValidationError):
    """Transient; does NOT disable the cc_pair."""
```

---

## AccessType — per-cc_pair access mode

Per Onyx (complementing F39 per-chunk tier):

```python
class AccessType(Enum):
    PUBLIC = "public"            # any actor in engagement sees everything in this cc_pair
    PRIVATE = "private"          # only explicit cc_pair-group-grants see
    SYNC = "sync"                # pull ACLs from source and enforce per-doc; perm_sync_freq controls cadence
```

`AccessType.SYNC` means "the source (SharePoint, Slack, etc.) is the source of truth for who-sees-what; kairix pulls + mirrors". F39 tier still applies on top; the two compose: actor reaches collection (scope-profile) → per-collection ACL further filters (cc_pair access_type=SYNC + per-doc ExternalAccess).

---

## Resolved break points (from `04-simulation.md`, expanded)

| # | Break | v1 resolution | v2 refinement |
|---|---|---|---|
| 1 | SourceConnector single-cursor | `iter_containers()` + per-container `list_changes(container)` | cc_pair owns the operational state; Container is the cursor scope unit; CheckpointedConnector + PollConnector capabilities differentiate cursor shapes |
| 2 | Chunk-writer bound to one collection | `CollectionRouter` per-connector | CollectionRouter at the cc_pair level (since a Connector can drive many cc_pairs each routing differently) |
| 3 | sensitivity_for hook is too late | `RawArtefact.sensitivity_hint` + four-step fallback | Five-step chain now (per-item-hint > collection_source.override > collection.default > cc_pair.access_type→F39-map > connector.default) |
| 4 | Sites.Selected revocation | `Container.access_state` | Plus typed `ContainerAccessDenied` exception + cc_pair `in_repeated_error_state` flag for operator visibility |
| 5 | archive vs delete | `ChangeEvent.op` enum extended | Same |
| 6 | Slack message edit | per-item-root RESET-and-WRITE | Same; chunker dispatch picks per-message chunking from registry |
| 7 | GitHub force-push | `reconcile_container(full=True)` | Resolver capability + cc_pair-level hierarchy refresh |
| 8 | Cross-source entity resolution | Curator's surface; provenance edges | Same; HierarchyNode emissions enrich the back-references |
| 9 | Inaccessible task_collection | `ResultEnvelope.excluded_collections` | Same |
| 10 | Aggregated query intersection | composition rule | Same; group-based grants make composition cheaper at scale |
| 11 | Webhook + reconciler universal | optional `subscribe`/etc. | EventConnector + SlimConnector as separate capabilities (push + reconcile-prune); Onyx pattern of `prune_freq` separate from `refresh_freq` adopted |
| 12 | Parallel per-container backfill | bounded ThreadPool | Same; rate-limit handlers shared via Redis when multi-worker (Onyx pattern) |

**New break points surfaced by Onyx research / chunking research:**

| # | Break | Resolution |
|---|---|---|
| 13 | OAuth token TTL < indexing-run duration | `OAuthConnector` capability + dynamic credential rotation under per-cc_pair lock (Onyx `OnyxDBCredentialsProvider` pattern) |
| 14 | Permission sync as a distinct concern | `SlimConnectorWithPermSync` + `cc_pair.last_time_perm_sync` separate from `last_successful_index_time` |
| 15 | Per-doc failure replay too expensive via re-running window | `Resolver.reindex(failures)` capability |
| 16 | Hierarchy queries (files-in-folder, siblings-of-doc) require re-deriving from `source_uri` | `HierarchyNode` first-class emission with `HierarchyNodeType` enum + `parent_id` chain |
| 17 | Code-aware chunking would mis-fire under uniform paragraph chunker | Chunker registry keyed by `(kind, mime, section.kind)` + F55 chunker versioning |
| 18 | Compose existing operator search indices without re-ingesting | `FederatedConnector` membership in `Collection` |

---

## Migration plan — 7 waves (back-compat per `feature-flag-architecture.md`)

### Wave A — schema additions (back-compat)

- New tables: `connectors`, `credentials`, `connector_credential_pairs`, `connector_containers`, `connector_hierarchy_nodes`, `collections`, `collection_sources`, `federated_connectors`, `group_grants`, `scope_profiles`, `skills`, `chunker_registry`.
- Extend `documents`: add `archived bool`, `access_lost bool`, `chunker_version text`.
- Extend `documents_media`: add `chunker_version text` (parallel to `extractor_version`).
- Extend `RawArtefact` dataclass: `sensitivity_hint` (default None).
- Extend `ChangeEvent.op` enum (back-compat).
- New typed exceptions (additive).

Feature flag: `topology_schema` (introduce stage default-off; controls whether new tables are populated alongside existing).

### Wave B — Protocol capability split

Extend `kairix.core.protocols` with the capability Protocols (`PollConnector`, `CheckpointedConnector`, `SlimConnector`, `SlimConnectorWithPermSync`, `EventConnector`, `Resolver`, `HierarchyConnector`, `OAuthConnector`). Existing 4 shipped connectors (obsidian, dex_crm, m365_email_headers, m365_calendar) get default-impl shims so they continue to satisfy the new shapes without behavioural change. (Obsidian's `SlimConnector` shim was later removed — PLA-553; see the capability mix-ins section.) F-rule **F56** lands (per-connector capability declaration check).

Feature flag: `topology_protocol` (introduce stage default-off).

### Wave C — runtime: cc_pair + CollectionRouter + Chunker registry

- cc_pair lifecycle (create / pause / resume / delete) + status state-machine.
- `CollectionRouter` in `kairix/core/connectors/silver.py` dispatching per-mapping.
- `ChunkerRegistry` keyed by `(kind, mime, section.kind)` with default fallback. F55 lands.
- `ResultEnvelope` extended with per-source freshness + included / excluded collections.
- `ScopeProfileResolver` reading `scope_profiles` YAML.

Feature flag: `topology_runtime` (introduce stage default-off). When OFF: behaviour identical to current (collection = cc_pair name, no scope enforcement at search, uniform chunker). When ON: new behaviour kicks in. Both branches tested per F54.

### Wave D — operator config promotion

Add a single `topology:` parent key (#305) with six nested blocks — `connectors:` / `credentials:` / `cc_pairs:` / `collections:` / `scope_profiles:` / `skills:` — to the operator config schema. The parent namespace prevents the Wave D `collections:` block from colliding with the legacy top-level `collections.shared` dict shape and aligns the YAML surface with the `topology_config` feature flag name. Validation rules:
- `cc_pairs.*.connector` references declared connector.
- `cc_pairs.*.credential` references declared credential.
- `collections.*.sources.*.cc_pair` references declared cc_pair.
- `scope_profiles.*.entries.*.collection_name` references declared collection.
- `skills.*.task_collections.*.sources.*.cc_pair` references declared cc_pair.

`kairix features status` extended to show topology diagnostics per actor. `kairix cc-pair list` / `kairix cc-pair pause <id>` etc. new operator CLI verbs.

### Wave E — per-connector opt-in to multi-container

For each connector kind that benefits, implement per-container path:
- `iter_containers()` enumerates real containers
- `load_from_checkpoint(container, checkpoint)` for CheckpointedConnector
- `retrieve_all_slim_docs(container, start, end)` for SlimConnector
- `load_hierarchy(cc_pair)` emitting HierarchyNodes
- Default sensitivity hint emission per kind
- Typed-exception emission per failure mode

Per-connector flag (`topology_<connector>`) so each plugin's adoption is independent. F54 both-branch tests per flag.

#### Wave E adoption status

| Connector | Flag | Status | Landed |
|---|---|---|---|
| `obsidian` | `topology_obsidian` | ✅ Pilot | v2026.5.24a1 |
| `dex_crm` | `topology_dex_crm` | 🟡 In flight | v2026.5.24a2 |
| `m365_email_headers` | `topology_m365_email_headers` | 🟡 In flight | v2026.5.24a2 |
| `m365_calendar` | `topology_m365_calendar` | 🟡 In flight | v2026.5.24a2 |
| `sharepoint` | `topology_sharepoint` | 🔴 Scoped | next slice (uses Wave B shims via SharePoint connector ship at v2026.5.24a1) |
| `notion` | `topology_notion` | 🔴 Backlog | future Wave 5 connector wave |
| `slack` | `topology_slack` | 🔴 Backlog | future Wave 5 connector wave |
| `github` | `topology_github` | 🔴 Backlog | future Wave 5 connector wave |
| `google_drive` | `topology_google_drive` | 🔴 Backlog | future Wave 5 connector wave |
| `teams` | `topology_teams` | 🔴 Backlog | future Wave 5 connector wave |
| `jira` | `topology_jira` | 🔴 Backlog | future Wave 5 connector wave |
| `confluence` | `topology_confluence` | 🔴 Backlog | future Wave 5 connector wave |

### Wave E.5 — tick safety + metadata propagation (2026-05-28)

Inserted between Wave E and Wave F after the 2026-05-27 morning + evening saturation incidents. Wave E.5 codifies three architectural responses landed in v2026.5.28 + this session:

**ADR-019 — compose-layer resource governance (shipped v2026.5.28 hotfix).** Every long-running container declares `cpus` + `mem_limit` + `blkio_config.weight`. Sum of ceilings stays under host capacity so OS + agent + tunnel have headroom. Env-overrideable for non-default hosts.

**ADR-020 — per-tick budget + disk-watermark gate (#328, shipped this session as `feat(wave-e5): #328`).** Every `SourceConnector` Protocol implementer declares `per_tick_max_items: int = 500` + `disk_watermark_min_free_bytes: int | None`. ConnectorPipeline enforces both: gates the tick when free disk falls below the watermark; persists the cursor + yields after the budget cap. F66 mechanically enforces declaration on every connector + framework tick-driven class. Recovery semantic: a 100k-item backlog at budget 500 converges over 200 ticks instead of one host-saturating drain.

**ADR-021 — per-source metadata normalisation (#329, shipped this session as `feat(wave-e5): #329`).** `SourceMetadata` frozen dataclass on the connector + extractor Protocols. `SourceConnector.metadata_for(item_id)` surfaces envelope metadata (modified_at, created_at, author, author_email, tags, properties); `Extractor.metadata_for(raw, mime)` surfaces document-body metadata. Silver merges connector > extractor > defaults. `Chunk` extends with `author` / `author_email` / `tags` / `metadata`. EntityGraphSink auto-emits Person from envelope author (high-confidence) ahead of the regex heuristic. F65 enforces `metadata_for` + propagation test per connector plugin.

**ADR-022 — container-level secret readiness gate (deferred to next release after Wave F).** Replaces the `kairix.service` systemd unit (eliminated post-incident in v2026.5.28's #332 fix) with a container entrypoint script that waits for `/run/secrets/kairix.env`. Phase 1 interim shape ships; Phase 2 ship deferred until Wave F to keep Wave E.5 scoped to in-flight bugs.

#### Wave E.5 fitness functions

| Rule | Scope | Status |
|---|---|---|
| F65 | every `kairix/connectors/<name>/` implements `metadata_for` + ships `tests/integration/test_<name>_metadata_propagation.py` | ✅ clean (paid down to zero this session) |
| F66 | every connector + tick-driven framework class declares `per_tick_max_items` + `disk_watermark_min_free_bytes` (or watermark-exempt rationale) | ✅ clean (paid down to zero this session) |

#### Wave E.5 retire-with-Wave-F

Once Wave F's chunker plugins land, Wave E.5's per-source metadata can become part of the chunker contract (each chunker reads `Chunk.author` / `Chunk.tags` to inform chunk boundaries). Watch for opportunities to fold the F55 chunker_version assertion + F65 metadata assertion into a single per-chunker contract test.

### Wave F — chunker plugins per source kind

Implement chunker plugins per `08-chunking-and-entity-strategies.md`:
- `TreeSitterChunker` (code: python/go/typescript/rust)
- `PerTicketChunker` (jira/linear/asana/zendesk)
- `ThreadAwareChunker` (slack/teams/discord)
- `MarkdownStructuralChunker v2` (notion/confluence/obsidian/bookstack)
- `SlideChunker` (pptx)
- `TabularRowChunker` (xlsx/airtable)
- `EmailThreadChunker` (gmail/m365/imap)
- `EventChunker` (calendar)
- `TranscriptChunker` (gong/fireflies)
- Web crawl chunker (trafilatura-extracted text)

Each ships with `version: str` (F55), a contract test (F43-equivalent for chunkers), and BDD coverage.

### Wave G — retirement

After all topology_* flags promote to default-ON for 4 weeks of cutover-stage soak:
- Retire flags + delete default-impl shims.
- Drop deprecated `connector_cursors` table.
- Drop the v1 `name = entry-point key = collection = cursor scope` overload entirely.

~12 months from Wave A landing.

---

## Operator-visible config example (post-migration)

The six Wave D blocks nest under a single `topology:` parent key
(#305) so the Wave D `collections:` block doesn't collide with the
legacy top-level `collections.shared` dict shape, and the YAML
namespace aligns with the `topology_config` feature flag name.

```yaml
topology:
  connectors:
    - kind: obsidian
      name: obsidian-personal
      connector_specific_config: { vault_root: /data/vaults/personal }
      refresh_freq: 5m
      default_sensitivity: internal

    - kind: sharepoint
      name: sharepoint-corp
      connector_specific_config:
        tenant_id: <uuid>
        sensitivity_label_map: { abc-public: public, abc-internal: internal, abc-confidential: confidential }
      refresh_freq: 5m
      prune_freq: 24h
      perm_sync_freq: 1h
      default_sensitivity: internal

  credentials:
    - kind: sharepoint
      name: sharepoint-app-tenant
      credential_ref: kv://kairix/sharepoint-app
      admin_public: yes

  cc_pairs:
    - connector: obsidian-personal
      credential: null    # no credential; local FS
      name: obsidian-personal-default
      access_type: PRIVATE

    - connector: sharepoint-corp
      credential: sharepoint-app-tenant
      name: kairix-team-sharepoint
      access_type: SYNC

  collections:
    - name: vault-projects
      sources:
        - { cc_pair: obsidian-personal-default, source_path_filter: "01-Projects/**" }
      default_sensitivity: internal

    - name: client-x-engagement
      sources:
        - { cc_pair: obsidian-personal-default, source_path_filter: "01-Projects/Client-X/**" }
        - { cc_pair: kairix-team-sharepoint,    source_path_filter: "site:client-x/**" }
      default_sensitivity: confidential
      group_grants:
        - { group_id: team-engagement, read: yes, write: no, max_sensitivity: confidential }

  scope_profiles:
    - actor_id: agent-shape
      actor_kind: agent
      inherits_from: [ team-shape-builder ]
      entries:
        - { collection_name: agent-shape/private-memory, read: yes, write: yes, max_sensitivity: restricted }

    - actor_id: team-shape-builder
      actor_kind: group
      entries:
        - { collection_name: team-shape-builder/decisions, read: yes, write: yes, max_sensitivity: confidential }
        - { collection_name: vault-projects,               read: yes, write: no,  max_sensitivity: internal }
        - { collection_name: client-x-engagement,          read: yes, write: no,  max_sensitivity: confidential }

  skills:
    - name: prepare-sow
      task_collections:
        - { name: client-x-engagement,         weight: 1.0 }
        - { name: reference-superannuation-au, weight: 0.7 }
        - { name: ai-operating-model-pattern,  weight: 0.8 }
        - { name: team-engagement-lessons,     weight: 0.5 }
      ranking: fuse_then_rerank_by_skill_priors
      iteration: sequential_per_task_collection
```

---

## Acceptance criteria

The ADR v2 is considered landed when:

1. **Wave A + B**: schema + Protocol capability shims merge with all flags off (no behavioural change). F54 both-branch tests exist for every new flag.
2. **Wave C runtime** lands with the dogfood VM's `obsidian-personal` cc_pair running in:
   - per-folder routing mode (`CollectionRouter` end-to-end)
   - chunker-registry dispatch (markdown structural chunker v2 active for the obsidian collection)
   - HierarchyNode emission (folder tree queryable via `kairix worker hierarchy show`)
3. **Wave E**: at least one tenant-credential connector (sharepoint OR notion OR jira) lands in multi-container mode with both-branch tests, proving per-container cursor + sensitivity-hint + perm-sync paths.
4. **Wave F**: at least 3 chunker plugins land (markdown structural, code via tree-sitter, per-ticket) with contract tests AND BDD coverage AND F55 version-string compliance.
5. **`kairix features status`** shows topology surface. Operator config reference doc at `docs/operations/configuration/connectors-and-collections.md`.
6. **IM-6 obsidian** promotes to cutover-stage under the new topology, not the legacy single-collection shape.

After all six: the topology_* flags retire over Wave G.

---

## Consequences

### Wins (refined from v1)

- **Multi-vault Obsidian, multi-tenant SharePoint, multi-org GitHub** all first-class via cc_pair triad.
- **Cross-source collections** + federated members (compose external search indices without re-ingest).
- **Per-actor + per-team + per-group access** enforced at search-time, not filtered post-hoc.
- **Per-skill task scoping** declaratively expressed in YAML.
- **Per-item sensitivity** (Purview labels, Slack channel privacy, GitHub visibility, Drive sharing tier) via 5-step fallback chain.
- **HierarchyNode** unlocks folder / space / site navigation queries.
- **Per-source chunker dispatch** so code, tickets, slides, tables, transcripts all chunk on their natural unit.
- **Failure-replay (Resolver) + prune (Slim) + perm-sync (SlimWithPermSync)** as separate scheduled cycles with independent cursors.
- **Webhook + reconciler universality** with framework-managed subscription lifecycle.
- **Typed exception hierarchy** at the connector boundary gives the runner type-narrowed handling.
- **Federated connector membership** in Collection composes external search indices the operator already runs.

### Tradeoffs

- **Schema breadth**: 12 net-new tables, multiple extended. Migration is back-compat per Wave A but the operational surface widens.
- **Capability Protocol explosion**: 9 optional Protocols means a connector author has more to think about. Mitigated by sensible defaults at the framework layer.
- **Operator config doubles** in line count: `connectors` + `credentials` + `cc_pairs` separated. Mitigated by validation rules + a `kairix init connector --kind X` scaffold that authors the triad in one go.
- **Chunker registry adds versioning concern**: F55 (chunker_version on every chunk) plus re-chunk-on-version-bump complexity. Mitigated by lazy re-chunk (only when the chunk's `chunker_version` < current registry version AND the item is next-touched by sync).
- **Cognitive load on retrieval callers**: scope profile + skill + task is more concept-set than "give me top-K". Mitigated by sensible defaults — most calls still produce sensible results with no skill invocation; defaults to the actor's profile + uniform ranking.

### Non-consequences (deliberately not addressed)

- **Curator + entity resolution semantics** — unchanged; topology hooks into existing Neo4j surface via chunk back-references. HierarchyNode emissions enrich it.
- **Hybrid retrieval pipeline** (BM25 + vector + RRF + intent + boosts) — unchanged in mechanics; WHAT collections it scopes over changes.
- **Bronze raw-blob storage** — unchanged within a cc_pair run.
- **Two-scope (engagement vs firm) boundary** per ADR-017 — unchanged; topology operates within one engagement scope.

---

## What's NOT yet covered (gates the next iteration)

The §09 extended BDD doc + §10 test architecture + §11 implementation-gap analysis are required to complete the design package:

- **Actor-perspective BDD scenarios** for operator / agent / human / auditor / external user that the current scenarios miss.
- **Test architecture**: which Protocol contracts get contract tests; how chunker plugins are tested in isolation; how scope-profile composition is property-tested.
- **Implementation-gap analysis**: explicit walk of `kairix/core/connectors/`, `tests/bdd/features/`, `tests/contracts/`, `kairix.config.yaml` schema to enumerate what changes per wave.

These three land next as `09-extended-bdd-scenarios.md`, `10-test-architecture.md`, `11-implementation-gap-analysis.md`.

---

## References

- `00-overview.md` — package nav + non-goals
- `01-source-analysis.md` — per-connector dimensional analysis + Onyx 48-connector catalog
- `02-use-cases.md` — 14 use cases
- `03-bdd-scenarios.md` — 30+ topology-pinning scenarios
- `04-simulation.md` — break-point walk
- `05-non-functionals.md` — storage / latency / freshness / cost
- `06-onyx-comparative-analysis.md` — Onyx framework patterns at `cd7c86e7`
- `07-research-closeout.md` — 5 open-question resolutions
- `08-chunking-and-entity-strategies.md` — 12-source-kind chunking + entity strategies
- `connector-ingestion-architecture.md` — Wave 0–5 framework this builds on
- `feature-flag-architecture.md` — Wave A–G flag pattern
- `provider-plugin-architecture.md` — parallel three-layer split
- `fact-layer.md` — entity resolution surface this composes with
- `ADR-017` (kairix-pro repo) — engagement / firm scope boundary
