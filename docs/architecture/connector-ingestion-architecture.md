# Connector + ingestion architecture — Bronze/Silver plugins, with Python-discipline locks

> **Status**: Implemented. The connector + ingestion framework shipped across Waves 0–5 (see §10 wave plan) and is mechanically enforced by F34–F44 (catalogued in `scripts/checks/_rule_catalogue.py`, run per-commit via pre-commit + `scripts/safe-commit.sh` + CI Stage 0). The forward evolution of this framework — canonical-topology collapse and per-connector resilience hardening — is tracked in `connector-architecture-refactor.md`.
> Names the connector + extractor + plugin architecture, defines the separation of concerns and Protocol seams between layers, and encodes the engineering patterns + fitness functions that close the gap between "Python by default" (the project's language strategy) and the strong-typing / encapsulation properties needed for a wide ingest surface that grows without refactoring.
>
> Companion to: `provider-plugin-architecture.md` (the precedent shape this document deliberately mirrors), `test-discipline-hardening.md` (the Wave 0 lock-in this implementation rides on — F45..F48 + `e2e_db` fixture + CI Stage 4.5), `feature-flag-architecture.md` (the cutover pattern that gated IM-6 and the flag-gated connectors), `streaming-bronze-plan.md` (the streaming-only bronze rollout that retired the filesystem blob store), `connector-architecture-refactor.md` (the canonical-topology + resilience-hardening evolution), `fact-layer.md`, and `ADR-018-dlt-connector-framework.md`.

## 1. Context and forcing functions

The connector framework was built to satisfy these requirements (all shipped — see §10):

- A pluggable `SourceConnector` Protocol with first-party Obsidian + SharePoint implementations.
- Mixed-media extraction (PDF, Office, OCR) — originals preserved, derivatives produced.
- Entity-signal emission alongside chunks — the entity-graph stage consumes signals downstream.
- A "Plain Python, no LLM" connector path, with the LLM-driven work (fact extraction, entity enrichment) staying on existing surfaces.
- Worker robustness: persistent cursor, per-batch transaction, dead-letter, tombstones on source delete.
- Schema additions that travel source metadata with chunks (`source_uri`, `source_modified_at`, `sensitivity`, `source_page`).

The architectural risk this document closes: **a wide surface area of connector and extractor plugins that grows over years without the existing kairix code-quality discipline carrying through into the plugin layer.** Python's runtime weaknesses (dict-as-record drift, untyped public returns, hidden coupling across modules, monkey-patched tests that hide design flaws) are real. They are not addressed by the language; they are addressed by mechanical gates.

The kairix codebase already operates under those gates for `kairix/core/`, `kairix/providers/`, and `kairix/transport/`. This document extends the gate regime to `kairix/core/connectors/`, `kairix/connectors/`, and `kairix/extractors/`.

## 2. Architecture — three layers and the seams between them

```
kairix/
  core/
    protocols.py                   ← canonical Protocol home (existing); adds
                                      SourceConnector, Extractor, BronzeStore,
                                      SilverProcessor, EntityGraphSink
    connectors/                    ← orchestration (Plain Python; no LLM)
      streaming_bronze.py          ← BronzeStore impl: metadata-only bronze (no raw-byte blobs;
                                      re-extract re-fetches via connector.fetch). Replaced the
                                      former filesystem-blob FilesystemBronzeStore — see
                                      streaming-bronze-plan.md
      silver.py                    ← SilverProcessor impl: chunking + signal extraction
      pipeline.py                  ← list_changes → fetch → bronze → silver → index → advance
      cursor_store.py              ← connector_cursors row management
      dead_letter.py               ← connector_deadletter row management
      registry.py                  ← entry-points dispatcher (mirrors kairix.providers)

  connectors/                      ← plug-points; one directory per source
    _base.py                       ← SourceConnector Protocol + ConnectorRegistry Protocol
    obsidian/                      ← watchdog + periodic full-scan reconciliation
    sharepoint/                    ← Microsoft Graph delta query
    dex_crm/                       ← CRM seed connector (flag-gated)
    m365_email_headers/            ← email signals only, no body (flag-gated)
    m365_calendar/                 ← calendar connector (flag-gated)
    # future: notion/, github/, confluence/, slack/, teams_transcripts/

  extractors/                      ← format-specific bytes-to-ExtractedDocument
    _base.py                       ← Extractor Protocol
    markitdown/                    ← default for PDF/DOCX/PPTX/XLSX/HTML
    pdf_fallback/                  ← pdfplumber for tables markitdown loses
    ocr/                           ← Pillow + opencv-headless + Tesseract; PaddleOCR opt-in
    office/                        ← python-pptx / python-docx / openpyxl (structure-aware)
    vision/                        ← vision-enhanced extraction (deferred); budget-gated
    passthrough/                   ← .md → .md no-op (Obsidian)
```

**Layer responsibilities — strict separation of concerns:**

- **`kairix/core/connectors/`** owns orchestration. It runs the per-batch transaction, advances cursors, writes Bronze, runs Silver, dispatches chunks to the index and signals to the entity-graph sink. It knows nothing about specific sources or formats.
- **`kairix/connectors/<name>/`** owns one source: how to authenticate, how to list changes since a cursor, how to fetch raw bytes for an item, how to render a deep-link. It does not chunk, does not extract, does not embed.
- **`kairix/extractors/<name>/`** owns one format family: how to take raw bytes plus a mime hint and produce an `ExtractedDocument` (markdown + per-page extractions + images + metadata). It does not know which source the bytes came from.

The seams are Protocols; the layers do not import each other.

## 3. Protocol contracts (canonical surface)

```python
# kairix/core/protocols.py — additions

class SourceConnector(Protocol):
    """One external source family. Implementations under kairix/connectors/<name>/
    register via the kairix.connectors entry-point group."""
    name: str  # "obsidian" | "sharepoint" | "dex_crm" | ...

    def list_changes(self, cursor: Cursor | None) -> Iterator[ChangeEvent]:
        """Stream changes since cursor. Resumable; cursor advances on batch commit."""

    def fetch(self, item_id: str) -> RawArtefact:
        """Fetch raw bytes + mime hint for an item."""

    def source_link(self, item_id: str) -> str:
        """Deep-link back to the source — surfaced in retrieval results."""

    def sensitivity_for(self, item_id: str) -> Sensitivity:
        """Return the sensitivity tier for this item. Default: connector's config tier."""


class Extractor(Protocol):
    """One format family. Implementations under kairix/extractors/<name>/
    register via the kairix.extractors entry-point group."""
    name: str
    version: str  # surfaced into documents_media.extractor_version

    def can_extract(self, mime: MimeType, magic_bytes: bytes) -> bool: ...
    def extract(self, raw: bytes, mime: MimeType) -> ExtractedDocument: ...
    def quality_ok(self, doc: ExtractedDocument) -> bool: ...
        # quality_ok drives escalation (markitdown → docling / OCR / vision)


class BronzeStore(Protocol):
    """Bronze-tier persistence of fetched artefacts. The shipped implementation is
    StreamingBronzeStore: it records a metadata-only bronze_records row
    (source_name, item_id, mime, fetched_at, content_hash) without persisting raw
    bytes to disk. The re-extract recovery path re-fetches bytes via
    connector.fetch(item_id); read() raises BronzeNotPersistedError. This replaced
    the original FilesystemBronzeStore (filesystem blob + SQLite pointer), which the
    SharePoint dogfood showed was unsustainable (~13 MB/item on disk). See
    streaming-bronze-plan.md."""
    def write(self, source_name: str, item_id: str, raw: bytes, mime: MimeType) -> BronzeRef: ...
    def read(self, ref: BronzeRef) -> tuple[bytes, MimeType]: ...
    def replay(self, source_name: str, since: datetime | None = None) -> Iterator[BronzeRef]: ...


class SilverProcessor(Protocol):
    """Chunking + entity-signal extraction. No LLM (the 'Plain Python' connector path)."""
    def process(self, raw: BronzeRef, extracted: ExtractedDocument,
                source_uri: str, source_modified_at: str,
                sensitivity: Sensitivity) -> SilverOutput: ...


class EntityGraphSink(Protocol):
    """Where entity signals land. Staged in SQLite; pushed to Neo4j by a separate
    worker job (decoupled per the Curator coupling boundary)."""
    def stage(self, signals: Sequence[EntitySignal]) -> int: ...
```

**Value object discipline — frozen dataclasses everywhere across the boundary:**

```python
# kairix/core/protocols.py — value objects

@dataclass(frozen=True)
class ChangeEvent:
    op: Literal["created", "modified", "deleted"]
    item_id: str
    modified_at: str  # ISO-8601 UTC
    parent_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class RawArtefact:
    raw: bytes
    mime: MimeType
    fetched_at: str

@dataclass(frozen=True)
class ExtractedDocument:
    markdown: str
    pages: tuple[Page, ...]      # per-page / per-slide / per-sheet extractions
    images: tuple[Image, ...]    # extracted, classified
    metadata: DocMetadata
    confidence: float            # average; drives OCR fallback

@dataclass(frozen=True)
class SilverOutput:
    chunks: tuple[Chunk, ...]            # → retrieval index
    entity_signals: tuple[EntitySignal, ...]  # → entity-graph stage

@dataclass(frozen=True)
class Chunk:
    text: str
    content_hash: str
    source_name: str
    source_uri: str
    source_modified_at: str
    source_page: int | None       # PDF page / PPTX slide / XLSX sheet index
    sensitivity: Sensitivity      # public | internal | client-confidential | personal
    # ... existing chunk fields

@dataclass(frozen=True)
class EntitySignal:
    kind: Literal["person", "org", "relationship"]
    value: str
    source_uri: str
    modified_at: str
    confidence: float
    sensitivity: Sensitivity
```

No `dict[str, Any]` returns from public Protocol methods. No `list[dict]`. The boundary is typed; Pydantic stays at the JSON edge (HTTP responses, config file load, MCP envelopes).

## 4. The Bronze/Silver split — and why the connector Protocol stays narrow

An alternative considered (and rejected) was putting both `write_bronze` and `process_silver` *inside* the connector Protocol. This document deliberately keeps the `SourceConnector` Protocol narrow (`list_changes` / `fetch` / `source_link` / `sensitivity_for`) and lifts Bronze and Silver to shared infrastructure under `kairix/core/connectors/`.

**Reason**: putting Silver inside the connector means every plugin re-implements chunking, image-extraction handoff, entity-signal extraction. Fitness functions that say "no cross-connector imports" then force chunking duplication. Lifting Silver to shared infrastructure preserves both the Plain-Python intent of the connector path *and* the enforceable layer separation.

Concretely, the pipeline runs:

```
for change in connector.list_changes(cursor):                  # per-source
    raw = connector.fetch(change.item_id)                      # per-source
    ref = bronze.write(connector.name, change.item_id, raw)    # core/connectors (streaming: metadata-only row, no blob)
    extractor = registry.resolve(raw.mime, raw.bytes[:8])      # extractors registry
    doc = extractor.extract(raw.raw, raw.mime)
    if not extractor.quality_ok(doc):
        doc = escalate(doc, raw)                               # markitdown → OCR → vision
    silver_out = silver.process(ref, doc,
                                source_uri=connector.source_link(change.item_id),
                                source_modified_at=change.modified_at,
                                sensitivity=connector.sensitivity_for(change.item_id))
    documents_writer.upsert(silver_out.chunks)                 # core/connectors
    entity_graph_sink.stage(silver_out.entity_signals)         # core/connectors
    cursor_store.advance(connector.name, change.cursor_token)  # core/connectors
# all of the above is one SQLite transaction; failure rolls back, cursor unchanged
```

Three failures map to three behaviours:
- **Fetch failure** — counted into `connector_deadletter`; cursor advances past the item after the configured retry count; sibling items proceed.
- **Extract failure** — escalation chain runs (markitdown → pdf_fallback → ocr → vision-if-budget); only after all escalators report `quality_ok = false` does the item land in dead-letter.
- **Silver failure** — rolls back the transaction; cursor unchanged; retried on next worker tick.

## 5. Engineering patterns (the Python downsides — and the specific defences)

This section is the heart of the document. It names each material Python weakness for the connector / ingest workload and maps it to a defence that is either already established in kairix or new in this document.

### 5.1 Weak runtime encapsulation

**Risk**: Python's module privacy is a convention (`_underscore`). Nothing prevents a connector from importing a sibling connector's private function.

**Defence (existing)**: F26 / F27 already enforce layer isolation for `kairix.core / providers / transport`. F1 prevents monkey-patching of internal symbols in tests.

**Defence (new)**:
- **F34** — `kairix/core/connectors/**` may not import `kairix/connectors/**` or `kairix/extractors/**` (Protocol-only).
- **F35** — `kairix/connectors/<a>/**` may not import another connector or any extractor (cross-plugin work goes through `kairix/core/connectors/`).
- **F38** — Silver processing (chunking, entity-signal extraction) may only land in `kairix/core/connectors/silver.py`. Stops connectors growing private chunkers.

### 5.2 Dict-as-record drift

**Risk**: Python idiom is to pass `dict[str, Any]` between functions. Three commits later nobody remembers which keys are required and the schema drifts.

**Defence (new)**:
- **F42** — every Protocol method on `SourceConnector` / `Extractor` / `SilverProcessor` / `BronzeStore` returns a frozen dataclass or a `tuple[FrozenDataclass, ...]`. Never `dict`, never `list[dict]`. Lints the method signatures via mypy + a small AST check.
- Pydantic is permitted only at the JSON edge (HTTP responses, YAML config, MCP envelopes). Inside kairix, frozen dataclasses.

### 5.3 Untyped or `Any`-typed returns

**Risk**: A Protocol declared with `-> Any` provides no compile-time safety; mypy strict cannot catch downstream misuse.

**Defence (existing)**: mypy strict is on for the whole project.

**Defence (new)**:
- **F41** — every plugin under `kairix/connectors/<name>/` and `kairix/extractors/<name>/` carries `py.typed`, is mypy-strict-clean, and has zero `# type: ignore` without F3-rationale. Checked over the full tree with no exemption list.

### 5.4 Hidden coupling across modules

**Risk**: A function in module A reads a module-level constant set by module B's import-time side effect. Refactoring A breaks B in ways static analysis doesn't see.

**Defence (existing)**: F2 (no `KAIRIX_*` env reads in tests), F4 (no `KAIRIX_*` env reads outside `paths.py` / `secrets.py`), F6 (no `*_fn=None` test-only kwargs in production).

**Defence (new)**:
- **F43** — every plugin has `tests/contracts/test_<plugin>_protocol.py` exercising the canonical fake plus the real implementation through the same contract test. Protocol compliance proven mechanically. Same shape as the existing `tests/contracts/test_protocols.py`.

### 5.5 Monkey-patched tests that hide design flaws

**Risk**: Python's flexibility encourages `@patch` and `monkeypatch.setattr` to "test" code that has no proper seam. The test passes; the seam is still missing; the design rots.

**Defence (existing)**: F1 (no internal-substitution patching of kairix code in tests), `tests/fakes.py` canonical fakes, the `feedback_no_monkeypatch` and `feedback_attribute_reassignment_is_monkeypatch` discipline. This regime is mature; it extends to the new plugins by F43.

### 5.6 Schema drift between extractor versions

**Risk**: A new version of markitdown changes the markdown output for the same PDF. Old derivatives are stale; re-extraction is needed but there's no way to identify them.

**Defence (new)**:
- **F40** — every `Extractor` plugin under `kairix/extractors/<name>/` declares `version: str` and writes it through to `documents_media.extractor_version`. Re-extracts become tractable (`kairix derivatives re-extract --extractor=markitdown --since=<version>`).

### 5.7 Sensitivity defaults that drift to "public"

**Risk**: A new connector forgets to populate `sensitivity` on chunks. The schema default is `public`. Confidential SharePoint content leaks into general search.

**Defence (new)**:
- **F39** — every chunk write must carry `source_uri`, `source_modified_at`, and `sensitivity` populated. Lints the chunk-write callsites; default-to-public is only valid when the connector config declares that tier explicitly. Same shape as F15 (boundary-enforcement at the write surface).

### 5.8 Parallel sync surfaces growing inside the codebase

**Risk**: A new connector grows its own polling loop inside `worker.py` because adding it via the registry felt heavyweight at the time. Two polling surfaces drift.

**Defence (new)**:
- **F37** — change-detection / sync code may only land under `kairix/connectors/<name>/` or `kairix/core/connectors/`. No parallel polling loops under `worker.py` or `corpus/`. Same shape as F29 (singular perf-measurement surface).

### 5.9 Plugin BDD parity

**Risk**: A new connector ships without behaviour tests because the BDD overhead "wasn't required". Two months later nobody knows what the plugin actually does on a delete event.

**Defence (new)**:
- **F36** — every plugin under `kairix/connectors/<name>/` has a matching `tests/bdd/features/connector_<name>.feature` AND appears as a Scenario Outline row in `tests/bdd/features/e2e_connector_sync.feature`. Same shape as F28 (provider plugin BDD parity).

## 6. Summary of new fitness functions

| Rule | What it locks | Mirrors |
|---|---|---|
| **F34** | `kairix/core/connectors/**` cannot import `kairix/connectors/**` or `kairix/extractors/**` | F26 |
| **F35** | `kairix/connectors/<a>/**` cannot import another connector or any extractor | F27 |
| **F36** | Every connector has a `connector_<name>.feature` and an `e2e_connector_sync.feature` Outline row | F28 |
| **F37** | Change-detection only under `kairix/connectors/<name>/` or `kairix/core/connectors/` | F29 |
| **F38** | Silver processing (chunking, signal extraction) only in `kairix/core/connectors/silver.py` | (new — singular Silver surface) |
| **F39** | Every chunk write carries `source_uri`, `source_modified_at`, `sensitivity` populated | F15 |
| **F40** | Every `Extractor` declares `version: str` and writes it through to `documents_media.extractor_version` | (new — re-extract tractability) |
| **F41** | Every plugin carries `py.typed`, mypy-strict-clean, zero unjustified `type: ignore` | (new — strictness at plugin boundary) |
| **F42** | Public Protocol returns are frozen dataclasses or tuples of them; never `dict`/`list[dict]` | (new — typed boundary) |
| **F43** | Every plugin has `tests/contracts/test_<plugin>_protocol.py` exercising canonical fake + real impl | F30 |
| **F44** | Engagement-scope code cannot import firm-scope storage clients (`psycopg`, `asyncpg`, …) | (new — two-scope boundary, see `connector-scope-topology/ADR.md`) |

All follow the F21 action-marked-failure template (`fix:` / `next:` / `run:`), run over the full tree with no baseline file, and wire into pre-commit + `scripts/safe-commit.sh` + CI Stage 0. Every violation blocks.

**Wave 0 dependency (test discipline)**: F34–F44 land on top of the Wave 0 hardening pass (`test-discipline-hardening.md`). The connector framework inherits the discipline by construction:

- **F45** new-capability BDD parity — every new `make_connector` / `make_extractor` symbol must ship with `tests/bdd/features/connector_<name>.feature` / `extractor_<name>.feature` in the same commit.
- **F46** BDD step impls must go through `factory.build_connector_pipeline` (Wave 1+ adds this factory function); direct `ConnectorPipeline(...)` construction is blocked.
- **F47** integration tests use the factory; the `e2e_db` fixture in `tests/conftest.py` is the canonical setup.
- **F48** every new top-level capability gets `tests/e2e/test_composed_<capability>_path.py` — for Wave 1+ that means `test_composed_connector_path.py`, etc.
F30, F46 and F47 have zero violations and no baseline, so a new connector / extractor surface that breaks any of them blocks at commit time.

F30 reached zero violations before Wave 1 dispatched; the connector framework starts on a clean composition-tested foundation.

## 7. Schema additions (Wave 1 migration)

```sql
-- Existing 'documents' table — additions
ALTER TABLE documents ADD COLUMN source_name TEXT;
ALTER TABLE documents ADD COLUMN source_uri TEXT;
ALTER TABLE documents ADD COLUMN source_modified_at TEXT;
ALTER TABLE documents ADD COLUMN source_page INTEGER;
ALTER TABLE documents ADD COLUMN sensitivity TEXT NOT NULL DEFAULT 'public';
CREATE INDEX idx_documents_source_uri ON documents(source_uri);

-- New tables
CREATE TABLE documents_media (
    hash TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    format TEXT NOT NULL,
    size_bytes INTEGER,
    page_count INTEGER,
    title TEXT,
    author TEXT,
    created_date TEXT,
    language TEXT,
    extraction_status TEXT DEFAULT 'pending',
    extraction_timestamp INTEGER,
    extractor_name TEXT,
    extractor_version TEXT
);

CREATE TABLE document_pages (
    hash TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    extracted_text TEXT,
    has_images INTEGER DEFAULT 0,
    image_descriptions TEXT,    -- JSON; vision-model output (Phase 3)
    PRIMARY KEY (hash, page_number),
    FOREIGN KEY (hash) REFERENCES documents_media(hash)
);

CREATE TABLE connector_cursors (
    source_name TEXT PRIMARY KEY,
    cursor_token TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE connector_deadletter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    item_id TEXT NOT NULL,
    failure_count INTEGER NOT NULL,
    last_error TEXT,
    last_attempt TEXT NOT NULL,
    UNIQUE(source_name, item_id)
);

CREATE TABLE bronze_records (
    source_name TEXT NOT NULL,
    item_id TEXT NOT NULL,
    raw_path TEXT NOT NULL,        -- streaming bronze writes a sentinel here (no blob on disk);
                                   -- BronzeRef.raw_path is None for streaming rows
    mime TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_hash TEXT,             -- sha256 of fetched bytes; lets re-extract detect drift
    PRIMARY KEY (source_name, item_id)
);

CREATE TABLE entity_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- person | org | relationship
    value TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    modified_at TEXT NOT NULL,
    confidence REAL NOT NULL,
    sensitivity TEXT NOT NULL,
    pushed_to_neo4j INTEGER DEFAULT 0,
    pushed_at TEXT
);
```

No blob bytes are persisted: streaming bronze records the metadata row only and re-fetches via `connector.fetch(item_id)` on the re-extract path (see `streaming-bronze-plan.md`). The bronze write is atomic with cursor advance inside the per-batch SQLite transaction. (The original `FilesystemBronzeStore` wrote blobs to `.kairix/bronze/<source>/<hash>`; the SharePoint dogfood — ~13 MB/item — showed that was unsustainable, and `StreamingBronzeStore` replaced it.)

## 8. Plugin discovery — entry points

First-party plugins register in kairix's own `pyproject.toml`:

```toml
[project.entry-points."kairix.connectors"]
obsidian          = "kairix.connectors.obsidian:make_connector"
sharepoint        = "kairix.connectors.sharepoint:make_connector"
dex_crm           = "kairix.connectors.dex_crm:make_connector"
m365_email_headers = "kairix.connectors.m365_email_headers:make_connector"
m365_calendar     = "kairix.connectors.m365_calendar:make_connector"

[project.entry-points."kairix.extractors"]
markitdown   = "kairix.extractors.markitdown:make_extractor"
pdf_fallback = "kairix.extractors.pdf_fallback:make_extractor"
ocr          = "kairix.extractors.ocr:make_extractor"
office       = "kairix.extractors.office:make_extractor"
passthrough  = "kairix.extractors.passthrough:make_extractor"
```

Operator selection in `kairix.config.yaml`:

```yaml
connectors:
  - name: obsidian
    sensitivity: internal
    config:
      root: /data/obsidian-vault
      collections: [02-Areas, 05-Knowledge]
  - name: sharepoint
    sensitivity: client-confidential
    config:
      # ``drives`` is a non-empty list. Each entry is ONE of:
      #
      #   (a) an explicit drive — a bare Graph drive-id string, or a
      #       mapping with ``drive_id`` (+ optional ``site_id`` /
      #       ``display_name`` / ``include_paths`` / ``exclude_paths``);
      #       OR
      #   (b) a SITE entry — a mapping naming ``site_id`` (and/or
      #       ``site_url``) and NO ``drive_id`` — meaning "auto-discover
      #       every document library on this site" (F42). The site's
      #       ``include_paths`` / ``exclude_paths`` are inherited by every
      #       discovered drive. Discovery runs LAZILY at sync time and
      #       re-runs each tick, so libraries added later are picked up
      #       automatically. A site that fails to resolve (not found /
      #       auth / throttle / empty) is logged + skipped for that tick;
      #       explicit drives and other sites still sync (per-site fault
      #       isolation).
      #
      # The M365 tenant / client / secret triple is resolved from secrets
      # (canonical identity ``(connector, m365, None, <leaf>)``) — it is
      # NOT declared here. Pin drives by id (not URL) for determinism
      # across site renames; use a site entry to let kairix self-discover.
      drives:
        - drive_id: <graph-drive-id>                # explicit drive
          site_id: <hostname,site-guid,web-guid>    # optional, informational
          include_paths: ["/Curated-Content"]       # optional folder scope
        - site_id: <hostname,site-guid,web-guid>    # SITE entry — discover all drives
          exclude_paths: ["/Archive"]               # inherited by every discovered drive
        # - site_url: https://contoso.sharepoint.com/sites/marketing
        #   # ``site_url`` is resolved to a site_id via Graph at sync time
        #   # (an alternative to pinning the composite ``site_id``).

extractors:
  default: markitdown
  escalate_chain: [pdf_fallback, ocr]
  vision_enabled: false                   # vision-enhanced extraction — deferred, off by default
```

Third parties ship a separate pip distribution declaring the same entry-point group. `pip install kairix-connector-foo` + `connectors: [foo]` works with zero kairix code change. **Third-party plugin sandboxing** (WASM/Extism) is a future direction; the shipped surface is first-party only.

## 9. BDD coverage matrix

| Layer | Feature files | Test seam | Proves |
|---|---|---|---|
| `kairix/core/connectors/` | `connector_pipeline.feature`, `connector_cursor.feature`, `connector_deadletter.feature`, `connector_bronze.feature`, `connector_silver.feature` | `FakeSourceConnector`, `FakeExtractor`, `FakeBronzeStore`, `FakeSilverProcessor` from `tests/fakes.py` | Orchestration works for any plugin combination |
| `kairix/connectors/<name>/` | `connector_<name>.feature` per plugin | Per-source HTTP/FS fixture | Auth, list-changes shape, fetch shape, source_link semantics, sensitivity tier |
| `kairix/extractors/<name>/` | `extractor_<name>.feature` per plugin | Recorded `bytes` fixtures (PDFs, DOCX, PPTX, scanned-PDF for OCR) | MIME handling, page-citation, `quality_ok` escalation gates |
| E2E | `e2e_connector_sync.feature` (Scenario Outline over connectors × extractors) | All-fake pipeline | "Operator configures connector X → docs flow into index with source_uri + sensitivity populated" |

F36 + F43 stop the "new plugin shipped without behaviour test" failure mode.

## 10. Wave plan (sequencing)

Mirrors the `provider-plugin-architecture.md` Wave 0/1/2/3 cadence; each wave a parallel-worktree batch, cherry-picked per the project's subagent dispatch playbook.

| Wave | Items | Parallel? | Depends on |
|---|---|---|---|
| **0 (ADR + arming) — DONE 2026-05-22** | This document; F34–F44 check scripts with empty (or seeded) baselines; CLAUDE.md edits; fitness-functions.md canonical entries. Landed in commits `acf89f81..6f8359c2` plus this doc's earlier commits. F41 + F43 baselines seeded at 7 entries each (existing provider plugins), later paid down; all baseline files were removed in PLA-472. All other F34–F44 baselines empty (vacuous-green; armed for Wave 1 surfaces). | foreground | — |
| **1 (scaffold) — DONE 2026-05-23** | SC-1 `kairix/core/connectors/` skeleton + Protocols (commit `3e12f236`) · SC-2 `kairix/connectors/_base.py` + entry-points (`da625018`) · SC-3 `kairix/extractors/_base.py` + entry-points (`41b22646`) · SC-4 schema migration v1→v2 with 6 new tables + 5 new columns on `documents` (`27e4f73f`) · SC-5 BDD feature stubs (`8bcae8b5`) · SC-6 worker `_default_connector_sync` seam (`d8b775c3`) · placeholder→canonical swap (`9eda46fb`). Decision 1 ratified: EntityGraphSink stages to SQLite `entity_signals`; async Neo4j push in a separate worker job (out of Wave 2 scope). F34–F44 already armed in connector-Wave-0 (separate from this Wave 1). | yes | Wave 0 |
| **2 (Obsidian end-to-end) — DONE 2026-05-24** | IM-1 CursorStore + DeadLetterStore impls with caller-owned-commit atomicity (`77667c06`) · IM-2 ConnectorPipeline.run_batch + bronze store + DefaultSilverProcessor + registry iter_* (`d954a053`; the original `FilesystemBronzeStore` shipped here was later replaced by `StreamingBronzeStore` — see `streaming-bronze-plan.md`) · IM-3 worker `_default_connector_sync` + `ConnectorSyncDeps` + `_SqliteChunkWriter`/`_SqliteEntityGraphSink` (`26ebc0c5`) · IM-4 passthrough + markitdown extractors + entry-points + BDD (`286ab5bb`) · IM-5 Obsidian connector with watchdog + reconciliation (`b6c23b58`). **IM-6** (DocumentScanner retirement via `obsidian_connector_primary`): cutover ran per `feature-flag-architecture.md` §4.2 (baseline → flip → 24h+ soak → post-flip gates); the Obsidian connector promoted to primary. | yes | Wave 1 |
| **3 (PDF mixed-media) — DONE 2026-05-23** | MM-1 pdfplumber fallback extractor (`a0fd9147`); MM-2 OCR extractor with Tesseract + pre-processing chain (deskew/binarise/orientation/layout, `01dde3c2`); MM-3 per-page chunk citation threaded end-to-end through Silver → SQL → search projection → MCP envelope (`efc407d6`). Decision 4 ratified: pdfplumber (MIT) shipped; pymupdf (AGPL) explicitly NOT shipped. Decision 5 ratified: Tesseract default; PaddleOCR opt-in is a future plugin. Reference-library PDF eval (NIST/OpenStax/APRA) deferred to a follow-up commit. | yes | Wave 2 |
| **4 (Office mixed-media) — DONE 2026-05-23** | OF-1 pptx slide-aware + speaker notes (`6579cf56`) · OF-2 docx heading-hierarchy + track-changes detection (`6dfd04d3`) · OF-3 xlsx sheet-as-document with merged-cell + formula handling (`a222d04e`). All three plugins: python-pptx/python-docx/openpyxl (MIT), lazy-imported through the extractor entry-points registry. | yes | Wave 3 |
| **5 (P1 connectors, flag-gated, 3 worktrees) — DONE 2026-05-22** | KP-1 dex_crm (`259c25c4`, flag `connector_dex_crm`) · KP-2 m365_email_headers (`08ac321a`, flag `connector_m365_email_headers`, OAuth2 client-creds auth helper) · KP-3 m365_calendar (`aad33570`, flag `connector_m365_calendar`). All landed at `introduce` stage default-off per `feature-flag-architecture.md`; auth via existing `kairix.secrets.get_secret()` chain — `connector-dex-api-key` for Dex; `connector-m365-{tenant-id,client-id,client-secret}` for M365 client-credentials. F54 both-branch tests + E2E rows for all three connectors in `e2e_connector_sync.feature`. Operator opt-in via `kairix.config.yaml` `features:` overlay; cutover protocol per flag flip when a tenant enables it. | yes | Wave 2 + Feature-flag PRs 1-4 |
| **6 (SharePoint, flag-gated)** | SP-1 Graph delta connector (flag `connector_sharepoint`); sensitivity tier wiring exercised end-to-end. Client-confidential surface; cutover protocol's sensitivity-parity check is non-negotiable. | foreground | Wave 5 |
| **7 (deferred, flag-gated)** | Vision-enhanced extraction (flag `extractor_vision_enabled`, cost-gated); Teams transcripts (flag `connector_teams_transcripts`); Curator-side EntitySignal → Neo4j push (flag `entity_signal_neo4j_push_enabled`). | — | Wave 6; Curator |
| **IM-6 (DocumentScanner retirement, flag-gated via `obsidian_connector_primary`)** | Stage 1: registry entry default-off; worker branches on the flag; legacy `DocumentScanner` runs by default. Stage 2: dogfood VM operator overlay → `true`; cutover protocol runs (baseline → flip → 24h+ soak → post-flip eval/perf/latency/sample-journey gates). Stage 3: 4 weeks of validation → registry PR `default=True`, `stage=cutover`. Stage 4: 4+ weeks cutover-stage with no rollbacks → retire flag, delete `kairix/core/db/scanner.py`. | sequential | Feature-flag PRs 1-7 |

Waves 0–2 deliver the connector-framework exit criteria (pluggable `SourceConnector` + Obsidian end-to-end). Waves 3–6 absorb the mixed-media extraction and P1-connector substance. Wave 7 is Curator-gated.

## 11. What this document is *not*

- **Not a Curator / LLM design.** The connector layer is "Plain Python, no LLM" — LLM-driven work (fact extraction in `kairix/corpus/ingest.py`, Curator enrichment) stays on its own surfaces. The connector path and the conversational corpus path are disjoint.
- **Not the firm-scope storage design.** Reflection-extractor schema, engagement-registry, audit envelope — all firm-scope, governed by the two-scope architecture (`connector-scope-topology/ADR.md`). F44 locks the boundary so this document's plugins cannot accidentally cross it.
- **Not a third-party plugin sandbox.** WASM/Extism is a future direction (see §8); the shipped surface is first-party only.

## 12. References

- `connector-scope-topology/` — **proposed evolution** of this framework. The Wave 0–5 framework defined here is foundational; the connector-scope-topology ADR adds the layered model (connector instance / container / collection / scope profile / skill) for multi-instance + multi-tenant + per-actor-scoped retrieval. See `connector-scope-topology/ADR.md`.
- `provider-plugin-architecture.md` — the architectural precedent this document mirrors
- `fitness-functions.md` — F-rule canon; this document adds F34–F44
- `ENGINEERING.md` — repository-wide testing patterns
- `fact-layer.md` — the fact-extraction surface that stays on its existing path
- `ADR-018-dlt-connector-framework.md` — the in-repo connector-framework ADR (the connector-framework brief this document realises)
- `connector-scope-topology/ADR.md` — the engagement-scope / firm-scope two-scope boundary that F44 locks (the in-repo home of the scope-architecture rationale)
- `streaming-bronze-plan.md` — the streaming-only bronze rollout that retired the filesystem blob store
