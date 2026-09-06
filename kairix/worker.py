"""Background worker for scheduled tasks.

Runs inside the kairix-worker Docker container. Handles:
- Incremental document indexing (every hour)
- Entity relationship seeding (once a day at 3am)
- Health check logging (every 6 hours)

Usage:
    python -m kairix.worker
    # Or via Docker: docker compose exec kairix-worker worker
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairix.paths import (
    connector_sync_disabled,
    data_dir,
    document_root,
    maintenance_interval_seconds,
    maintenance_retention_days,
    maintenance_skip_noop_threshold,
    preflight_strict,
    worker_pause_flag_path,
    worker_state_path,
)
from kairix.worker_state import WorkerPhase, WorkerState, read_state, write_state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kairix.core.connectors.silver import DefaultSilverProcessor
    from kairix.core.embed.use_cases import EmbedPipelineResult
    from kairix.core.features.registry import FeatureFlag
    from kairix.core.protocols import Chunk, EntitySignal

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """ISO-8601 UTC timestamp string (matches every other topology table).

    Mirrors :func:`kairix.core.connectors.cc_pair._now` so the worker's
    ``topology_*`` writes carry the identical timestamp shape.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bump_cc_pair_total_docs_indexed(
    db: sqlite3.Connection,
    name: str | None,
    cc_pair_id: int | None,
    indexed: int,
) -> None:
    """Best-effort increment of ``topology_cc_pairs.total_docs_indexed``.

    Runs in its own transaction, committed AFTER ``pipeline.run_batch()`` has
    already committed the chunk writes (a separate transaction). Guarded so a
    counter failure logs and continues rather than abandoning every subsequent
    connector this tick. No-ops when nothing was indexed or the entry has no
    resolved cc_pair (``cc_pair_id is None``).
    """
    if indexed <= 0 or cc_pair_id is None:
        return
    try:
        db.execute(
            "UPDATE topology_cc_pairs SET total_docs_indexed = total_docs_indexed + ?, updated_at = ? WHERE id = ?",
            (indexed, _now_iso(), cc_pair_id),
        )
        db.commit()
    except Exception as exc:
        logger.warning("worker: connector %s — failed to bump total_docs_indexed: %s", name, exc)


def _stamp_cc_pair_last_poll(
    db: sqlite3.Connection,
    name: str | None,
    cc_pair_id: int | None,
) -> None:
    """SYNC-OBS blind-spot fix — stamp a per-source "successful poll" heartbeat.

    The doc-count bump (:func:`_bump_cc_pair_total_docs_indexed`) is the ONLY
    sync-path writer of ``topology_cc_pairs.updated_at`` and it short-circuits
    when ``indexed == 0``. So a connector that polled successfully, advanced
    its cursor, but surfaced zero new docs left ``updated_at`` frozen at the
    last doc-producing tick — byte-identical on ``kairix cc-pair list`` to a
    connector whose worker died days ago.

    This stamps ``last_successful_index_time`` AND ``updated_at`` on EVERY
    non-erroring batch (callers invoke it only after the batch returned without
    raising), regardless of doc count, so a healthy-quiet source shows a fresh
    ``updated`` while a dead one stays stale. Best-effort: a stamp failure logs
    and continues so the remaining connectors this tick are unaffected. Runs in
    its own transaction, like the doc-count bump.
    """
    if cc_pair_id is None:
        return
    try:
        now_iso = _now_iso()
        db.execute(
            "UPDATE topology_cc_pairs SET last_successful_index_time = ?, updated_at = ? WHERE id = ?",
            (now_iso, now_iso, cc_pair_id),
        )
        db.commit()
    except Exception as exc:
        logger.warning("worker: connector %s — failed to stamp last poll heartbeat: %s", name, exc)


# #224 phase 4 — pause-flag polling cadence.
# When the worker is in PAUSED phase, it sleeps this long between flag
# re-checks. Short enough that operators see resumption quickly (CLI tells
# them "may take up to 5s"), long enough not to thrash on a touch-file
# stat call. Exposed as a module constant so tests can run a couple of
# iterations through the pause-check without injecting the value.
PAUSE_POLL_INTERVAL_S = 5

# Task schedule (seconds between runs)
EMBED_INTERVAL = 3600  # 1 hour
ENTITY_SEED_INTERVAL = 86400  # 24 hours
HEALTH_CHECK_INTERVAL = 21600  # 6 hours
WIKILINKS_INTERVAL = 3600  # 1 hour — runs after embed; --changed mtime-filters
# SC-6 connector-framework seam — the worker tick that drives every
# registered SourceConnector through list_changes → fetch → bronze →
# silver → cursor.advance. 900s (15 min) is the Wave-1 default: short
# enough to feel responsive on a webhook-less source (Notion polling),
# long enough not to thrash Graph delta-tokens on quieter sources.
# Wave 2 fills in the body inside kairix/core/connectors/; Wave 1 only
# wires the dispatch slot. See docs/architecture/connector-ingestion-architecture.md §6.
CONNECTOR_SYNC_INTERVAL = 900  # 15 minutes
# Dispatch-table key for the connector-sync slot. Held as a constant
# rather than inline so the maintenance-cycle dispatch list, the per-task
# timestamp dict, and the return tuple stay in lock-step (F17).
_CONNECTOR_SYNC_KEY = "connector_sync"

# GH #334 — Neo4j entity-graph drain cadence. 600s (10 min) drains
# 3000 rows/hour at the default 500-row batch — fast enough that
# fresh signals reach Neo4j within ~30 min, slow enough that a
# transient Neo4j outage retries gracefully without thrashing.
# Operators with a large historical backlog run
# ``kairix curator drain --batch-size 5000 --max-batches 100``
# manually; the unattended cadence below protects the worker loop.
NEO4J_DRAIN_INTERVAL = 600  # 10 minutes

# R3 (#389) — SQLite WAL checkpoint cadence. Forces a periodic
# ``PRAGMA wal_checkpoint(TRUNCATE)`` so the WAL file can't grow
# unbounded. Production trace (2026-06-02) showed the WAL hit 3.6 GB
# under heavy connector + embed write traffic before being manually
# checkpointed; auto-checkpoint's ~1000-page (4 MB) threshold doesn't
# keep up under sustained load. 600s matches the neo4j drain cadence
# so both DB-maintenance ticks share the same operational rhythm.
WAL_CHECKPOINT_INTERVAL = 600  # 10 minutes

# PR-5 — orphaned-source dead-letter sweep cadence. The per-connector
# auto-drain (run after each connector batch) only drains CURRENTLY-ACTIVE
# connectors; dead-letters from a source whose connector was removed never
# drain through that path. This periodic sweep walks EVERY distinct
# source_name in connector_deadletter and drains the orphaned backlog over
# time. 3600s (1 hour) is deliberately slower than CONNECTOR_SYNC_INTERVAL
# (900s) so the per-connector drain almost always runs first; the sweep is
# the cheap, idempotent backstop for the orphaned remainder. Cheap when
# clean — an empty table is a single GROUP BY read.
DEADLETTER_SWEEP_INTERVAL = 3600  # 1 hour

# ADR-028 Wave F.4 — cadence for the bounded re-chunk sweep tick. 1h matches the
# embed cycle so a tick's freshly-written (un-embedded) chunks are visible to the
# next embed run. Gated OFF by default (re_chunk_sweep_enabled).
RECHUNK_SWEEP_INTERVAL = 3600  # 1 hour

# Idle backoff (#224): when embed runs find no work to do, the next-embed
# wait extends exponentially. Cap at 4 hours so we don't go totally silent
# on a long-idle vault but also don't churn CPU/IO every hour for nothing.
EMBED_BACKOFF_NOOP_THRESHOLD = 2  # after N consecutive no-ops, start backing off
EMBED_BACKOFF_MAX_INTERVAL = 14400  # 4 hours — cap on backed-off embed interval

# #224 phase 2 — maintenance-skip threshold.
# When the embed no-op streak hits this count, the three maintenance scans
# (entity_seed, health_check, wikilinks_inject) become pointless work and
# the worker skips them too until embed next finds work. Resolved at module
# import time from KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD via paths.py
# (F4 — env reads stay centralised). Threshold tuned to default 10 so the
# embed-backoff exponential has time to slow polling down before we silence
# maintenance, but operators can lower it on tiny shared hosts.
MAINTENANCE_SKIP_NOOP_THRESHOLD = maintenance_skip_noop_threshold()


def _default_embed() -> EmbedPipelineResult:
    """Default embed implementation — runs the embed use case directly.

    Returns the structured ``EmbedPipelineResult`` so the worker can log
    structured outcomes (embed counts, recall score, alerts) without
    depending on CLI exit-code semantics. Critically, this DOES NOT call
    the CLI ``main()`` — that path raises ``SystemExit`` on recall-gate
    failures and would terminate the worker process. The use case raises
    only on truly unrecoverable conditions.
    """
    from kairix.core.embed.use_cases import run_incremental_embed_pipeline

    return run_incremental_embed_pipeline()


def _default_entity_seed() -> None:
    """Default entity seed implementation — lazy-imports and runs store crawl."""
    from kairix.knowledge.store.cli import main as store_main

    store_main(
        [
            "crawl",
            "--document-root",
            str(document_root()),
        ]
    )


def _default_wikilinks_inject() -> None:
    """Default wikilinks inject — runs ``kairix wikilinks inject --changed``.

    The CLI's ``main`` may raise ``SystemExit`` (e.g. when no entities
    are loaded yet, before the entity seed has run). The worker's
    ``run_wikilinks_inject`` catches that to keep the worker alive.
    """
    from kairix.knowledge.wikilinks.cli import main as wikilinks_main

    wikilinks_main(["inject", "--changed"])


def _default_health_check() -> list[Any]:
    """Default health check — lazy-imports and runs all deployment checks."""
    from kairix.platform.onboard.check import run_all_checks

    return run_all_checks()


@dataclass(frozen=True)
class ConnectorSyncResult:
    """Structured outcome of one connector-framework sync tick.

    Wave-1 placeholder shape. The worker logs these counters at INFO so
    operators can see end-to-end progress without grep-ing per-connector
    logs. Wave 2 (orchestration under ``kairix/core/connectors/``) will
    populate the fields from the real per-batch transaction.

    Fields:
        synced: items successfully written to Bronze and processed through
            Silver in this tick (cursor advanced past each).
        failed: items where ``fetch`` raised after the configured retry
            count — counted toward dead-letter on the next tick.
        dead_letter_added: items moved into the dead-letter table this
            tick (so operators can alert on a non-zero delta).
    """

    synced: int = 0
    failed: int = 0
    dead_letter_added: int = 0
    # SYNC-OBS — quiet-vs-dead aggregate fields (purely additive; defaults
    # keep every existing ``ConnectorSyncResult(...)`` construction valid).
    #
    # * ``connectors_polled`` — how many connectors were actually driven
    #   through a batch this tick (excludes gated-off / failed-to-construct
    #   entries). A non-zero value with ``synced == 0`` is the canonical
    #   "polled OK, nothing new" signal that a dead worker can't produce.
    # * ``quiet`` — connectors that polled successfully but surfaced zero
    #   items (``items_seen == 0``). The healthy-and-idle bucket.
    # * ``poisoned_skipped`` / ``skipped_low_disk`` — rolled up from the
    #   per-batch results that the 3-tuple return used to discard, so a
    #   disk-blocked or all-poisoned source is no longer indistinguishable
    #   from a clean quiet tick.
    connectors_polled: int = 0
    quiet: int = 0
    poisoned_skipped: int = 0
    skipped_low_disk: int = 0


@dataclass(frozen=True)
class _ConnectorBatchOutcome:
    """SYNC-OBS — the rich per-connector batch outcome the worker keeps.

    ``_run_one_connector_batch`` previously collapsed the pipeline's
    :class:`~kairix.core.connectors.pipeline.BatchResult` to a
    ``(processed, dead_lettered, cc_pair_id)`` 3-tuple, dropping
    ``items_seen`` / ``cursor_advanced`` / ``poisoned_skipped`` /
    ``skipped_low_disk`` on the floor. This carries them through so the
    sync loop can log a per-source one-line summary and roll them up into
    :class:`ConnectorSyncResult`. Frozen per F42.
    """

    indexed: int
    dead_lettered: int
    cc_pair_id: int | None
    items_seen: int = 0
    poisoned_skipped: int = 0
    cursor_advanced: bool = True
    skipped_low_disk: bool = False


class _SqliteChunkWriter:
    """Minimal in-process :class:`~kairix.core.protocols.ChunkWriter`.

    Wave-2 IM-3 keeps the worker independent from the legacy
    ``DocumentScanner`` writer surface — there is no production
    ``DocumentsTableWriter`` yet. This writer persists each
    :class:`~kairix.core.protocols.Chunk` against the canonical
    ``documents`` + ``content`` + ``content_vectors`` + ``documents_fts``
    tables using the same shared :class:`sqlite3.Connection` the pipeline
    drives, so the per-batch transaction stays atomic.

    The writer never commits — the caller's per-batch transaction owns
    the commit (matches :class:`FilesystemBronzeStore` discipline).

    FTS5 invariant: every chunk write also lands a ``documents_fts`` row
    so BM25 retrieval can find it. Without this, the hybrid ranker
    silently degrades to vector-only for new-path chunks. Contract test
    ``tests/contracts/test_chunk_writer_fts_invariant.py`` and integration
    test ``tests/integration/test_connector_search_round_trip.py`` pin
    the pairing.
    """

    def __init__(self, db: sqlite3.Connection, collection: str) -> None:
        self._db = db
        self._collection = collection

    def upsert(self, chunks: Sequence[Chunk]) -> int:
        """Persist ``chunks`` to documents + content + content_vectors + documents_fts.

        Each chunk lands as one ``documents`` row keyed by ``(collection,
        path=source_uri+seq)``, one ``content`` row keyed by
        ``content_hash``, one ``content_vectors`` row carrying the chunk
        sequence, AND one ``documents_fts`` row so BM25 search finds it.
        Does NOT commit.
        """
        for source_uri in sorted({chunk.source_uri for chunk in chunks}):
            self.delete_by_source_uri(source_uri)
        written = 0
        now = _now_iso()
        for seq, chunk in enumerate(chunks):
            path = f"{chunk.source_uri}#{seq}"
            # Use UPSERT (ON CONFLICT DO UPDATE) rather than INSERT OR REPLACE.
            # INSERT OR REPLACE on a UNIQUE conflict DELETEs the old row and
            # INSERTs a new one — that allocates a fresh rowid, which orphans
            # the existing documents_fts row keyed by the old rowid. UPSERT
            # preserves the documents.id so the FTS row stays addressable.
            self._db.execute(
                "INSERT INTO documents "
                "(collection, path, hash, source_name, source_uri, "
                "source_modified_at, source_page, sensitivity, created_at, modified_at, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT (collection, path) DO UPDATE SET "
                "hash = excluded.hash, source_name = excluded.source_name, "
                "source_uri = excluded.source_uri, "
                "source_modified_at = excluded.source_modified_at, "
                "source_page = excluded.source_page, "
                "sensitivity = excluded.sensitivity, "
                "modified_at = excluded.modified_at, active = 1",
                (
                    self._collection,
                    path,
                    chunk.content_hash,
                    chunk.source_name,
                    chunk.source_uri,
                    chunk.source_modified_at,
                    chunk.source_page,
                    chunk.sensitivity,
                    now,
                    chunk.source_modified_at,
                ),
            )
            self._db.execute(
                "INSERT OR REPLACE INTO content (hash, doc, created_at) VALUES (?, ?, ?)",
                (chunk.content_hash, chunk.text, now),
            )
            self._db.execute(
                "INSERT OR REPLACE INTO content_vectors (hash, seq, pos) VALUES (?, ?, ?)",
                (chunk.content_hash, seq, 0),
            )
            # FTS5 write — look up the stable documents.id via the unique key
            # (collection, path), then DELETE-then-INSERT the FTS row so the
            # match-text reflects the current chunk.text on update.
            row = self._db.execute(
                "SELECT id FROM documents WHERE collection = ? AND path = ?",
                (self._collection, path),
            ).fetchone()
            if row is not None:
                doc_id = row[0]
                self._db.execute("DELETE FROM documents_fts WHERE rowid = ?", (doc_id,))
                self._db.execute(
                    "INSERT INTO documents_fts (rowid, filepath, title, doc) VALUES (?, ?, ?, ?)",
                    (doc_id, path, "", chunk.text),
                )
            written += 1
        return written

    def delete_by_source_uri(self, source_uri: str) -> int:
        """Delete every chunk whose ``source_uri`` matches.

        Required by the entity-summary projector (ADR-036 §Mechanics):
        before re-projecting an entity whose Wikidata description
        changed, the projector clears the prior chunk row(s) for
        ``entity://<QID>`` so the new ``content_hash`` doesn't leave a
        stale row behind.

        FTS5 cleanup runs in lockstep — for each matched ``documents``
        row we delete the paired ``documents_fts`` row by the same
        rowid so BM25 retrieval doesn't keep finding the old text.

        Returns the row count deleted (matches the parameterised
        ``DELETE`` row count, not the FTS5 cleanup count). Returns 0
        when no rows match — operators / callers can treat 0 as 'no
        prior chunk for this URI'. Does NOT commit.
        """
        # F63-bounded: every entity-summary write under #457 produces ≤1
        # row per source_uri, so this scan returns ≤1 row in practice.
        # Connector pipelines that adopt delete_by_source_uri for other
        # patterns must keep that one-URI-per-source invariant or accept
        # a tighter LIMIT.
        rows = self._db.execute(
            "SELECT id, hash FROM documents WHERE collection = ? AND source_uri = ?",  # F63-bounded: one URI per source
            (self._collection, source_uri),
        ).fetchall()
        hashes = {str(row[1]) for row in rows}
        for doc_id, _hash in rows:
            self._db.execute("DELETE FROM documents_fts WHERE rowid = ?", (doc_id,))
        cursor = self._db.execute(
            "DELETE FROM documents WHERE collection = ? AND source_uri = ?",
            (self._collection, source_uri),
        )
        for content_hash in hashes:
            row = self._db.execute("SELECT 1 FROM documents WHERE hash = ? LIMIT 1", (content_hash,)).fetchone()
            if row is None:
                self._db.execute("DELETE FROM content_vectors WHERE hash = ?", (content_hash,))
        return int(cursor.rowcount or 0)


class _SqliteEntityGraphSink:
    """Minimal in-process :class:`~kairix.core.protocols.EntityGraphSink`.

    Stages :class:`~kairix.core.protocols.EntitySignal` rows into the
    ``entity_signals`` table on the shared connection. A separate worker
    job (Curator-coupling boundary, Wave 3+) drains the table and pushes
    to Neo4j. Wave 2 only needs the staging side wired.

    Does NOT commit — the caller's per-batch transaction owns the commit.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def buffer(self, signals: Sequence[EntitySignal]) -> int:
        """Write entity signals to the ``entity_signals`` staging table.

        Batches every signal into a single ``executemany`` rather than N
        single-row ``execute`` calls — one prepared statement, one C-level
        bind loop — so a fast connector emitting thousands of signals per
        batch doesn't pay per-row Python/SQLite dispatch overhead. Behaviour
        is identical (same rows, same order). Does NOT commit — the caller's
        per-batch transaction owns the commit.
        """
        rows = [
            (sig.kind, sig.value, sig.source_uri, sig.modified_at, sig.confidence, sig.sensitivity) for sig in signals
        ]
        if not rows:
            return 0
        self._db.executemany(
            "INSERT INTO entity_signals "
            "(kind, value, source_uri, modified_at, confidence, sensitivity, pushed_to_neo4j) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            rows,
        )
        return len(rows)


def _topology_entry_for_cc_pair(connector: Any, cc_pair: Any) -> dict[str, Any]:
    """Build one canonical ingest entry dict from a connector + cc_pair.

    Single source of truth for the kind/name/config/extractor split so the
    live-sync reader (:func:`_load_connector_config_entries`, Task 4) and
    the re-extract reader (:func:`_load_connector_entry`, Task 5) build
    identical entry dicts. The overloaded legacy ``entry["name"]`` splits
    into the canonical values (D1/D2/D3):

      * ``kind`` — the plugin resolution key (``ConnectorConfig.kind``,
        == entry-point name == ``connector_<kind>`` flag suffix);
      * ``name`` — the cc_pair routing key (chunk-writer collection name),
        from ``CCPairConfig.name``;
      * ``config`` — the connector_specific_config mapping, read back as a
        dict at the per-connector boundary;
      * ``extractor`` / ``extractor_chain`` / ``extractor_config`` /
        ``extractor_chain_configs`` — the extractor wiring (D1) consumed by
        ``build_extractor_from_entry``.
    """
    from kairix.config.topology import config_pairs_to_mapping

    return {
        "name": cc_pair.name,
        "kind": connector.kind,
        "config": config_pairs_to_mapping(connector.connector_specific_config),
        "extractor": connector.extractor,
        "extractor_chain": list(connector.extractor_chain),
        "extractor_config": config_pairs_to_mapping(connector.extractor_config),
        "extractor_chain_configs": config_pairs_to_mapping(connector.extractor_chain_configs),
    }


def _parse_topology_entries(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse the merged mapping into one ingest entry per cc_pair.

    Shared parse+split used by both connector readers (Task 4 sync + Task 5
    re-extract). Reads the canonical ``topology`` block (parsed from the
    overlay-aware MERGED operator mapping), NOT the legacy top-level
    ``connectors:`` list — the wizard-onboarding fix: the setup wizard
    writes ``topology.connectors`` and both readers now read it from the
    same place.

    Yields **one entry per cc_pair** (D2): a topology connector referenced
    by N cc_pairs produces N entries (each cc_pair is the
    connector/credential/collection binding). A connector with **zero**
    cc_pairs is not ingestable — there is no collection target — so it is
    skipped with an INFO log.

    Returns ``[]`` when the mapping carries no parseable topology
    connectors; both readers treat that as "no connector".
    """
    from kairix.config.topology import parse_topology

    try:
        parsed = parse_topology(mapping)
    except Exception as exc:  # pragma: no cover — parse errors are rare and logged
        logger.warning("worker: failed to parse topology connectors — %s", exc)
        return []

    cc_pairs_by_connector_id: dict[str, list[Any]] = {}
    for cc_pair in parsed.cc_pairs:
        cc_pairs_by_connector_id.setdefault(cc_pair.connector, []).append(cc_pair)

    entries: list[dict[str, Any]] = []
    for connector in parsed.connectors:
        pairs = cc_pairs_by_connector_id.get(connector.id, [])
        if not pairs:
            logger.info(
                "worker: connector %s (kind=%s) has no cc_pair — not ingestable, skipping",
                connector.id,
                connector.kind,
            )
            continue
        for cc_pair in pairs:
            entries.append(_topology_entry_for_cc_pair(connector, cc_pair))
    return entries


def _load_connector_config_entries(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Enumerate ingestable connector entries from ``topology.connectors``.

    Task 4 of the connector canonical-collapse refactor: the live-sync
    enumeration delegates to the shared :func:`_parse_topology_entries`
    parse+split so it stays byte-for-byte identical to the re-extract
    reader (Task 5).
    """
    return _parse_topology_entries(mapping)


def _run_one_connector_batch(
    db: sqlite3.Connection,
    entry: dict[str, Any],
    bronze_root: Path,
    connector: Any,
) -> _ConnectorBatchOutcome:
    """Wire one connector entry through the :class:`ConnectorPipeline`.

    Returns a :class:`_ConnectorBatchOutcome` carrying ``items_indexed``,
    ``items_dead_lettered``, the resolved ``cc_pair_id`` AND the SYNC-OBS
    fields (``items_seen`` / ``poisoned_skipped`` / ``cursor_advanced`` /
    ``skipped_low_disk``) that the old 3-tuple return dropped. The
    ``cc_pair_id`` is the ``topology_cc_pairs.id`` the entry routes on
    (``None`` when the entry has no registered cc_pair — the legacy
    single-collection writer path); the caller uses it to increment the
    cc_pair's lifetime ``total_docs_indexed`` counter. Raises on
    registry / pipeline construction failures so the caller's per-entry
    try/except logs them and continues to the next connector.

    Task 4 canonical-collapse split: the overloaded legacy ``name`` is
    now three distinct values. The plugin is resolved via ``entry["kind"]``
    (``ConnectorConfig.kind`` == entry-point name); the chunk-writer and
    cursor routing key is ``entry["name"]`` (the cc_pair name). A cc_pair
    routes through :class:`CollectionRouter` when a cc_pair + mapping is
    registered for that name; :func:`legacy_chunk_writer` remains the
    fallback when no cc_pair has been registered yet (zero-behavioural-
    change guarantee for entries the operator hasn't fully wired).
    """
    from kairix.core.connectors import (
        ConnectorPipeline,
        CursorStore,
        DeadLetterStore,
    )
    from kairix.core.connectors.collection_router import _legacy_chunk_writer
    from kairix.core.connectors.registry import build_bronze_from_entry, build_extractor_from_entry

    name = entry["name"]
    # bronze_root is signature-only; streaming bronze writes no files.
    if bronze_root is not None:
        logger.debug("_run_one_connector_batch: bronze_root parameter is unused.")
    extractor = build_extractor_from_entry(entry)
    bronze_store = build_bronze_from_entry(entry, db=db)
    # N3: resolve the cc_pair id ONCE here and thread it through both the
    # chunk-writer resolution and the caller's return — the name→id lookup
    # is otherwise paid twice per entry (once inside the writer resolver,
    # once below). The id is None when the entry routes through the legacy
    # writer (no registered cc_pair) — the counter is a cc_pair concept, so
    # a legacy entry simply has nothing to bump.
    cc_pair_id = _lookup_cc_pair_id_by_name(db, name)
    # Route through CollectionRouter when a cc_pair exists for `name`;
    # legacy_chunk_writer remains the fallback when no cc_pair has been
    # registered for the entry.
    chunk_writer = resolve_chunk_writer_for_entry(db, name, cc_pair_id=cc_pair_id)
    silver = build_worker_silver_processor(db)
    pipeline = ConnectorPipeline(
        db=db,
        bronze=bronze_store,
        silver=silver,
        chunk_writer=chunk_writer,
        entity_graph_sink=_SqliteEntityGraphSink(db),
        cursor_store=CursorStore(db),
        dead_letter=DeadLetterStore(db),
    )
    result = pipeline.run_batch(connector, extractor)
    # PR-4: auto-drain permanently-unprocessable dead-letters for this
    # connector AFTER the batch — the pre-extract compat gate has already
    # kept this tick's own unsupported items out of the queue, so the drain
    # only mops up the historical poisoned backlog. Keyed on connector.name
    # (the connector KIND — the value every dead-letter write used), NOT the
    # cc_pair routing name. Best-effort: a drain failure never fails the sync.
    _auto_drain_connector(db, connector_name=connector.name, silver=silver)
    del _legacy_chunk_writer
    return _ConnectorBatchOutcome(
        indexed=result.processed,
        dead_lettered=result.dead_lettered,
        cc_pair_id=cc_pair_id,
        items_seen=result.items_seen,
        poisoned_skipped=result.poisoned_skipped,
        cursor_advanced=result.cursor_advanced,
        skipped_low_disk=result.skipped_low_disk,
    )


def _auto_drain_connector(
    db: sqlite3.Connection,
    *,
    connector_name: str,
    silver: Any,
) -> None:
    """Run the PR-4 dead-letter auto-drain pass for one connector.

    Thin worker-side seam over
    :func:`kairix.core.connectors.deadletter_drain.drain_connector_deadletters`.
    Best-effort: any drain-pass failure is logged and swallowed so a drain
    problem never aborts the surrounding sync tick (the drain itself is
    per-row best-effort; this guard covers a catastrophic enumeration
    failure). The pass is a cheap no-op when the connector's dead-letter
    queue holds no eligible rows.
    """
    from kairix.core.connectors.deadletter_drain import drain_connector_deadletters

    try:
        drain_connector_deadletters(db, connector_name=connector_name, silver=silver)
    except Exception as exc:  # drain must never fail the sync
        logger.warning("auto-drain: pass failed for connector=%s — %s", connector_name, exc)


def resolve_chunk_writer_for_entry(
    db: sqlite3.Connection,
    name: str,
    cc_pair_id: int | None = None,
) -> Any:
    """Resolve the chunk-writer for ``name``.

    Looks up cc_pair_id for ``name`` in ``topology_cc_pairs.name``. If
    found, returns a :class:`CollectionRouter` adapter for that cc_pair.
    If not found (or cc_pair has no mappings), falls through to the
    legacy writer — guarantees bit-for-bit behaviour parity for entries
    operator config hasn't yet registered.

    ``cc_pair_id`` is an optional pre-resolved id (N3): callers that have
    already looked it up (``_run_one_connector_batch``) pass it to avoid a
    second name→id query. When omitted (or ``None``) the id is resolved
    here from ``name`` — preserving the standalone-call behaviour.

    Returns ``Any`` because the union of ``_SqliteChunkWriter`` and
    ``_CollectionRouterChunkWriter`` is satisfied via duck-typing on
    the ``.upsert(chunks) -> int`` ChunkWriter Protocol shape; both
    return types live in private modules.
    """
    from kairix.core.connectors.collection_router import CollectionRouter, legacy_chunk_writer

    if cc_pair_id is None:
        cc_pair_id = _lookup_cc_pair_id_by_name(db, name)
    if cc_pair_id is None:
        return legacy_chunk_writer(db, collection=name)
    router = CollectionRouter(db, cc_pair_id)
    if router.mapping_count() == 0:
        # cc_pair exists but no collection_sources mapped — preserve legacy
        # single-collection behaviour. Operator-config validation blocks a
        # cc_pair landing without at least one mapping.
        return legacy_chunk_writer(db, collection=name)
    return _CollectionRouterChunkWriter(router=router)


_resolve_chunk_writer_for_entry = resolve_chunk_writer_for_entry


def _lookup_cc_pair_id_by_name(db: sqlite3.Connection, name: str) -> int | None:
    """SELECT topology_cc_pairs.id WHERE name = ?. Returns None on miss.

    Wraps the raw query so the worker doesn't reach into topology_*
    schema directly (the framework owns those tables; this is the
    operator-name → cc_pair-id bridge).
    """
    try:
        row = db.execute("SELECT id FROM topology_cc_pairs WHERE name = ?", (name,)).fetchone()
    except sqlite3.OperationalError:
        # topology_cc_pairs may not exist on a legacy schema (pre Wave A).
        return None
    return None if row is None else int(row[0])


class _CollectionRouterChunkWriter:
    """ChunkWriter Protocol adapter routing every chunk through CollectionRouter.

    The ChunkWriter Protocol exposes ``upsert(chunks) -> int``; the
    router exposes ``write_chunks(item_id, chunks) -> RouteResult``.
    The adapter bridges by extracting ``item_id`` from the first chunk's
    ``source_uri`` (matches the per-item invariant SilverProcessor
    enforces — every chunk in a single ``upsert`` batch shares one
    ``source_uri``).
    """

    def __init__(self, *, router: Any) -> None:
        self._router = router

    def upsert(self, chunks: Sequence[Chunk]) -> int:
        if not chunks:
            return 0
        item_id = chunks[0].source_uri
        result = self._router.write_chunks(item_id, chunks)
        return int(result.n_written)

    def delete_by_source_uri(self, source_uri: str) -> int:
        """Delete every chunk for ``source_uri`` across routed collections.

        Delegates to :meth:`CollectionRouter.delete_chunks_by_source_uri`
        so the adapter stays a thin shim around the router's lifecycle.
        """
        return int(self._router.delete_chunks_by_source_uri(source_uri))


def _load_merged_config_mapping_default() -> dict[str, Any]:
    """Default boundary read for the MERGED operator config (#492).

    Resolves base + overlay through
    :func:`kairix.config_layers.load_merged_mapping` — the same layered
    read path the setup wizard writes through. On the shipped compose
    the wizard's saves land on the ``KAIRIX_CONFIG_OVERLAY_PATH`` file
    while the base config is mounted read-only; a single-file read of
    the base would never see a wizard-saved ``topology:`` block.
    """
    from kairix.config_layers import load_merged_mapping

    return load_merged_mapping()


def _open_db_default() -> sqlite3.Connection:
    """Default DB-factory boundary call — wraps :func:`kairix.core.db.open_db`."""
    from kairix.core.db import open_db

    return open_db()


def _bronze_root_default() -> Path:
    """Default bronze-root resolver — ``data_dir() / "bronze"``."""
    return data_dir() / "bronze"


def _new_connector_from_entry(entry: dict[str, Any]) -> Any:
    """Construct one connector from a canonical topology entry."""
    from kairix.core.connectors import resolve_connector

    return resolve_connector(entry["kind"])(entry.get("config", {}))


@dataclass
class ConnectorSyncDeps:
    """Injectable dependencies for :func:`run_connector_sync_pipeline`.

    F6-clean: every field has a ``default_factory`` so production callers
    construct ``ConnectorSyncDeps()`` and get the real boundary calls;
    tests construct ``ConnectorSyncDeps(disabled_fn=lambda: True, ...)``
    and pass it as a single argument. Matches :class:`WorkerDeps`'s
    discipline for the sibling worker callables.

    Fields:
      * ``disabled_fn`` — short-circuit predicate; default
        :func:`connector_sync_disabled`.
      * ``config_mapping_fn`` — returns the parsed + MERGED operator
        config mapping (base + overlay, #492); default
        :func:`_load_merged_config_mapping_default`. Mirrors
        :class:`TopologyApplyDeps`'s seam so the canonical ingest
        redirect (Task 4) reads connectors off ``topology`` through the
        same overlay-aware path the wizard writes to.
      * ``flag_reader`` — resolves a feature-flag name to its effective
        value; default :func:`_default_flag_value`. Feeds
        :func:`connector_enabled` so per-connector enablement keys on
        connector KIND (``connector_<kind>``).
      * ``db_factory`` — opens a fresh SQLite connection; default
        :func:`kairix.core.db.open_db`.
      * ``bronze_root_resolver`` — returns the Bronze blob root; default
        ``data_dir() / "bronze"``.
      * ``connector_provider`` — returns the connector instance for an
        entry. Direct one-shot callers get a fresh connector; the worker's
        :class:`ConnectorSyncRuntime` supplies its lifetime-owned cache.
    """

    disabled_fn: Callable[[], bool] = field(default_factory=lambda: connector_sync_disabled)
    config_mapping_fn: Callable[[], dict[str, Any]] = field(default_factory=lambda: _load_merged_config_mapping_default)
    flag_reader: Callable[[str], bool] = field(default_factory=lambda: _default_flag_value)
    db_factory: Callable[[], sqlite3.Connection] = field(default_factory=lambda: _open_db_default)
    bronze_root_resolver: Callable[[], Path] = field(default_factory=lambda: _bronze_root_default)
    connector_provider: Callable[[dict[str, Any]], Any] = field(default_factory=lambda: _new_connector_from_entry)


@dataclass
class _OwnedConnector:
    """One cached connector and the config that constructed it."""

    config: dict[str, Any]
    instance: Any


class _ConnectorOwner:
    """Own connector instances for one worker lifetime."""

    def __init__(self, connector_factory_resolver: Callable[[str], Callable[[dict[str, Any]], Any]]) -> None:
        self._connector_factory_resolver = connector_factory_resolver
        self._connectors: dict[tuple[str, str], _OwnedConnector] = {}
        self._closed = False

    @staticmethod
    def _close_instance(instance: Any) -> None:
        close = getattr(instance, "close", None)
        if callable(close):
            close()

    def connector_for(self, entry: dict[str, Any]) -> Any:
        """Return the stable instance for an entry, replacing it on config change."""
        if self._closed:
            raise RuntimeError("connector runtime is closed")
        key = (entry["kind"], entry["name"])
        config = dict(entry.get("config", {}))
        owned = self._connectors.get(key)
        if owned is not None and owned.config == config:
            return owned.instance
        if owned is not None:
            self._close_instance(owned.instance)
        factory = self._connector_factory_resolver(entry["kind"])
        instance = factory(config)
        self._connectors[key] = _OwnedConnector(config=config, instance=instance)
        return instance

    def close(self) -> None:
        """Close every owned connector exactly once."""
        if self._closed:
            return
        self._closed = True
        for owned in self._connectors.values():
            self._close_instance(owned.instance)
        self._connectors.clear()


class ConnectorSyncRuntime:
    """Callable connector-sync slot with worker-lifetime connector ownership."""

    def __init__(
        self,
        *,
        deps: ConnectorSyncDeps | None = None,
        connector_factory_resolver: Callable[[str], Callable[[dict[str, Any]], Any]] | None = None,
    ) -> None:
        from dataclasses import replace

        from kairix.core.connectors import resolve_connector

        self._owner = _ConnectorOwner(connector_factory_resolver or resolve_connector)
        self._deps = replace(deps or ConnectorSyncDeps(), connector_provider=self.connector_for)

    def connector_for(self, entry: dict[str, Any]) -> Any:
        """Resolve an entry through this runtime's lifetime-owned connector pool."""
        return self._owner.connector_for(entry)

    def __call__(self) -> ConnectorSyncResult:
        return run_connector_sync_pipeline(self._deps)

    def close(self) -> None:
        self._owner.close()


@dataclass
class _SyncAccumulator:
    """SYNC-OBS — mutable per-tick rollup folded into a :class:`ConnectorSyncResult`.

    Keeps :func:`run_connector_sync_pipeline`'s loop body flat (one helper
    call per entry) so its cognitive complexity stays well under the F16
    ceiling. Every per-connector batch outcome is folded in via
    :meth:`fold`; :meth:`to_result` freezes the final aggregate.
    """

    synced: int = 0
    failed: int = 0
    dead_letter_added: int = 0
    connectors_polled: int = 0
    quiet: int = 0
    poisoned_skipped: int = 0
    skipped_low_disk: int = 0

    def fold(self, outcome: _ConnectorBatchOutcome) -> None:
        self.synced += outcome.indexed
        self.failed += outcome.dead_lettered
        self.dead_letter_added += outcome.dead_lettered
        self.connectors_polled += 1
        self.poisoned_skipped += outcome.poisoned_skipped
        if outcome.skipped_low_disk:
            self.skipped_low_disk += 1
        elif outcome.items_seen == 0:
            self.quiet += 1

    def to_result(self) -> ConnectorSyncResult:
        return ConnectorSyncResult(
            synced=self.synced,
            failed=self.failed,
            dead_letter_added=self.dead_letter_added,
            connectors_polled=self.connectors_polled,
            quiet=self.quiet,
            poisoned_skipped=self.poisoned_skipped,
            skipped_low_disk=self.skipped_low_disk,
        )


def _log_sync_source_summary(name: str | None, outcome: _ConnectorBatchOutcome) -> None:
    """SYNC-OBS — one structured line PER source PER tick (quiet ≠ dead).

    Emits e.g. ``sync source=sharepoint attempted=1 items_seen=0
    processed=0 dead_lettered=0 poisoned_skipped=0 cursor_advanced=false
    skipped_low_disk=false`` so an operator can tell, from a single line,
    that the source WAS reached this tick even when it surfaced nothing.
    """
    logger.info(
        "sync source=%s attempted=1 items_seen=%d processed=%d dead_lettered=%d "
        "poisoned_skipped=%d cursor_advanced=%s skipped_low_disk=%s",
        name,
        outcome.items_seen,
        outcome.indexed,
        outcome.dead_lettered,
        outcome.poisoned_skipped,
        str(outcome.cursor_advanced).lower(),
        str(outcome.skipped_low_disk).lower(),
    )


def _process_sync_entry(
    db: sqlite3.Connection,
    entry: dict[str, Any],
    bronze_root: Path,
    acc: _SyncAccumulator,
    connector_provider: Callable[[dict[str, Any]], Any],
) -> None:
    """Run one connector batch, fold its outcome into ``acc``, stamp counters.

    A per-connector failure (registry miss, plugin raise, pipeline
    rollback) is logged and swallowed so a single misconfigured connector
    does not halt sibling sync work — the same isolation the inline loop
    had before SYNC-OBS extracted it.
    """
    try:
        connector = connector_provider(entry)
        outcome = _run_one_connector_batch(db, entry, bronze_root, connector)
    except Exception as exc:
        logger.warning("worker: connector %s failed — %s", entry.get("name"), exc)
        return
    acc.fold(outcome)
    _log_sync_source_summary(entry.get("name"), outcome)
    name = entry.get("name")
    # Wire the operator-facing counter: increment (not recompute) the
    # cc_pair's lifetime total_docs_indexed so `kairix cc-pair list` stops
    # reporting docs=0. ``outcome.indexed`` already excludes deleted +
    # dead-lettered items. The bump short-circuits on indexed==0.
    _bump_cc_pair_total_docs_indexed(db, name, outcome.cc_pair_id, outcome.indexed)
    # SYNC-OBS blind-spot fix: stamp the per-source heartbeat on EVERY
    # non-erroring batch (even a zero-doc poll) so a quiet source shows a
    # fresh ``updated`` on ``cc-pair list`` instead of looking dead.
    _stamp_cc_pair_last_poll(db, name, outcome.cc_pair_id)


def run_connector_sync_pipeline(deps: ConnectorSyncDeps | None = None) -> ConnectorSyncResult:
    """Drive one tick across every configured connector.

    Reads the operator's ``kairix.config.yaml`` ``connectors:`` list,
    resolves each plugin via the entry-point registry, composes the
    canonical :class:`~kairix.core.connectors.ConnectorPipeline` against
    the shared SQLite connection, and runs one batch per connector.

    Returns a :class:`ConnectorSyncResult` aggregating ``items_indexed``
    and ``items_dead_lettered`` across every connector. Per-connector
    failures (registry miss, plugin raise, pipeline rollback) are logged
    and the loop continues — a single misconfigured connector does not
    halt sibling sync work.

    Short-circuits to a zero-counter result when ``deps.disabled_fn``
    (default :func:`kairix.paths.connector_sync_disabled`) returns True
    OR when no connectors are configured (the common case on a vault-
    only deploy).

    Per F37 this function MUST NOT import change-detection libraries
    (``watchdog`` / ``msgraph`` / ``notion_client`` / ``dulwich`` /
    ``slack_sdk.rtm``). Imports route through ``kairix.core.connectors``
    (orchestration) and ``kairix.core.db`` (transaction); the actual
    sync libraries land transitively only when a configured connector
    factory loads its own implementation module.

    See docs/architecture/connector-ingestion-architecture.md §6.
    """
    deps = deps if deps is not None else ConnectorSyncDeps()
    if deps.disabled_fn():
        logger.info("worker: connector sync disabled via KAIRIX_CONNECTOR_SYNC_DISABLED")
        return ConnectorSyncResult(synced=0, failed=0, dead_letter_added=0)

    entries = _load_connector_config_entries(deps.config_mapping_fn())
    if not entries:
        return ConnectorSyncResult(synced=0, failed=0, dead_letter_added=0)

    bronze_root = deps.bronze_root_resolver()

    from kairix.core.db.schema import create_schema

    db = deps.db_factory()
    try:
        create_schema(db)
        acc = _SyncAccumulator()
        for entry in entries:
            # D3: enablement keys on connector KIND (``connector_<kind>``).
            # A registered-and-OFF kind is skipped (logged); a flagless kind
            # always runs. A sibling connector in the same tick is unaffected.
            if not connector_enabled(entry["kind"], deps.flag_reader):
                logger.info("worker: connector %s gated off (flag connector_%s OFF)", entry["kind"], entry["kind"])
                continue
            _process_sync_entry(db, entry, bronze_root, acc, deps.connector_provider)
        return acc.to_result()
    finally:
        db.close()


def run_via_connector_pipeline(deps: ConnectorSyncDeps | None = None) -> ConnectorSyncResult:
    """Flag-ON branch — drive every configured connector through the
    canonical :class:`~kairix.core.connectors.ConnectorPipeline`.

    Thin shim over :func:`run_connector_sync_pipeline`. The split
    exists so the OFF and ON branches stay symmetrical in
    :func:`_default_connector_sync` — one named helper each, no inline
    orchestration. Emits a branch-identifier INFO log so operators
    (and BDD scenarios) can see which path the flag selected this tick.

    ``deps`` is the F6-clean injection seam — production callers omit
    it and the default ``ConnectorSyncDeps()`` factory wires the real
    boundary calls; BDD + integration tests pass a tmp_path-rooted
    deps object so the pipeline runs against a sandboxed DB / config.
    """
    logger.info("worker: connector sync routing via obsidian connector pipeline (flag ON)")
    return run_connector_sync_pipeline(deps)


@dataclass(frozen=True)
class ReextractResult:
    """Outcome of :func:`run_reextract_dead_letter`.

    Frozen per F42. Fields:

    * ``recovered`` — items where extract+silver+writer succeeded;
      dead_letter row deleted.
    * ``still_failing`` — items that raised again; dead_letter row
      kept with bumped failure_count.
    * ``skipped_no_bronze`` — items whose bronze_records row was
      absent (typical after the 2026-05-25 orphan-prune recovery
      where some dead_letter rows pre-date the surviving bronze).
    * ``skipped_no_connector`` — items whose source_name isn't
      configured in the current kairix.config.yaml (operator removed
      the connector but old dead_letter rows remain).
    * ``skipped_source_unavailable`` — Phase 5: streaming-mode rows
      where ``connector.fetch(item_id)`` raised (item deleted from
      source, auth failed, source HTTP 5xx, etc.). The dead_letter row
      is kept for operator triage.
    """

    recovered: int
    still_failing: int
    skipped_no_bronze: int
    skipped_no_connector: int
    skipped_source_unavailable: int = 0  # added in Phase 5


def run_reextract_dead_letter(
    *,
    source_name: str,
    db: sqlite3.Connection | None = None,
    bronze_root: Path | None = None,
    config_mapping: dict[str, Any] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> ReextractResult:
    """Re-extract every dead-lettered item for ``source_name``.

    Recovers from past extract failures (e.g. missing libraries fixed
    in a later release) without requiring the source to re-emit the
    items. Walks ``DeadLetterStore.list(source_name)`` and for each
    entry:

    1. Looks up the ``bronze_records`` row for ``(source_name, item_id)``.
       Missing → skipped_no_bronze.
    2. Reads the raw bytes via ``bronze.read(ref)``.
    3. Resolves the connector from the canonical ``topology`` block so
       ``source_link(item_id)`` and ``sensitivity_for(item_id)`` come
       from the same connector instance that wrote bronze originally.
       No matching cc_pair → skipped_no_connector.
    4. Runs ``extractor.extract(raw, mime)`` (whatever extractor is
       currently registered — picks up fixes like #322 markitdown
       extras). Failure → still_failing (row stays in dead_letter,
       failure_count incremented).
    5. Runs silver → chunk_writer → entity_graph_sink.
    6. Clears the dead_letter row.
    7. Commits per item (chunked-commit principle from #321).

    Use after a Dockerfile / connector fix lands to recover items that
    dead-lettered under the old behaviour. ``dry_run`` walks the same
    logic but commits nothing — useful for sizing the recovery before
    committing to it. ``limit`` caps the number of items processed
    (None = all).

    Task 5 (connector canonical-collapse): the connector entry is read
    from the canonical ``topology`` block in the overlay-aware merged
    operator mapping — the same place the live sync path reads — NOT the
    legacy single-file ``connectors:`` list. ``config_mapping`` injects an
    explicit merged mapping (tests pass the wizard-shaped topology dict);
    when ``None`` it resolves via :func:`_load_merged_config_mapping_default`
    (base + overlay).
    """
    from kairix.core.connectors import DeadLetterStore
    from kairix.core.db import open_db
    from kairix.core.db.schema import create_schema

    mapping = config_mapping if config_mapping is not None else _load_merged_config_mapping_default()

    db_owned = False
    if db is None:
        db = open_db()
        db_owned = True
    try:
        create_schema(db)
        bronze_root_resolved = bronze_root if bronze_root is not None else _bronze_root_default()
        dead_letter = DeadLetterStore(db)

        entry = _load_connector_entry(source_name, mapping)
        if entry is None:
            rows_no_conn = dead_letter.list(source_name)
            return ReextractResult(
                recovered=0,
                still_failing=0,
                skipped_no_bronze=0,
                skipped_no_connector=len(rows_no_conn),
            )

        # Phase 5: re-extract reads bronze per-row, not per-config — old
        # filesystem-shape rows + new streaming-shape rows can coexist in
        # the same dead_letter table. _read_raw_for_reextract dispatches
        # on ref.raw_path. No bronze store is constructed at this level.

        connector, extractor, silver, chunk_writer, entity_graph_sink = _build_reextract_components(
            entry=entry,
            db=db,
        )
        rows = dead_letter.list(source_name)
        if limit is not None:
            rows = rows[:limit]
        return _reextract_rows(
            rows=rows,
            db=db,
            bronze_root=bronze_root_resolved,
            extractor=extractor,
            silver=silver,
            chunk_writer=chunk_writer,
            entity_graph_sink=entity_graph_sink,
            connector=connector,
            dead_letter=dead_letter,
            dry_run=dry_run,
        )
    finally:
        if db_owned:
            db.close()


def _load_connector_entry(source_name: str, mapping: dict[str, Any]) -> dict[str, Any] | None:
    """Return the canonical topology entry whose cc_pair name matches.

    Task 5 of the connector canonical-collapse refactor: the re-extract
    reader reads the canonical ``topology`` block from the overlay-aware
    merged ``mapping`` — the SAME parse+split the live-sync reader uses
    (:func:`_parse_topology_entries`) — and returns the entry whose ``name``
    (the cc_pair routing key) matches ``source_name`` (the dead_letter
    routing key). Sharing the builder guarantees the entry dict carries the
    identical kind/name/config/extractor shape the sync path resolves.

    Returns ``None`` when the mapping declares no topology connectors or no
    cc_pair matches ``source_name`` — both shapes map to
    ``skipped_no_connector`` at the caller, so the caller doesn't need to
    distinguish.
    """
    entries = _parse_topology_entries(mapping)
    return next((e for e in entries if e.get("name") == source_name), None)


def resolve_collection_for_entry(entry: dict[str, Any]) -> str:
    """Return the collection name a connector entry's writes must carry.

    Single-source invariant for the ``documents.collection`` column —
    both the live-sync path (``_run_one_connector_batch``) and the
    re-extract path (``_build_reextract_components``) call this helper
    so a connector's writes always tag with its connector name.

    The connector entry's ``name`` field is the canonical source. An
    explicit ``collection`` override is honoured (operators who pre-
    declare a typed collection via topology still get that name on
    the legacy writer path), but the silent ``"default"`` fallback that
    leaked ~1M SharePoint docs into the ``default`` collection in
    production (GH #371) is gone — every entry must declare ``name``
    (already enforced by :func:`_load_connector_config_entries`).

    fix: every connector entry must have a non-empty ``name`` key in
    the operator config. next: see
    ``docs/architecture/connector-ingestion-architecture.md`` §8.
    """
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(
            "connector entry is missing the 'name' key — every entry under "
            "connectors: must declare a non-empty name string. "
            "fix: add `name: <connector>` (e.g. `name: sharepoint`) to the entry. "
            "next: see docs/architecture/connector-ingestion-architecture.md §8."
        )
    override = entry.get("collection")
    if isinstance(override, str) and override:
        return override
    return name


def _build_reextract_components(
    *,
    entry: dict[str, Any],
    db: sqlite3.Connection,
) -> tuple[Any, Any, Any, Any, Any]:
    """Wire connector + extractor + silver + chunk_writer + entity-graph sink.

    Mirrors ``_run_one_connector_batch``'s resolution shape so re-extract
    sees identical wiring to the original sync — including the
    ``documents.collection`` tagging invariant via
    :func:`resolve_collection_for_entry`.

    Task 5 canonical-collapse split: the plugin is resolved via the
    connector ``entry["kind"]`` (``ConnectorConfig.kind`` == entry-point
    name) — the SAME key the live-sync path
    (:func:`_run_one_connector_batch`) resolves on — NOT the cc_pair
    routing name. ``entry["name"]`` keys the chunk-writer collection (via
    :func:`resolve_collection_for_entry`).
    """
    from kairix.core.connectors import resolve_connector
    from kairix.core.connectors.collection_router import legacy_chunk_writer
    from kairix.core.connectors.registry import build_extractor_from_entry

    connector = resolve_connector(entry["kind"])(entry.get("config", {}))
    # Builds either a single extractor or an EscalatingExtractor depending
    # on whether the entry sets ``extractor_chain: [...]`` or ``extractor: <name>``.
    extractor = build_extractor_from_entry(entry)
    # #336 — wire SqliteDocumentsMediaWriter so re-extract writes the
    # documents_media row that the original sync would have. Without
    # this, re-extracted documents flow through but the per-doc row is
    # silently skipped, leaving F40/F70 blind to recovered docs.
    silver = build_worker_silver_processor(db)
    # GH #371 — re-extract MUST tag with the same collection the sync
    # path uses. The previous ``entry.get("collection", "default")``
    # silently leaked ~1M SharePoint docs into the ``default`` collection.
    chunk_writer = legacy_chunk_writer(db, collection=resolve_collection_for_entry(entry))
    entity_graph_sink = _SqliteEntityGraphSink(db)
    return connector, extractor, silver, chunk_writer, entity_graph_sink


# Re-extract per-row outcome bucket names. Extracted as module-level
# constants so the duplicate-string check (F17) doesn't fire on the
# multiple call/return sites and so the dispatch in _reextract_rows
# becomes a string-equality switch over named buckets rather than
# magic-string comparisons.
_BUCKET_RECOVERED = "recovered"
_BUCKET_STILL_FAILING = "still_failing"
_BUCKET_SKIPPED_NO_BRONZE = "skipped_no_bronze"
_BUCKET_SKIPPED_SOURCE_UNAVAILABLE = "skipped_source_unavailable"
_BUCKET_OK = "ok"


def _reextract_one(
    *,
    entry: Any,
    db: sqlite3.Connection,
    bronze_root: Path,
    extractor: Any,
    silver: Any,
    chunk_writer: Any,
    entity_graph_sink: Any,
    connector: Any,
    dead_letter: Any,
    dry_run: bool,
) -> str:
    """Re-extract one dead-letter row; return a bucket name for the counter.

    Buckets: 'recovered', 'still_failing', 'skipped_no_bronze',
    'skipped_source_unavailable'. Extracted from the inner loop of
    ``_reextract_rows`` to keep that function under the F16 cognitive-
    complexity threshold.
    """
    from kairix.core.protocols import BronzeRef

    row = db.execute(
        "SELECT raw_path, mime, fetched_at FROM bronze_records WHERE source_name = ? AND item_id = ?",
        (entry.source_name, entry.item_id),
    ).fetchone()
    if row is None:
        return _BUCKET_SKIPPED_NO_BRONZE
    db_raw_path = str(row[0])
    ref = BronzeRef(
        source_name=entry.source_name,
        item_id=entry.item_id,
        raw_path=db_raw_path if db_raw_path else None,
        mime=str(row[1]),
        fetched_at=str(row[2]),
    )
    raw_or_none, mime_or_none, outcome = _read_raw_for_reextract(
        ref=ref,
        connector=connector,
        db=db,
        bronze_root=bronze_root,
        item_id=entry.item_id,
    )
    if outcome != _BUCKET_OK:
        return outcome
    assert raw_or_none is not None and mime_or_none is not None
    try:
        doc = extractor.extract(raw_or_none, mime_or_none)
        silver_out = silver.process(
            ref,
            doc,
            source_uri=connector.source_link(entry.item_id),
            source_modified_at=str(row[2]),
            sensitivity=connector.sensitivity_for(entry.item_id),
        )
        chunk_writer.upsert(silver_out.chunks)
        entity_graph_sink.buffer(silver_out.entity_signals)
        dead_letter.clear(entry.source_name, entry.item_id)
        if not dry_run:
            db.commit()
        else:
            db.rollback()
        return _BUCKET_RECOVERED
    except Exception as exc:
        _record_reextract_failure(db=db, dead_letter=dead_letter, entry=entry, exc=exc, dry_run=dry_run)
        return _BUCKET_STILL_FAILING


def _record_reextract_failure(
    *,
    db: sqlite3.Connection,
    dead_letter: Any,
    entry: Any,
    exc: Exception,
    dry_run: bool,
) -> None:
    """Roll back the failed per-item txn and refresh dead-letter bookkeeping.

    GH #351 — bumps ``failure_count`` + sets ``last_attempt = now()`` +
    writes ``last_error`` so operators see fresh state after each
    reextract attempt. Without this, the row reads "3-day-old error"
    even after a brand-new reextract with a fixed extractor (#337
    SharePoint triage hit this).

    ``dry_run`` preserves the "commits nothing" contract — the row is
    NOT touched in that mode, so operators can size a recovery without
    dirtying the table.

    Extracted from :func:`_reextract_one`'s except branch to keep that
    function under the F16 cognitive-complexity threshold.
    """
    db.rollback()
    if dry_run:
        return
    try:
        dead_letter.record(entry.source_name, entry.item_id, f"reextract: {exc}")
        db.commit()
    except Exception:
        # Don't let a bookkeeping failure mask the original failure bucket;
        # operator still gets still_failing surfaced via the counter.
        db.rollback()


def _read_raw_for_reextract(
    *,
    ref: Any,
    connector: Any,
    db: sqlite3.Connection,
    bronze_root: Path,
    item_id: str,
) -> tuple[bytes | None, str | None, str]:
    """Dual-mode read for the Phase 5 re-extract loop.

    Returns ``(raw, mime, outcome)`` where outcome is one of:
    - ``"ok"`` — raw + mime are populated; caller proceeds with extract
    - ``"skipped_source_unavailable"`` — streaming-row connector.fetch raised
    - ``"still_failing"`` — filesystem-row bronze.read raised

    Extracted to keep ``_reextract_rows`` under the F16 cognitive-complexity
    threshold. The branching logic + rollback handling lives here; the
    outer loop only handles the counter bookkeeping.
    """
    if ref.raw_path is None:
        try:
            raw_artefact = connector.fetch(item_id)
            return raw_artefact.raw, raw_artefact.mime, _BUCKET_OK
        except Exception:
            db.rollback()
            return None, None, _BUCKET_SKIPPED_SOURCE_UNAVAILABLE
    try:
        raw, mime = _read_filesystem_bronze(db, bronze_root, ref)
        return raw, mime, _BUCKET_OK
    except Exception:
        db.rollback()
        return None, None, _BUCKET_STILL_FAILING


def _read_filesystem_bronze(_db: sqlite3.Connection, bronze_root: Path, ref: Any) -> tuple[bytes, str]:
    """Read a legacy on-disk bronze blob (pre-Phase-7 filesystem-mode rows).

    Phase 7 removed FilesystemBronzeStore as a writeable class, but old
    dead-letter rows on existing deploys still point at on-disk blobs.
    This helper reads them via raw filesystem I/O so Bug D re-extract
    can still recover those items. Operators who've never run a
    pre-Phase-7 build never hit this branch.
    """
    abs_path = bronze_root / ref.raw_path
    return abs_path.read_bytes(), ref.mime


def _reextract_rows(
    *,
    rows: tuple[Any, ...],
    db: sqlite3.Connection,
    bronze_root: Path,
    extractor: Any,
    silver: Any,
    chunk_writer: Any,
    entity_graph_sink: Any,
    connector: Any,
    dead_letter: Any,
    dry_run: bool,
) -> ReextractResult:
    """Inner loop of :func:`run_reextract_dead_letter` — split out so the
    outer function's setup stays under F16 cognitive complexity."""

    recovered = 0
    still_failing = 0
    skipped_no_bronze = 0
    skipped_source_unavailable = 0

    for entry in rows:
        bucket = _reextract_one(
            entry=entry,
            db=db,
            bronze_root=bronze_root,
            extractor=extractor,
            silver=silver,
            chunk_writer=chunk_writer,
            entity_graph_sink=entity_graph_sink,
            connector=connector,
            dead_letter=dead_letter,
            dry_run=dry_run,
        )
        if bucket == _BUCKET_RECOVERED:
            recovered += 1
        elif bucket == _BUCKET_STILL_FAILING:
            still_failing += 1
        elif bucket == _BUCKET_SKIPPED_NO_BRONZE:
            skipped_no_bronze += 1
        elif bucket == _BUCKET_SKIPPED_SOURCE_UNAVAILABLE:
            skipped_source_unavailable += 1
    return ReextractResult(
        recovered=recovered,
        still_failing=still_failing,
        skipped_no_bronze=skipped_no_bronze,
        skipped_no_connector=0,
        skipped_source_unavailable=skipped_source_unavailable,
    )


def _default_neo4j_drain() -> Any:
    """Worker-loop dispatch slot for the Neo4j entity-graph drain (GH #334).

    Wraps the production drain with the live SQLite DB + live Neo4j
    client; returns a
    :class:`kairix.core.curator.drain.NeoDrainResult` so the worker can
    log structured outcomes. The lazy imports keep startup fast — only
    the drain tick pays the cost of loading the graph layer.

    Failure modes:
      * Neo4j unreachable → :func:`run_neo4j_drain_tick` returns
        ``NeoDrainResult(neo4j_available=False, pushed=0)`` and the
        worker logs a single warning. The next tick retries.
      * SQLite read fails → propagates up; the worker's
        ``(Exception, SystemExit)`` discipline at the dispatch site
        keeps the loop alive.

    Tests inject a substitute via ``WorkerDeps(neo4j_drain_fn=fake)``;
    production omits and gets this default. The component-build chain
    (graph client → repo → SQLite handle → tick) lives in
    :func:`kairix.core.curator.drain.run_default_drain_tick` so the
    drain module is self-contained and worker.py stays the thin
    dispatcher. Both modules independently satisfy their per-file
    coverage floors.
    """
    from kairix.core.curator.drain import run_default_drain_tick

    return run_default_drain_tick()


def _default_wal_checkpoint() -> dict[str, int]:
    """Worker-loop dispatch slot for the periodic SQLite WAL checkpoint (R3 / #389).

    Opens the kairix index DB and runs ``PRAGMA wal_checkpoint(TRUNCATE)``
    so the WAL file can't grow unbounded (production trace showed 3.6 GB
    accumulation before manual intervention). Returns the standard SQLite
    checkpoint tuple as a dict so the worker can log structured outcomes.

    The pragma is a no-op when there's nothing to truncate; never
    corrupts. Failure modes:

      * DB locked (concurrent embed write) → ``busy=1``, ``checkpointed=0``;
        the next tick retries.
      * DB path missing → propagates ``OperationalError`` up; the worker's
        ``(Exception, SystemExit)`` discipline at the dispatch site keeps
        the loop alive.

    Tests inject a substitute via ``WorkerDeps(wal_checkpoint_fn=fake)``;
    production omits and gets this default.
    """
    import sqlite3

    from kairix.paths import db_path

    conn = sqlite3.connect(str(db_path()), timeout=30.0)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    # PRAGMA wal_checkpoint returns (busy, log_pages_in_wal, pages_checkpointed).
    busy, log_pages, checkpointed = (int(row[0]), int(row[1]), int(row[2])) if row else (0, 0, 0)
    return {"busy": busy, "log_pages": log_pages, "checkpointed": checkpointed}


def _default_deadletter_sweep() -> tuple[Any, ...]:
    """Worker-loop dispatch slot for the orphaned-source dead-letter sweep (PR-5).

    Opens the kairix index DB and runs
    :func:`kairix.core.connectors.deadletter_drain.drain_all_source_deadletters`
    over EVERY distinct ``source_name`` — including ORPHANED sources whose
    connector is no longer active, which the per-connector auto-drain never
    reaches. Returns the per-source :class:`DrainSummary` tuple so the
    worker can log a structured outcome.

    Reuses the EXISTING narrow eligibility (corrupt_zip OR known-unsupported
    MIME) and the per-source best-effort / idempotent core verbatim — this
    slot adds reach across sources, never broadened eligibility. Cheap when
    clean: an empty table enumerates to zero sources.

    Tests inject a substitute via ``WorkerDeps(deadletter_sweep_fn=fake)``;
    production omits and gets this default.
    """
    import sqlite3

    from kairix.core.connectors.deadletter_drain import drain_all_source_deadletters
    from kairix.paths import db_path

    conn = sqlite3.connect(str(db_path()), timeout=30.0)
    try:
        silver = build_worker_silver_processor(conn)
        return drain_all_source_deadletters(conn, silver=silver)
    finally:
        conn.close()


def _default_rechunk_sweep() -> Any:
    """Worker-loop dispatch slot for the re-chunk sweep tick (ADR-028 Wave F.4).

    Opens the kairix index DB and runs one bounded re-chunk sweep pass
    (:func:`kairix.core.connectors.rechunk_sweep.run_rechunk_sweep`) over up to
    ``KAIRIX_RECHUNK_SWEEP_PER_TICK_CAP`` documents from the persisted cursor.
    Returns the :class:`RechunkSweepResult` so the worker can log the outcome.

    Tests inject a substitute via ``WorkerDeps(rechunk_sweep_fn=fake)``;
    production omits and gets this default.
    """
    import sqlite3

    from kairix.core.connectors.rechunk_sweep import run_rechunk_sweep as _run_sweep_pass
    from kairix.paths import db_path, rechunk_sweep_per_tick_cap

    conn = sqlite3.connect(str(db_path()), timeout=30.0)
    try:
        return _run_sweep_pass(conn, cap=rechunk_sweep_per_tick_cap())
    finally:
        conn.close()


def _default_connector_sync() -> ConnectorSyncResult:
    """Worker-loop dispatch slot for the connector-sync maintenance task.

    Drives each configured connector through the
    :class:`~kairix.core.connectors.ConnectorPipeline` path —
    list_changes → fetch → bronze → silver → cursor.advance. The
    legacy DocumentScanner path retired with ``obsidian_connector_primary``
    (task #132 cutover).

    Production callers reach this via ``WorkerDeps.connector_sync_fn``
    default-factory.
    """
    return dispatch_connector_sync()


def _default_flag_value(name: str) -> bool:
    """Production default for connector-sync dispatchers' ``read_flag`` arg
    — delegates to :func:`kairix.core.features.flag`.

    Lifted to a module-level helper so the dispatchers' signatures can
    carry a real callable default (F6-clean) without a per-call
    ``Optional[...] = None`` shape.
    """
    from kairix.core.features import flag as _prod_flag

    return _prod_flag(name)


# PR#6 — read once per Silver construction. OFF (default) -> registry None ->
# Silver keeps its byte-identical paragraph fallback; ON -> per-type dispatch.
_CHUNKER_REGISTRY_FLAG = "chunker_registry_dispatch_enabled"
_RECHUNK_SWEEP_FLAG = "re_chunk_sweep_enabled"


def build_worker_silver_processor(
    db: sqlite3.Connection,
    *,
    read_flag: Callable[[str], bool] = _default_flag_value,
) -> DefaultSilverProcessor:
    """Construct the worker's Silver processor.

    Wires the per-type chunker registry when
    ``chunker_registry_dispatch_enabled`` is ON (default OFF -> paragraph fallback).

    Also wires the ``documents_media`` + ``document_pages`` + ``silver_source``
    writers so each processed document records its extractor/chunker identity,
    page-level text, and source markdown — the latter lets the re-chunk sweep
    (ADR-028 Wave F.4) re-chunk from the original text without re-fetching from
    the remote connector.
    """
    from kairix.core.connectors.chunker_registry import build_default_registry
    from kairix.core.connectors.silver import (
        DefaultSilverProcessor,
        SqliteDocumentPagesWriter,
        SqliteDocumentsMediaWriter,
        SqliteSilverSourceWriter,
    )

    registry = build_default_registry() if read_flag(_CHUNKER_REGISTRY_FLAG) else None
    return DefaultSilverProcessor(
        documents_media_writer=SqliteDocumentsMediaWriter(db),
        document_pages_writer=SqliteDocumentPagesWriter(db),
        silver_source_writer=SqliteSilverSourceWriter(db),
        chunker_registry=registry,
    )


def connector_enabled(
    kind: str,
    read_flag: Callable[[str], bool],
    registry: dict[str, FeatureFlag] | None = None,
) -> bool:
    """Return whether a connector ``kind`` is enabled for this tick.

    Enablement keys on connector KIND — the ``ConnectorConfig.kind`` value
    (== entry-point name == REGISTRY suffix, e.g. ``sharepoint`` →
    ``connector_sharepoint``) — NOT the cc_pair routing name. A kind whose
    ``connector_<kind>`` flag is registered runs iff ``read_flag`` resolves
    True; a flagless kind (no matching REGISTRY entry) always runs.

    ``registry`` is injectable for tests; production passes ``None`` and the
    canonical :data:`kairix.core.features.registry.REGISTRY` is resolved.

    Per Task 2+3 this predicate is landed but NOT yet wired into
    ``run_connector_sync_pipeline``'s per-entry loop — Task 4 wires it once
    entries carry ``kind``.
    """
    if registry is None:
        from kairix.core.features.registry import REGISTRY

        registry = REGISTRY
    name = f"connector_{kind}"
    return read_flag(name) if name in registry else True


def dispatch_connector_sync(
    on_branch: Callable[[], ConnectorSyncResult] = run_via_connector_pipeline,
) -> ConnectorSyncResult:
    """Dispatch the connector-sync slot via the connector pipeline.

    ``obsidian_connector_primary`` retired (task #132); the OFF/legacy
    DocumentScanner branch is gone. ``on_branch`` defaults to the
    production pipeline helper and remains injectable so integration
    tests can pass tmp_path-rooted variants when they need to assert
    against the resulting ConnectorSyncResult counters.
    """
    return on_branch()


# ---------------------------------------------------------------------------
# KFEAT-021 Phase 1 — maintenance scheduler wiring (behind the
# ``maintenance_loop`` feature flag). When the flag is OFF the
# :func:`maybe_run_maintenance_loop_tick` helper is a structural no-op
# (no DB open, no scheduler instantiated) — bit-for-bit pre-KFEAT-021
# behaviour is preserved.
# ---------------------------------------------------------------------------


@dataclass
class MaintenanceLoopDeps:
    """Injectable dependencies for :func:`run_maintenance_loop_tick`.

    F6-clean: every field has a ``default_factory`` so production
    callers omit the Deps and get the real boundary calls; tests pass
    fakes to drive the OFF / ON / failure branches without monkey-
    patching kairix internals.

    Fields:
      * ``flag_reader`` — returns the effective value of the named
        feature flag. Default :func:`_default_flag_value`. Tests pass
        a lambda returning a deterministic bool to pin the gate.
      * ``db_factory`` — opens the SQLite connection the scheduler
        prunes through; default :func:`_open_db_default`.
      * ``retention_days_resolver`` — returns the retention window in
        days; default :func:`maintenance_retention_days` (reads
        ``KAIRIX_MAINTENANCE_RETENTION_DAYS``).
      * ``scheduler_factory`` — builds a
        :class:`~kairix.core.maintenance.MaintenanceScheduler` for the
        given connection + retention window. Default constructs the
        production scheduler with default Deps; tests pass a factory
        that returns a Fake with pre-canned tick results.
      * ``prune_orphans_per_tick_cap`` — per-tick row cap forwarded to
        :class:`MaintenanceScheduler` so its orphan scan stays bounded
        on production-scale DBs. Operators can tune via the worker
        deps wiring; default 1000 matches the scheduler default and
        keeps one tick under 5s on a 2M-row table.
    """

    flag_reader: Callable[[str], bool] = field(default_factory=lambda: _default_flag_value)
    db_factory: Callable[[], sqlite3.Connection] = field(default_factory=lambda: _open_db_default)
    retention_days_resolver: Callable[[], int] = field(default_factory=lambda: maintenance_retention_days)
    scheduler_factory: Callable[[sqlite3.Connection, int, int], Any] = field(
        default_factory=lambda: _default_scheduler_factory
    )
    prune_orphans_per_tick_cap: int = 1000


def _default_scheduler_factory(db: sqlite3.Connection, retention_days: int, prune_orphans_per_tick_cap: int) -> Any:
    """Production seam — build a :class:`MaintenanceScheduler` with prod Deps.

    Lazy import keeps the worker importable on hosts that haven't yet
    landed the maintenance module (defensive — Phase 1 is forward-only
    but we keep the boundary tidy).
    """
    from kairix.core.maintenance import MaintenanceScheduler

    return MaintenanceScheduler(
        db,
        retention_days=retention_days,
        prune_orphans_per_tick_cap=prune_orphans_per_tick_cap,
    )


def run_maintenance_loop_tick(deps: MaintenanceLoopDeps | None = None) -> Any:
    """Run one ``MaintenanceScheduler.tick`` (flag-gated).

    Returns the :class:`MaintenanceTickResult` envelope when the flag
    is ON, or ``None`` when the flag is OFF (structural no-op). The
    structured ``maintenance_tick_completed`` log line carries the
    same envelope fields so log-only consumers see the cadence
    without parsing the return value.

    Per the KFEAT-021 brief: when the flag is OFF this MUST be a
    bit-for-bit no-op so flipping the flag in / out is reversible.

    ``deps`` is the F6-clean injection seam — production callers omit
    it; the BDD + integration tests pass a :class:`MaintenanceLoopDeps`
    with the flag pinned through a :class:`FakeFeatureFlagResolver` so
    each branch is exercised against the real scheduler.
    """
    deps = deps if deps is not None else MaintenanceLoopDeps()
    if not deps.flag_reader("maintenance_loop"):
        # Flag OFF — log nothing (avoid spamming on every loop iter)
        # and return None so the worker treats the tick as skipped.
        return None

    retention = deps.retention_days_resolver()
    db = deps.db_factory()
    try:
        from kairix.core.db.schema import create_schema

        create_schema(db)
        scheduler = deps.scheduler_factory(db, retention, deps.prune_orphans_per_tick_cap)
        result = scheduler.tick(db)
    except Exception as exc:
        logger.warning("worker: maintenance tick raised — %s", exc)
        return None
    finally:
        db.close()
    return result


def maybe_run_entity_summary_projector_tick(
    *,
    deps: Any | None,
    transition: Callable[[WorkerPhase], None],
    state: WorkerState,
    state_path: Path,
    write_state_fn: Callable[[WorkerState, Path], None],
    now: float,
    last_tick_at: float,
    interval_seconds: int,
) -> float:
    """Run an entity-summary projector tick when due; return the new ``last_tick_at``.

    ADR-036 §Worker. Mirrors :func:`maybe_run_maintenance_loop_tick`:

      * OUTER gate — the ``entity_summary_indexing_enabled`` feature
        flag check happens inside
        :func:`run_entity_summary_projector_tick`. When OFF, the
        dispatcher returns ``None`` and this helper leaves
        ``last_tick_at`` unchanged so the OFF→ON flip fires
        immediately rather than waiting an interval.
      * INNER gate — cadence ``is_tick_due(now, last_tick_at,
        interval_seconds)``.

    On a productive tick, records the ``EntitySummaryProjectionResult``
    counters onto :class:`WorkerState` so ``kairix doctor`` /
    ``kairix features status`` can surface them.
    """
    from kairix.core.maintenance import is_tick_due
    from kairix.knowledge.entities.summary_projector import (
        run_entity_summary_projector_tick,
    )

    if not is_tick_due(now, last_tick_at, interval_seconds):
        return last_tick_at

    transition(WorkerPhase.MAINTENANCE)
    result = run_entity_summary_projector_tick(deps)
    transition(WorkerPhase.IDLE)
    if result is None:
        # Flag OFF → no tick. Don't advance the timestamp so the next
        # loop iter re-checks (OFF→ON should fire immediately).
        return last_tick_at

    state.last_entity_summary_tick_at = now
    state.last_entity_summary_projected = int(getattr(result, "projected", 0))
    state.last_entity_summary_updated = int(getattr(result, "updated", 0))
    state.last_entity_summary_skipped = int(getattr(result, "skipped", 0))
    state.last_entity_summary_failed = int(getattr(result, "failed", 0))
    write_state_fn(state, state_path)
    return now


def maybe_build_capability_corpus_at_boot(
    *,
    read_flag: Callable[[str], bool] = _default_flag_value,
    db_factory: Callable[[], sqlite3.Connection] = _open_db_default,
    corpus_deps: Any | None = None,
) -> Any | None:
    """Build the capability-recommender corpus at worker boot, flag-gated.

    Spec A §3.5 — when the ``recommender`` flag is ON, write kairix's own
    capability catalogue into the ``capabilities`` collection so the corpus
    is fresh on boot. The OUTER gate is the flag check; when OFF this is a
    structural no-op (the DB is never opened, the builder never runs) and
    returns ``None`` — installing the recommender code is a no-op for
    operators.

    Failure-isolated: ``build_capability_corpus`` never raises (it surfaces
    failures via ``CapabilityCorpusResult.error``); this helper logs the
    outcome and returns the result. A boot-time corpus build failure
    degrades — the worker continues; the recommender simply has an empty or
    stale corpus until the next build.

    ``read_flag`` / ``db_factory`` / ``corpus_deps`` are DI seams — tests
    pin the flag via :class:`FakeFeatureFlagResolver` and pass a tmp
    ``db_factory`` + a BM25-only ``CapabilityCorpusDeps`` to drive both
    branches without env vars or a provider (F1/F2-clean).
    """
    if not read_flag("recommender"):
        logger.info("worker: capability corpus build skipped (recommender flag OFF)")
        return None

    from kairix.core.db.schema import create_schema
    from kairix.knowledge.capabilities.builder import build_capability_corpus

    db = db_factory()
    try:
        create_schema(db)
        result = build_capability_corpus(db, deps=corpus_deps)
        db.commit()
    finally:
        db.close()

    if result.error:
        logger.warning("worker: capability corpus build degraded — %s", result.error)
    else:
        logger.info(
            "worker: capability corpus built — written=%d embedded=%d",
            result.written,
            result.embedded,
        )
    return result


def maybe_run_maintenance_loop_tick(
    *,
    deps: MaintenanceLoopDeps | None,
    transition: Callable[[WorkerPhase], None],
    state: WorkerState,
    state_path: Path,
    write_state_fn: Callable[[WorkerState, Path], None],
    now: float,
    last_tick_at: float,
    interval_seconds: int,
) -> float:
    """Run a maintenance tick when due; persist state; return the new ``last_tick_at``.

    When the flag is OFF, this is a structural no-op — the scheduler
    is never instantiated and the DB is never opened (the flag check
    inside :func:`run_maintenance_loop_tick` short-circuits). The
    ``last_tick_at`` value flows back unchanged so the worker loop's
    next-due calculation is unaffected.

    Cadence: a tick is due when ``now - last_tick_at >= interval`` OR
    when ``last_tick_at == 0`` (first cycle post-flag-flip / restart).
    The flag check is the OUTER gate; the cadence is the inner gate.
    """
    from kairix.core.maintenance import is_tick_due

    if not is_tick_due(now, last_tick_at, interval_seconds):
        return last_tick_at

    transition(WorkerPhase.MAINTENANCE)
    result = run_maintenance_loop_tick(deps)
    transition(WorkerPhase.IDLE)
    if result is None:
        # Flag OFF — no tick fired. Don't advance the timestamp so the
        # next loop iter re-checks (the OFF→ON flip should fire
        # immediately rather than wait an interval).
        return last_tick_at

    state.last_maintenance_tick_at = now
    state.last_maintenance_orphans_pruned = int(getattr(result, "orphans_pruned", 0))
    state.last_maintenance_pruned_table_size = int(getattr(result, "pruned_table_size", 0))
    state.last_maintenance_elapsed_ms = int(getattr(result, "elapsed_ms", 0))
    write_state_fn(state, state_path)
    return now


@dataclass
class WorkerDeps:
    """Injectable dependencies for the worker loop and its task helpers.

    Replaces the F6-violating ``embed_fn=None`` / ``entity_seed_fn=None`` /
    ``health_check_fn=None`` / ``wikilinks_fn=None`` / ``sleep_fn=None``
    test-only kwargs with a typed dataclass. Production code calls
    ``main()`` without ``deps`` and the default factory wires the real
    task callables. Tests construct
    ``WorkerDeps(embed=fake, sleep=lambda _s: None)`` and pass it through.

    Each callable field is non-Optional with a ``default_factory`` (per
    CLAUDE.md F6 guidance: avoid the ``Optional[Callable] + post-init``
    pattern that "just landed a mypy bug") so mypy sees the production
    callable directly — no ``assert deps.x is not None`` ladder is
    needed inside the worker loop.
    """

    embed: Callable[[], Any] = field(default_factory=lambda: _default_embed)
    entity_seed: Callable[[], None] = field(default_factory=lambda: _default_entity_seed)
    health_check: Callable[[], list[Any]] = field(default_factory=lambda: _default_health_check)
    wikilinks: Callable[[], None] = field(default_factory=lambda: _default_wikilinks_inject)
    # SC-6 — connector-framework seam (Wave 1 wires; Wave 2 implements).
    # Same F6-clean default_factory pattern as the four task callables
    # above. Tests pass a Fake; production omits and gets the
    # NotImplementedError-raising default until Wave 2 swaps it for the
    # real ``kairix.core.connectors`` dispatcher.
    connector_sync_fn: Callable[[], ConnectorSyncResult] = field(default_factory=ConnectorSyncRuntime)
    # GH #334 — Neo4j entity-graph drain dispatch slot. Same F6-clean
    # default_factory shape as ``connector_sync_fn``. Tests pass a
    # Fake; production omits and gets ``_default_neo4j_drain`` which
    # wires the live SQLite + Neo4j client. The return type is
    # ``NeoDrainResult`` (frozen dataclass) — typed as ``Any`` here so
    # the import stays inside the function body (lazy load of the
    # graph layer keeps worker boot fast).
    neo4j_drain_fn: Callable[[], Any] = field(default_factory=lambda: _default_neo4j_drain)
    # R3 (#389) — SQLite WAL checkpoint dispatch slot. Same F6-clean
    # default_factory shape as ``neo4j_drain_fn``. Tests pass a Fake;
    # production omits and gets ``_default_wal_checkpoint`` which opens
    # the kairix index DB and runs PRAGMA wal_checkpoint(TRUNCATE).
    wal_checkpoint_fn: Callable[[], Any] = field(default_factory=lambda: _default_wal_checkpoint)
    # PR-5 — orphaned-source dead-letter sweep dispatch slot. Same F6-clean
    # default_factory shape as ``wal_checkpoint_fn``. Tests pass a Fake;
    # production omits and gets ``_default_deadletter_sweep`` which opens
    # the kairix index DB and drains every distinct source's permanently-
    # unprocessable backlog — orphaned (no-longer-active) sources included.
    # Returns the per-source DrainSummary tuple (typed Any so the import
    # stays in the function body, keeping worker boot lazy).
    deadletter_sweep_fn: Callable[[], Any] = field(default_factory=lambda: _default_deadletter_sweep)
    # ADR-028 Wave F.4 — re-chunk sweep dispatch slot. Same F6-clean shape.
    # Tests pass a Fake; production omits and gets ``_default_rechunk_sweep``
    # which opens the index DB and runs one bounded re-chunk pass. Returns the
    # RechunkSweepResult (typed Any so the import stays in the function body).
    rechunk_sweep_fn: Callable[[], Any] = field(default_factory=lambda: _default_rechunk_sweep)
    # ADR-028 Wave F.4 — feature-flag resolver seam for the re-chunk sweep gate.
    # Default reads the live registry; tests inject a stub. Mirrors the
    # flag_reader on MaintenanceLoopDeps / ConnectorSyncDeps.
    flag_reader: Callable[[str], bool] = field(default_factory=lambda: _default_flag_value)
    sleep: Callable[[float], None] = field(default_factory=lambda: time.sleep)
    # #224 phase 4-5 combined — observable state + pause flag.
    # ``state`` is the in-memory dataclass the loop mutates on phase changes.
    # ``state`` defaults to None so the boot path in main() can read prior
    # state off disk first (restart_count survives container restarts).
    # ``state_path`` is where it gets persisted via ``write_state_fn`` so
    # operators (and ``kairix worker status``) can read it.
    # ``read_state_fn`` is the read-side test seam mirroring ``write_state_fn``.
    # ``pause_flag_path`` is the touch-file the operator-facing
    # ``kairix worker pause/resume`` toggles; the loop polls it each
    # iteration. All are F6-clean (typed, default_factory).
    state: WorkerState = field(default_factory=WorkerState)
    state_path: Path = field(default_factory=worker_state_path)
    write_state_fn: Callable[[WorkerState, Path], None] = field(default_factory=lambda: write_state)
    read_state_fn: Callable[[Path], WorkerState | None] = field(default_factory=lambda: read_state)
    pause_flag_path: Path = field(default_factory=worker_pause_flag_path)
    # KFEAT-021 Phase 1 — maintenance-loop tick deps. F6-clean: a real
    # MaintenanceLoopDeps default; tests pass a substitute with the flag
    # pinned via FakeFeatureFlagResolver so the flag-OFF / flag-ON
    # branches are exercised against the real scheduler.
    maintenance_loop_deps: MaintenanceLoopDeps = field(default_factory=MaintenanceLoopDeps)
    # ADR-036 — entity-summary projector tick deps. F6-clean: a real
    # EntitySummaryProjectorDeps default; tests pass a substitute with
    # the entity_summary_indexing_enabled flag pinned via
    # FakeFeatureFlagResolver so OFF / ON branches are exercised
    # against a real projector + scripted Neo4j fake.
    entity_summary_projector_deps: Any = field(default_factory=lambda: _default_entity_summary_projector_deps())


def _default_entity_summary_projector_deps() -> Any:
    """Production default — lazy-imports to keep worker.py's top-level
    import graph thin. The dispatcher itself reads the live feature
    flag + builds the real projector via the canonical placeholder
    until Slice C+ adds a live-Neo4j builder.
    """
    from kairix.knowledge.entities.summary_projector import EntitySummaryProjectorDeps

    return EntitySummaryProjectorDeps()


def entity_summary_projector_interval_seconds() -> int:
    """Resolve the entity-summary projector tick cadence in seconds.

    Reads ``KAIRIX_ENTITY_SUMMARY_PROJECTOR_INTERVAL_S`` from the
    paths boundary (F4-clean); defaults to 60s — at the canonical
    per_tick_max_items=200, a 7,461-entity backlog clears in ~38
    cycles (~38 min on the default cadence).
    """
    from kairix.paths import read_int_env

    return read_int_env("KAIRIX_ENTITY_SUMMARY_PROJECTOR_INTERVAL_S", default=60)


@dataclass(frozen=True)
class EmbedRunOutcome:
    """Structured outcome of one embed pass — used by ``main()`` to update
    the persisted ``WorkerState`` counters.

    Field semantics mirror ``EmbedPipelineResult`` but with safe-default
    integers so a legacy stub returning a sparse object still feeds the
    state counters cleanly.
    """

    did_work: bool
    embedded: int = 0
    failed: int = 0
    recall_passed: bool | None = None


def _log_embed_complete(embedded: Any, failed: Any, recall_score: Any) -> None:
    """Emit the standard 'embed complete' info line with recall as percentage or n/a."""
    recall_str = f"{recall_score:.0%}" if isinstance(recall_score, float) else "n/a"
    logger.info("worker: embed complete — embedded=%s failed=%s recall=%s", embedded, failed, recall_str)


def _log_embed_warnings(failed: Any, recall_passed: Any, recall_alert: Any, diagnostics: list[Any]) -> None:
    """Emit failure / recall-gate / diagnostic warnings from an embed result."""
    if isinstance(failed, int) and failed > 0:
        logger.warning("worker: %d chunks failed during embed", failed)
    if recall_passed is False:
        logger.warning(
            "worker: recall gate alert — %s",
            recall_alert or "search quality degraded; see kairix onboard check",
        )
    for diag in diagnostics:
        logger.warning("worker: %s", diag)


def _outcome_from_result(result: Any) -> EmbedRunOutcome:
    """Map an ``EmbedPipelineResult``-shaped object into a typed ``EmbedRunOutcome``."""
    embedded = getattr(result, "embedded", None)
    failed = getattr(result, "failed", None)
    recall_passed = getattr(result, "recall_passed", None)
    diagnostics = getattr(result, "diagnostics", None) or []
    _log_embed_complete(embedded, failed, getattr(result, "recall_score", None))
    _log_embed_warnings(failed, recall_passed, getattr(result, "recall_alert", None), diagnostics)
    did_work = (isinstance(embedded, int) and embedded > 0) or (isinstance(failed, int) and failed > 0)
    return EmbedRunOutcome(
        did_work=did_work,
        embedded=embedded if isinstance(embedded, int) else 0,
        failed=failed if isinstance(failed, int) else 0,
        recall_passed=recall_passed if isinstance(recall_passed, bool) else None,
    )


def run_embed_with_outcome(deps: WorkerDeps | None = None) -> EmbedRunOutcome:
    """Run incremental embed and return a structured outcome.

    Same try/except/logging discipline as ``run_embed`` (see its
    docstring for the "never crash the worker" rationale); this variant
    additionally surfaces the counters main() folds into ``WorkerState``.
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        logger.info("worker: starting incremental embed")
        result = deps.embed()
        if result is None:
            logger.info("worker: embed complete")
            return EmbedRunOutcome(did_work=False)
        return _outcome_from_result(result)
    except (Exception, SystemExit) as exc:
        logger.warning("worker: embed pipeline raised — %s", exc)
        return EmbedRunOutcome(did_work=False)


def run_embed(deps: WorkerDeps | None = None) -> bool:
    """Run incremental embed — indexes new and changed documents.

    Returns ``True`` when the embed run did real work (embedded > 0 or
    failed > 0), ``False`` when it was a no-op. The main loop uses this
    signal to apply idle-backoff per #224.

    The worker treats every outcome of the embed pipeline as
    non-fatal: failed chunks, recall-gate alerts, and unexpected
    exceptions are all logged and the worker continues to the next
    interval. This decoupling is deliberate — the worker's job is to
    KEEP RUNNING on a schedule; the embed use case's job is to do the
    work and report what happened.

    The embed use case returns an ``EmbedPipelineResult`` dataclass that
    the worker inspects and logs — it must NOT call a code path that uses
    ``sys.exit()`` (e.g. the embed CLI) because ``SystemExit`` is not
    caught by ``except Exception`` and any gate alert would kill the
    worker process.

    ``deps.embed`` is the injection seam: tests pass a callable returning
    either the result dataclass or None (legacy). Production passes
    ``_default_embed`` which runs the use case.
    """
    return run_embed_with_outcome(deps).did_work


def compute_embed_interval(base: int, noop_streak: int) -> int:
    """Apply exponential idle-backoff after a streak of no-op embed runs.

    No backoff until ``EMBED_BACKOFF_NOOP_THRESHOLD`` consecutive no-ops.
    After that, each additional no-op doubles the interval, capped at
    ``EMBED_BACKOFF_MAX_INTERVAL`` (4 hours). The exponent is
    ``noop_streak - threshold + 1`` so the FIRST backoff hop is 2x, not 1x.

    Implements #224's "Add backoff/jitter when scans find no new or
    changed work" acceptance criterion.
    """
    if noop_streak <= EMBED_BACKOFF_NOOP_THRESHOLD:
        return base
    exponent = noop_streak - EMBED_BACKOFF_NOOP_THRESHOLD
    return int(min(base * (2**exponent), EMBED_BACKOFF_MAX_INTERVAL))


def run_entity_seed(deps: WorkerDeps | None = None) -> None:
    """Run entity relationship seeding from document store structure.

    Treats every outcome as non-fatal: the underlying store-crawl CLI
    (``kairix.knowledge.store.cli``) calls ``sys.exit(0)`` on success
    and ``sys.exit(1)`` on error. Catch ``(Exception, SystemExit)`` at
    every CLI boundary so a "successful" ``sys.exit(0)`` from the
    callee can't terminate the worker process. Same discipline as
    ``run_embed`` and ``run_wikilinks_inject``.

    Args:
        deps: Injectable worker dependencies. Tests construct
              ``WorkerDeps(entity_seed=fake)``; production omits the
              kwarg and the default factory wires the real store crawl
              CLI entry point.
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        logger.info("worker: starting entity seed")
        deps.entity_seed()
        logger.info("worker: entity seed complete")
    except (Exception, SystemExit) as exc:
        logger.warning("worker: entity seed raised — %s", exc)


def run_wikilinks_inject(deps: WorkerDeps | None = None) -> None:
    """Inject ``[[wikilinks]]`` on first mention into agent-written documents.

    Closes #100 — the host cron's nightly ``kairix wikilinks inject
    --changed`` was lost in the Docker migration. The worker now runs
    it on the same cadence as embed (hourly) so new agent-written notes
    get linked to known entities.

    Treats every outcome as non-fatal: the wikilinks CLI may
    ``sys.exit(1)`` when entities aren't loaded yet (pre-first-seed
    bootstrapping), and that must NOT terminate the worker. Same
    ``(Exception, SystemExit)`` discipline as ``run_embed``.

    ``deps.wikilinks`` is the injection seam tests use; production
    falls through to ``_default_wikilinks_inject``.
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        logger.info("worker: starting wikilinks inject")
        deps.wikilinks()
        logger.info("worker: wikilinks inject complete")
    except (Exception, SystemExit) as exc:
        logger.warning("worker: wikilinks inject raised — %s", exc)


def run_health_check(deps: WorkerDeps | None = None) -> None:
    """Log a health check.

    Treats every outcome as non-fatal — including ``SystemExit`` — for
    the same reason as ``run_entity_seed``: a maintenance helper that
    calls ``sys.exit`` must not terminate the worker process. Catch
    ``(Exception, SystemExit)`` at every CLI boundary.

    Args:
        deps: Injectable worker dependencies. Tests construct
              ``WorkerDeps(health_check=fake)``; production omits the
              kwarg and the default factory wires ``run_all_checks``.
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        results = deps.health_check()
        passed = sum(1 for r in results if r.ok)
        total = len(results)
        logger.info("worker: health check %d/%d passed", passed, total)
    except (Exception, SystemExit) as exc:
        logger.warning("worker: health check raised — %s", exc)


def run_connector_sync(deps: WorkerDeps | None = None) -> ConnectorSyncResult | None:
    """Drive one connector-framework sync tick (SC-6 seam).

    Wave 1 wires this as a no-op-friendly dispatch slot: the default
    ``deps.connector_sync_fn`` raises ``NotImplementedError`` and we
    catch it here so a pre-Wave-2 deploy does not crash the worker.
    Wave 2 plugs in the real ``kairix.core.connectors`` dispatcher and
    the same call path becomes the production sync surface (per F37
    this function MUST NOT import change-detection libraries directly —
    those live under ``kairix/connectors/<name>/`` and are reached via
    ``kairix/core/connectors/``).

    Treats every other outcome as non-fatal — same ``(Exception, SystemExit)``
    discipline as the other ``run_*`` helpers. A failing connector
    must not bring the worker process down; failures are logged and
    surfaced via the structured ``ConnectorSyncResult`` on the next
    successful tick.

    SYNC-OBS — returns the :class:`ConnectorSyncResult` on a successful
    tick (``None`` when the slot was a no-op / raised) so
    :func:`maybe_run_connector_sync_tick` can fold the counters into
    ``WorkerState``. The return value is additive: the previous callers
    ignored it and still may.

    Args:
        deps: Injectable worker dependencies. Tests construct
              ``WorkerDeps(connector_sync_fn=fake)``; production omits
              the kwarg and the default factory wires
              ``_default_connector_sync`` (Wave-2-implemented).
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        logger.info("worker: starting connector sync")
        result = deps.connector_sync_fn()
        # SYNC-OBS — the aggregate one-liner now carries the quiet-vs-dead
        # rollup so an operator can see "polled N connectors, M quiet" on a
        # zero-doc tick instead of an indistinguishable "synced=0".
        logger.info(
            "worker: connector sync complete — connectors_polled=%d synced=%d quiet=%d "
            "failed=%d dead_letter_added=%d poisoned_skipped=%d skipped_low_disk=%d",
            result.connectors_polled,
            result.synced,
            result.quiet,
            result.failed,
            result.dead_letter_added,
            result.poisoned_skipped,
            result.skipped_low_disk,
        )
        return result
    except NotImplementedError:
        # Wave 1: the default raises this. The slot is wired but the
        # body is not yet implemented. Log once-per-tick so operators
        # can see the worker reached the dispatch slot without it
        # crashing the loop. Wave 2 removes the default raise and this
        # branch becomes dead-but-harmless.
        logger.warning("worker: connector sync not yet implemented (Wave 2)")
    except (Exception, SystemExit) as exc:
        logger.warning("worker: connector sync raised — %s", exc)
    return None


def run_neo4j_drain(deps: WorkerDeps | None = None) -> None:
    """GH #334 — drive one Neo4j entity-graph drain tick.

    Invokes ``deps.neo4j_drain_fn`` (default
    :func:`_default_neo4j_drain`) and logs the structured
    :class:`~kairix.core.curator.drain.NeoDrainResult`. Mirrors the
    ``(Exception, SystemExit)`` discipline of every other worker
    maintenance helper — failures inside the drain must not bring the
    worker process down. A graph outage shows up as
    ``neo4j_available=false`` in the result envelope and the next tick
    retries.

    Tests construct ``WorkerDeps(neo4j_drain_fn=fake)``; production
    omits the kwarg and the default factory wires
    :func:`_default_neo4j_drain`.
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        logger.info("worker: starting neo4j drain")
        result = deps.neo4j_drain_fn()
        if not getattr(result, "neo4j_available", True):
            logger.warning("worker: neo4j drain skipped — backend unavailable; will retry next tick")
            return
        logger.info(
            "worker: neo4j drain complete — pushed=%d failed=%d skipped_relationships=%d elapsed_ms=%d",
            getattr(result, "pushed", 0),
            getattr(result, "failed", 0),
            getattr(result, "skipped_relationships", 0),
            getattr(result, "elapsed_ms", 0),
        )
    except (Exception, SystemExit) as exc:
        logger.warning("worker: neo4j drain raised — %s", exc)


def run_wal_checkpoint(deps: WorkerDeps | None = None) -> None:
    """R3 (#389) — drive one SQLite WAL checkpoint tick.

    Invokes ``deps.wal_checkpoint_fn`` (default
    :func:`_default_wal_checkpoint`) and logs the structured result
    dict. Mirrors the ``(Exception, SystemExit)`` discipline of every
    other worker maintenance helper — failures inside the checkpoint
    must not bring the worker process down. A locked DB (concurrent
    embed writer) shows up as ``busy=1`` in the log and the next tick
    retries.

    Tests construct ``WorkerDeps(wal_checkpoint_fn=fake)``; production
    omits the kwarg and the default factory wires
    :func:`_default_wal_checkpoint`.
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        logger.info("worker: starting wal checkpoint")
        result = deps.wal_checkpoint_fn()
        logger.info(
            "worker: wal checkpoint complete — busy=%d log_pages=%d checkpointed=%d",
            int(result.get("busy", 0)) if isinstance(result, dict) else 0,
            int(result.get("log_pages", 0)) if isinstance(result, dict) else 0,
            int(result.get("checkpointed", 0)) if isinstance(result, dict) else 0,
        )
    except (Exception, SystemExit) as exc:
        logger.warning("worker: wal checkpoint raised — %s", exc)


def run_deadletter_sweep(deps: WorkerDeps | None = None) -> None:
    """PR-5 — drive one orphaned-source dead-letter sweep tick.

    Invokes ``deps.deadletter_sweep_fn`` (default
    :func:`_default_deadletter_sweep`) which drains the permanently-
    unprocessable backlog for EVERY distinct source — including ORPHANED
    sources whose connector is no longer active, the gap the
    per-connector auto-drain leaves open. Logs an aggregate
    sources/drained line from the returned :class:`DrainSummary` tuple.

    Mirrors the ``(Exception, SystemExit)`` discipline of every other
    worker maintenance helper — a sweep failure must never bring the
    worker process down. The per-source core is itself best-effort +
    idempotent, so a re-run is always safe.

    Tests construct ``WorkerDeps(deadletter_sweep_fn=fake)``; production
    omits the kwarg and the default factory wires
    :func:`_default_deadletter_sweep`.
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        summaries = deps.deadletter_sweep_fn()
        rows = tuple(summaries) if summaries is not None else ()
        drained = sum(int(getattr(s, "drained", 0)) for s in rows)
        if drained:
            logger.info("worker: deadletter sweep complete — sources=%d drained=%d", len(rows), drained)
    except (Exception, SystemExit) as exc:
        logger.warning("worker: deadletter sweep raised — %s", exc)


def run_rechunk_sweep_tick(deps: WorkerDeps | None = None) -> None:
    """ADR-028 Wave F.4 — drive one bounded re-chunk sweep tick.

    No-op unless BOTH ``re_chunk_sweep_enabled`` AND
    ``chunker_registry_dispatch_enabled`` are ON: the sweep converges documents
    to the registry chunker versions, so running it while ingest still uses the
    legacy chunker would churn. Invokes ``deps.rechunk_sweep_fn`` (default
    :func:`_default_rechunk_sweep`) and logs the per-tick outcome. Mirrors the
    ``(Exception, SystemExit)`` discipline of every other maintenance helper —
    a sweep failure must never bring the worker process down (the per-document
    core is best-effort + idempotent, so a re-run is always safe).
    """
    deps = deps if deps is not None else WorkerDeps()
    if not deps.flag_reader(_RECHUNK_SWEEP_FLAG):
        return
    if not deps.flag_reader(_CHUNKER_REGISTRY_FLAG):
        logger.info("worker: re-chunk sweep skipped — chunker_registry_dispatch_enabled is OFF")
        return
    try:
        result = deps.rechunk_sweep_fn()
        # ``result`` is the RechunkSweepResult from _default_rechunk_sweep (typed
        # Any via the WorkerDeps seam); only log when something was re-chunked.
        if result is not None and result.rechunked:
            logger.info(
                "worker: re-chunk sweep — scanned=%d stale=%d rechunked=%d skipped_paged=%d failed=%d",
                result.scanned,
                result.stale,
                result.rechunked,
                result.skipped_paged,
                result.failed,
            )
    except (Exception, SystemExit) as exc:
        logger.warning("worker: re-chunk sweep raised — %s", exc)


@dataclass
class _Schedule:
    """Worker task interval config — bundles the cadence ints.

    Scalar config (not a test-injection seam); main() builds this once
    from kwargs + module defaults so the inner loop helpers can pass a
    single value around rather than discrete ``_embed_interval`` ints.
    SC-6 added ``connector_sync`` alongside the four maintenance cadences;
    GH #334 added ``neo4j_drain`` for the Curator-coupling boundary.
    """

    embed: int
    entity: int
    health: int
    wikilinks: int
    connector_sync: int
    neo4j_drain: int
    wal_checkpoint: int
    deadletter_sweep: int
    rechunk_sweep: int


def _resolve_schedule(
    embed_interval: int | None,
    entity_seed_interval: int | None,
    health_check_interval: int | None,
    wikilinks_interval: int | None,
    connector_sync_interval: int | None,
    neo4j_drain_interval: int | None = None,
    wal_checkpoint_interval: int | None = None,
    deadletter_sweep_interval: int | None = None,
    rechunk_sweep_interval: int | None = None,
) -> _Schedule:
    """Fold kwargs + module defaults into a single ``_Schedule``."""
    return _Schedule(
        embed=embed_interval if embed_interval is not None else EMBED_INTERVAL,
        entity=entity_seed_interval if entity_seed_interval is not None else ENTITY_SEED_INTERVAL,
        health=health_check_interval if health_check_interval is not None else HEALTH_CHECK_INTERVAL,
        wikilinks=wikilinks_interval if wikilinks_interval is not None else WIKILINKS_INTERVAL,
        connector_sync=connector_sync_interval if connector_sync_interval is not None else CONNECTOR_SYNC_INTERVAL,
        neo4j_drain=neo4j_drain_interval if neo4j_drain_interval is not None else NEO4J_DRAIN_INTERVAL,
        wal_checkpoint=(wal_checkpoint_interval if wal_checkpoint_interval is not None else WAL_CHECKPOINT_INTERVAL),
        deadletter_sweep=(
            deadletter_sweep_interval if deadletter_sweep_interval is not None else DEADLETTER_SWEEP_INTERVAL
        ),
        rechunk_sweep=(rechunk_sweep_interval if rechunk_sweep_interval is not None else RECHUNK_SWEEP_INTERVAL),
    )


@dataclass
class PreflightDeps:
    """Injectable dependencies for :func:`_run_preflight_at_boot`.

    F6-clean: each field carries a ``default_factory`` so production
    callers construct ``PreflightDeps()`` and get the real boundary
    functions; tests pass a ``PreflightDeps(db_factory=fake,
    strict_fn=lambda: True)`` rooted at tmp_path to drive the boot
    audit against a sandboxed DB. Mirrors the discipline established
    by :class:`WorkerDeps` / :class:`ConnectorSyncDeps`.

    Fields:
      * ``db_factory`` — opens the SQLite connection preflight should
        audit; default :func:`kairix.core.db.open_db`.
      * ``strict_fn`` — returns True when boot should abort on any
        error-severity gap; default :func:`kairix.paths.preflight_strict`
        (reads ``KAIRIX_PREFLIGHT_STRICT``).
    """

    db_factory: Callable[[], sqlite3.Connection] = field(default_factory=lambda: _open_db_default)
    strict_fn: Callable[[], bool] = field(default_factory=lambda: preflight_strict)


def _run_preflight_at_boot(deps: PreflightDeps | None = None) -> bool:
    """Run the persistence-integrity audit before the first embed cycle.

    Returns True iff boot should continue. Logs the report at INFO when
    healthy, WARNING-per-gap when not. In strict mode
    (``KAIRIX_PREFLIGHT_STRICT=1``) returns False on any error-severity
    gap so the worker exits non-zero; otherwise always returns True so
    slightly-degraded boots surface as warnings instead of crashlooping.

    ``deps`` is the F6-clean injection seam: production callers omit
    ``deps`` and the default factory wires real boundary calls; tests
    pass a :class:`PreflightDeps` rooted at tmp_path so the boot path
    never touches the dev's real index.
    """
    from kairix.core.db.integrity import check_integrity

    deps = deps if deps is not None else PreflightDeps()

    try:
        db = deps.db_factory()
    except Exception as exc:  # pragma: no cover - boundary
        logger.warning("worker: preflight could not open db — %s", exc)
        return True

    try:
        report = check_integrity(db)
    except Exception as exc:
        logger.warning("worker: preflight integrity check raised — %s", exc)
        return True
    finally:
        db.close()

    if report.healthy and not report.gaps:
        logger.info("worker: preflight integrity check passed")
        return True

    error_gaps = [g for g in report.gaps if g.severity == "error"]
    logger.warning(
        "worker: preflight integrity check found %d gap(s) — %d error / %d warn / %d info",
        len(report.gaps),
        sum(1 for g in report.gaps if g.severity == "error"),
        sum(1 for g in report.gaps if g.severity == "warn"),
        sum(1 for g in report.gaps if g.severity == "info"),
    )
    for gap in report.gaps:
        remediation_first_line = gap.remediation.split(";")[0].strip()
        logger.warning(
            "worker: preflight gap — [%s] %s count=%d — %s",
            gap.severity,
            gap.invariant,
            gap.count,
            remediation_first_line,
        )

    if error_gaps and deps.strict_fn():
        logger.warning(
            "worker: preflight strict mode active — %d error-severity gap(s) — exiting",
            len(error_gaps),
        )
        return False
    return True


def probe_vec_index_at_boot(
    *,
    db_path: Path | None = None,
    enabled: bool | None = None,
) -> None:
    """Open the vec_index at worker boot to surface recovery actions early.

    The 2026-05-31 production bug had the operator discover index
    corruption ~6 hours into a force-embed run, via the recall canary
    check at the end. This probe runs the same load_or_recreate() at
    boot so any recovery (orphan .tmp promotion, corrupt-file
    recreation) is logged immediately AND fixed in place before the
    first embed tick. The probe is idempotent — opening a healthy
    index is a no-op.

    Disabled when ``enabled=False`` (or, when ``enabled`` is None,
    when ``worker_writes_vec_index()`` returns False — the operator
    opted out of worker-side vec writes; the probe wouldn't help and
    the open shouldn't happen).

    Never raises — failures log as WARNING and boot continues. The
    embed pipeline's own load_or_recreate() will retry at first-tick
    if this probe somehow missed a state transition.

    ``db_path`` / ``enabled`` are the F2-clean test seams; production
    callers omit both and the boundary reads from KairixPaths.
    """
    try:
        if enabled is None:
            from kairix.paths import worker_writes_vec_index

            enabled = worker_writes_vec_index()
        if not enabled:
            return

        if db_path is None:
            from kairix.paths import db_path as get_db_path

            db_path = get_db_path()

        from kairix.core.embed.embed import open_usearch_index_for_paths

        # Opens, recovers, logs — no further action needed. The returned
        # VectorIndex isn't kept (the embed tick opens its own).
        open_usearch_index_for_paths(
            index_path=db_path.parent / "vectors.usearch",
            meta_path=db_path.parent / "vectors.meta.json",
            db_path=db_path,
        )
    except Exception as exc:  # pragma: no cover - boundary
        logger.warning(
            "worker: vec_index startup probe raised — %s. "
            "fix: this is non-fatal; first embed tick will retry. "
            "next: kairix onboard check if the warning persists. "
            "run: ls -la $KAIRIX_DOCUMENT_ROOT/../kairix/vectors.usearch*",
            exc,
        )


@dataclass
class TopologyApplyDeps:
    """Injectable dependencies for :func:`apply_topology_at_boot`.

    F6-clean: every field has a ``default_factory`` so production callers
    construct ``TopologyApplyDeps()`` and get the real boundary calls;
    tests construct ``TopologyApplyDeps(config_mapping_fn=...,
    db_factory=...)`` and pass it as a single argument to drive the
    apply step against a tmp_path-rooted config + DB without touching
    the dev's real vault.

    Fields:
      * ``config_mapping_fn`` — returns the parsed + MERGED operator
        config mapping (base + overlay, #492); ``{}`` when no config
        resolves. Default :func:`_load_merged_config_mapping_default`.
        Tests drive real file reads with
        ``lambda: load_merged_mapping(env={...})`` (explicit env dict,
        F2-clean).
      * ``db_factory`` — opens the SQLite connection the apply step
        writes through; default :func:`kairix.core.db.open_db`.
    """

    config_mapping_fn: Callable[[], dict[str, Any]] = field(default_factory=lambda: _load_merged_config_mapping_default)
    db_factory: Callable[[], sqlite3.Connection] = field(default_factory=lambda: _open_db_default)


@dataclass
class CanonicalEntitySeedDeps:
    """F6-clean injection seam for :func:`seed_canonical_entities_at_boot` (#431).

    Fields:
      * ``load_canonical_entities_fn`` — returns the parsed list of
        :class:`CanonicalEntity` from the operator's
        ``kairix.config.yaml`` ``canonical_entities:`` block. Default
        :func:`kairix.core.search.config_loader.load_canonical_entities`.
      * ``neo4j_client_fn`` — returns the Neo4jClient the seeder upserts
        through. Default :func:`kairix.knowledge.graph.client.get_client`.
    """

    load_canonical_entities_fn: Callable[[], list[Any]] = field(
        default_factory=lambda: _default_load_canonical_entities
    )
    neo4j_client_fn: Callable[[], Any] = field(default_factory=lambda: _default_neo4j_client_for_seed)


def _default_load_canonical_entities() -> list[Any]:
    """Production default — read the operator's YAML via config_loader."""
    from kairix.core.search.config_loader import load_canonical_entities

    return load_canonical_entities()


def _default_neo4j_client_for_seed() -> Any:
    """Production default — build the live Neo4jClient via get_client."""
    from kairix.knowledge.graph.client import get_client

    return get_client()


def seed_canonical_entities_at_boot(deps: CanonicalEntitySeedDeps | None = None) -> int:
    """Seed operator-declared canonical entities into Neo4j (#431).

    Reads the ``canonical_entities:`` YAML block, upserts each entry
    via :func:`seed_canonical_entities`. Returns the count seeded so
    boot logs can record a number.

    Failure-isolated: never raises. A missing config block, an
    unavailable Neo4j, a per-entity write failure, or a parser
    malformation all degrade to a logged warning + a returned count
    (zero on full degradation). The worker continues so the operator
    can fix the issue without crashlooping.

    Canonical seeding underpins ``entity_suggest`` exclusion: with the
    canonicals materialised in Neo4j, the suggester's
    ``find_by_name`` lookup returns ``is_new=False`` for them
    automatically. No additional filter wiring needed.
    """
    deps = deps if deps is not None else CanonicalEntitySeedDeps()

    try:
        canonicals = deps.load_canonical_entities_fn()
    except Exception as exc:
        logger.warning("worker: canonical-entity seed skipped — could not load config: %s", exc)
        return 0

    if not canonicals:
        logger.info("worker: canonical-entity seed skipped — no canonical_entities block in config")
        return 0

    try:
        from kairix.knowledge.entities.canonical import seed_canonical_entities

        client = deps.neo4j_client_fn()
        seeded = seed_canonical_entities(client, canonicals)
    except Exception as exc:
        logger.warning("worker: canonical-entity seed failed — %s", exc)
        return 0

    logger.info("worker: seeded %d canonical entities into Neo4j", seeded)
    return seeded


def apply_topology_at_boot(deps: TopologyApplyDeps | None = None) -> None:
    """Materialise the operator's ``topology:`` YAML into runtime rows.

    ``topology_config`` retired post-cutover (task #132); the
    apply step now runs unconditionally at boot. Returns None
    unconditionally — failures are logged but never crash the boot.
    The worker continues so the operator can fix the config without
    crashlooping; the cc_pair lookup in
    :func:`resolve_chunk_writer_for_entry` falls back to the legacy
    writer when no cc_pair has been registered, so a failed apply
    degrades gracefully.
    """
    deps = deps if deps is not None else TopologyApplyDeps()

    try:
        raw = deps.config_mapping_fn()
    except Exception as exc:
        logger.warning("worker: topology apply skipped — could not read config: %s", exc)
        return
    if not raw:
        logger.info("worker: topology apply skipped — no kairix.config.yaml on disk")
        return

    from kairix.config import parse_topology
    from kairix.core.connectors.topology_applier import (
        ApplyValidationError,
        apply_topology,
    )
    from kairix.core.db.schema import create_schema

    try:
        parsed = parse_topology(raw)
    except Exception as exc:
        logger.warning("worker: topology apply skipped — parse failure: %s", exc)
        return

    if not (parsed.connectors or parsed.credentials or parsed.cc_pairs or parsed.collections):
        logger.info("worker: topology apply skipped — no blocks declared in config")
        return

    db = deps.db_factory()
    try:
        create_schema(db)
        try:
            result = apply_topology(db, parsed)
        except ApplyValidationError as exc:
            logger.warning("worker: topology apply rejected — %s", exc)
            db.rollback()
            return
        db.commit()
    finally:
        db.close()

    logger.info(
        "worker: topology applied — created=%d updated=%d unchanged=%d",
        result.created,
        result.updated,
        result.unchanged,
    )


def _boot_state(deps: WorkerDeps) -> WorkerState:
    """Load prior state from disk (increment restart_count) or start fresh.

    #224 phase 5: if a prior run left a state file, we INCREMENT its
    ``restart_count`` and reuse historical counters so operators see
    lifetime totals across restarts.
    """
    prior = deps.read_state_fn(deps.state_path)
    if prior is not None:
        prior.restart_count += 1
        logger.info("worker: resumed from prior state — restart_count=%d", prior.restart_count)
        return prior
    logger.info("worker: no prior state on disk — starting fresh")
    return deps.state


def _apply_embed_outcome(state: WorkerState, outcome: EmbedRunOutcome, consecutive_noops: int) -> int:
    """Fold an embed outcome into worker state; return the updated no-op streak."""
    new_streak = 0 if outcome.did_work else consecutive_noops + 1
    state.consecutive_embed_noops = new_streak
    state.embedded_total += outcome.embedded
    state.failed_chunks_total += outcome.failed
    state.last_embed_run_at = time.time()
    state.last_embed_did_work = outcome.did_work
    if outcome.recall_passed is False:
        state.recall_alerts_total += 1
    return new_streak


def _apply_connector_sync_outcome(state: WorkerState, result: ConnectorSyncResult) -> None:
    """SYNC-OBS — fold a connector-sync tick into worker state (quiet ≠ dead).

    Mirrors :func:`_apply_embed_outcome`. ``syncs_attempted`` increments on
    EVERY tick the sync ran (regardless of yield) so ``kairix worker status``
    proves the worker is still polling even when no docs flowed.
    ``last_connector_tick_yielded`` is True iff this tick surfaced any new
    items — the per-tick quiet/active signal an operator reads to tell a
    healthy-idle source from a dead one.
    """
    state.syncs_attempted += 1
    state.last_connector_sync_at = time.time()
    state.last_connector_tick_yielded = result.synced > 0
    state.last_connector_synced = result.synced
    state.last_connector_dead_letter_added = result.dead_letter_added
    state.last_connector_connectors_polled = result.connectors_polled


def _check_paused(deps: WorkerDeps, transition: Callable[[WorkerPhase], None], previously_paused: bool) -> bool:
    """Handle the operator-pause flag. Returns the new ``previously_paused`` value.

    When the flag is present we sleep and return True; otherwise we restore
    IDLE phase if we were paused and return False.
    """
    if deps.pause_flag_path.exists():
        if not previously_paused:
            transition(WorkerPhase.PAUSED)
            logger.info("worker: paused — flag file present at %s", deps.pause_flag_path)
        deps.sleep(PAUSE_POLL_INTERVAL_S)
        return True
    if previously_paused:
        transition(WorkerPhase.IDLE)
        logger.info("worker: resumed — flag file removed")
    return False


def _log_maintenance_toggle(maintenance_active: bool, previously_skipping: bool, streak: int) -> bool:
    """Log skip-enter / skip-exit transitions; return the new ``previously_skipping`` flag."""
    if not maintenance_active and not previously_skipping:
        logger.info(
            "worker: skipping maintenance scans — %d consecutive no-op embeds (threshold %d)",
            streak,
            MAINTENANCE_SKIP_NOOP_THRESHOLD,
        )
        return True
    if maintenance_active and previously_skipping:
        logger.info("worker: maintenance scans resumed — embed found work")
        return False
    return previously_skipping


def _run_embed_cycle(
    deps: WorkerDeps,
    state: WorkerState,
    transition: Callable[[WorkerPhase], None],
    streak: int,
) -> int:
    """Run one embed pass, persist state, log idle-backoff if applicable. Returns new streak."""
    transition(WorkerPhase.INGEST)
    outcome = run_embed_with_outcome(deps)
    new_streak = _apply_embed_outcome(state, outcome, streak)
    transition(WorkerPhase.IDLE)
    return new_streak


def _run_maintenance_task(
    deps: WorkerDeps,
    transition: Callable[[WorkerPhase], None],
    task: Callable[[WorkerDeps], None],
) -> None:
    """Run one maintenance task with MAINTENANCE→IDLE phase transitions."""
    transition(WorkerPhase.MAINTENANCE)
    task(deps)
    transition(WorkerPhase.IDLE)


def maybe_run_connector_sync_tick(
    *,
    deps: WorkerDeps,
    transition: Callable[[WorkerPhase], None],
    state: WorkerState,
    state_path: Path,
    write_state_fn: Callable[[WorkerState, Path], None],
) -> ConnectorSyncResult | None:
    """SYNC-OBS — run one connector-sync tick and fold it into worker state.

    Mirrors :func:`maybe_run_maintenance_loop_tick`: runs the existing
    :func:`run_connector_sync` (with the same MAINTENANCE→IDLE phase
    transitions :func:`_run_maintenance_task` applied before), then folds
    the returned :class:`ConnectorSyncResult` into ``state`` and persists
    it via ``write_state_fn`` so ``kairix worker status`` reflects the
    latest tick. ``state`` is passed explicitly (NOT read off ``deps``)
    because :func:`_boot_state` may hand the loop a restored-from-disk
    state object distinct from ``deps.state`` — the same discipline
    :func:`maybe_run_maintenance_loop_tick` uses.

    Returns the result (``None`` when the sync was a no-op / raised) so
    callers / tests can assert on it. Cadence is decided by the caller —
    this fires unconditionally when invoked, so a quiet tick still bumps
    ``syncs_attempted`` (the whole point). Control flow is otherwise
    identical to the previous
    ``_run_maintenance_task(deps, transition, run_connector_sync)`` call
    that discarded the result — purely additive state capture.
    """
    transition(WorkerPhase.MAINTENANCE)
    result = run_connector_sync(deps)
    transition(WorkerPhase.IDLE)
    if result is not None:
        _apply_connector_sync_outcome(state, result)
        write_state_fn(state, state_path)
    return result


@dataclass(frozen=True)
class _LastTicks:
    """The last-run wall-clock for each maintenance tick (bundled to keep
    :func:`_maybe_run_maintenance_cycle` under the Sonar S107 param ceiling)."""

    entity: float
    health: float
    wikilinks: float
    connector_sync: float
    neo4j_drain: float
    wal_checkpoint: float
    deadletter_sweep: float
    rechunk_sweep: float


def _maybe_run_maintenance_cycle(
    *,
    deps: WorkerDeps,
    transition: Callable[[WorkerPhase], None],
    now: float,
    maintenance_active: bool,
    last: _LastTicks,
    schedule: _Schedule,
    state: WorkerState,
) -> tuple[float, float, float, float, float, float, float, float]:
    """Run any maintenance task whose interval has elapsed; return updated timestamps.

    Two buckets (#312):

    * **Local-content-dependent** (entity, health, wikilinks) — gated by
      ``maintenance_active``. When the local vault has been idle long
      enough to set the embed-noop streak above the threshold, none of
      these have anything to do and the maintenance scan is wasted work.

    * **External-source-discovery** (connector_sync), **Curator coupling
      boundary** (neo4j_drain, GH #334), **DB maintenance**
      (wal_checkpoint) AND the **orphaned-source dead-letter sweep**
      (deadletter_sweep, PR-5) — ALWAYS run on their intervals regardless
      of ``maintenance_active``. A quiet local vault does NOT imply quiet
      upstream sources, a drained ``entity_signals`` queue, or a drained
      orphaned-source dead-letter backlog.
    """
    # Unpack the bundled timestamps into locals so the dispatch body below
    # reads unchanged (the bundle exists only to bound the param count).
    last_entity = last.entity
    last_health = last.health
    last_wikilinks = last.wikilinks
    last_connector_sync = last.connector_sync
    last_neo4j_drain = last.neo4j_drain
    last_wal_checkpoint = last.wal_checkpoint
    last_deadletter_sweep = last.deadletter_sweep
    last_rechunk_sweep = last.rechunk_sweep

    new_entity, new_health, new_wikilinks = last_entity, last_health, last_wikilinks
    if maintenance_active:
        local_tasks = (
            ("entity", schedule.entity, last_entity, run_entity_seed),
            ("health", schedule.health, last_health, run_health_check),
            ("wikilinks", schedule.wikilinks, last_wikilinks, run_wikilinks_inject),
        )
        new_local: dict[str, float] = {
            "entity": last_entity,
            "health": last_health,
            "wikilinks": last_wikilinks,
        }
        for name, interval, last_run, task in local_tasks:
            if now - last_run >= interval:
                _run_maintenance_task(deps, transition, task)
                new_local[name] = now
        new_entity, new_health, new_wikilinks = new_local["entity"], new_local["health"], new_local["wikilinks"]

    new_connector_sync = last_connector_sync
    if now - last_connector_sync >= schedule.connector_sync:
        # SYNC-OBS — fold the tick's ConnectorSyncResult into worker state
        # (syncs_attempted / last_connector_*) so a quiet source is visible
        # on ``kairix worker status``. Same MAINTENANCE→IDLE transitions as
        # the prior _run_maintenance_task call; only the result capture is new.
        maybe_run_connector_sync_tick(
            deps=deps,
            transition=transition,
            state=state,
            state_path=deps.state_path,
            write_state_fn=deps.write_state_fn,
        )
        new_connector_sync = now

    new_neo4j_drain = last_neo4j_drain
    if now - last_neo4j_drain >= schedule.neo4j_drain:
        _run_maintenance_task(deps, transition, run_neo4j_drain)
        new_neo4j_drain = now

    new_wal_checkpoint = last_wal_checkpoint
    if now - last_wal_checkpoint >= schedule.wal_checkpoint:
        _run_maintenance_task(deps, transition, run_wal_checkpoint)
        new_wal_checkpoint = now

    new_deadletter_sweep = last_deadletter_sweep
    if now - last_deadletter_sweep >= schedule.deadletter_sweep:
        _run_maintenance_task(deps, transition, run_deadletter_sweep)
        new_deadletter_sweep = now

    # ADR-028 Wave F.4 — re-chunk sweep. Always-run bucket (independent of
    # ``maintenance_active``; the tick self-gates on its feature flags).
    new_rechunk_sweep = last_rechunk_sweep
    if now - last_rechunk_sweep >= schedule.rechunk_sweep:
        _run_maintenance_task(deps, transition, run_rechunk_sweep_tick)
        new_rechunk_sweep = now

    return (
        new_entity,
        new_health,
        new_wikilinks,
        new_connector_sync,
        new_neo4j_drain,
        new_wal_checkpoint,
        new_deadletter_sweep,
        new_rechunk_sweep,
    )


def _close_connector_runtime(connector_sync_fn: Callable[[], ConnectorSyncResult]) -> None:
    """Close a lifetime-owning connector sync callable when it exposes shutdown."""
    close = getattr(connector_sync_fn, "close", None)
    if callable(close):
        close()


def main(
    *,
    deps: WorkerDeps | None = None,
    embed_interval: int | None = None,
    entity_seed_interval: int | None = None,
    health_check_interval: int | None = None,
    wikilinks_interval: int | None = None,
    connector_sync_interval: int | None = None,
    neo4j_drain_interval: int | None = None,
    deadletter_sweep_interval: int | None = None,
    rechunk_sweep_interval: int | None = None,
) -> None:
    """Run the worker loop.

    All callable dependencies are bundled into ``WorkerDeps``;
    interval ints stay as plain kwargs because they're scalar
    config (not test-substitution seams). Production omits ``deps``
    and the default factory wires the real task callables.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Hydrate the secrets bundle into os.environ ONCE at worker boot,
    # before any SecretsLoader (or any credential-reading code path) is
    # constructed. After this call, kairix.secrets.SecretsLoader.get()
    # reads os.environ live and sees every canonical env var the bundle
    # provided. Replaces the per-call-site _ensure_bundle_loaded hacks.
    from kairix.secrets.bootstrap import bootstrap_secrets

    bootstrap_secrets()

    deps = deps if deps is not None else WorkerDeps()
    schedule = _resolve_schedule(
        embed_interval,
        entity_seed_interval,
        health_check_interval,
        wikilinks_interval,
        connector_sync_interval,
        neo4j_drain_interval,
        deadletter_sweep_interval=deadletter_sweep_interval,
        rechunk_sweep_interval=rechunk_sweep_interval,
    )

    logger.info(
        "kairix worker starting — embed every %ds, entity seed every %ds, wikilinks every %ds, neo4j drain every %ds",
        schedule.embed,
        schedule.entity,
        schedule.wikilinks,
        schedule.neo4j_drain,
    )

    # Preflight integrity audit — catches the IM-6 failure mode (FTS
    # silently empty) before the first embed cycle. Logs the report at
    # boot; ``KAIRIX_PREFLIGHT_STRICT=1`` makes error gaps fatal.
    if not _run_preflight_at_boot():
        return

    # Vec-index startup probe — surfaces any pending .tmp promotion or
    # corrupt-file recreation BEFORE the first embed tick runs. Without
    # this, operators learnt about recovery actions hours later via the
    # next recall canary (the 2026-05-31 production bug). The probe
    # itself is idempotent — load_or_recreate either passes through an
    # already-valid index or fixes it in place.
    probe_vec_index_at_boot()

    # Wave D apply-bridge — when the topology_config flag is ON, read
    # the parsed config and materialise it into runtime topology_* rows
    # before the first sync tick. Flag OFF: this is a structural no-op
    # (the function short-circuits before opening the DB). Failures
    # degrade gracefully — the legacy single-collection writer remains
    # the fallback in resolve_chunk_writer_for_entry.
    apply_topology_at_boot()

    # #431 — seed operator-declared canonical entities into Neo4j so
    # entity_suggest's find_by_name lookup returns is_new=False for
    # them. Failure-isolated; worker continues even when Neo4j /
    # config is degraded.
    seed_canonical_entities_at_boot()

    # Spec A — capability-recommender corpus build at boot. OUTER gate is
    # the ``recommender`` flag (default OFF → structural no-op: the DB is
    # never opened, the builder never runs). Failure-isolated; the worker
    # continues with an empty/stale corpus if the build degrades.
    maybe_build_capability_corpus_at_boot()

    state = _boot_state(deps)
    # Persist initial state (STARTING) so ``kairix worker status`` is
    # answerable immediately after boot, before the first embed completes.
    state.current_phase = WorkerPhase.STARTING
    state.last_phase_change_at = time.time()
    deps.write_state_fn(state, deps.state_path)

    def _transition(phase: WorkerPhase) -> None:
        """Update state's phase + timestamp and persist atomically.

        Each call is a single write — the persistence layer's temp-file +
        rename keeps concurrent ``kairix worker status`` readers safe.
        """
        state.current_phase = phase
        state.last_phase_change_at = time.time()
        deps.write_state_fn(state, deps.state_path)

    # Track when each task last ran
    last_embed = 0.0
    last_entity = 0.0
    last_health = 0.0
    last_wikilinks = 0.0
    last_connector_sync = 0.0
    # GH #334 — last Neo4j drain tick. Starts at 0.0 so the first
    # post-boot iteration drains immediately (matches the connector_sync
    # bootstrap convention).
    last_neo4j_drain = 0.0
    # R3 (#389) — last SQLite WAL checkpoint. Same 0.0 bootstrap so the
    # first post-boot iteration truncates the WAL immediately (catches
    # any inherited bloat from before the previous shutdown).
    last_wal_checkpoint = 0.0
    # PR-5 — last orphaned-source dead-letter sweep. Same 0.0 bootstrap so
    # the first post-boot iteration sweeps immediately, clearing any
    # orphaned backlog inherited from before the previous shutdown.
    last_deadletter_sweep = 0.0
    # ADR-028 Wave F.4 — last re-chunk sweep. 0.0 bootstrap so the first
    # post-boot iteration sweeps immediately (a no-op unless both flags are ON).
    last_rechunk_sweep = 0.0
    # KFEAT-021 — last maintenance tick. Carried in WorkerState across
    # restarts so the cadence survives a container bounce; mirror it
    # into a local for the in-loop is_tick_due comparison.
    last_maintenance_tick = state.last_maintenance_tick_at
    maintenance_interval = maintenance_interval_seconds()
    # ADR-036 — entity-summary projector tick cadence. Carried in
    # WorkerState so cadence survives a container bounce. Default 60s
    # — a 7,461-entity backlog clears in ~38 cycles (~38 min), well
    # within ADR-036's 24h soak window.
    last_entity_summary_tick = state.last_entity_summary_tick_at
    entity_summary_interval = entity_summary_projector_interval_seconds()

    # #224 idle backoff: extend the embed interval after consecutive
    # no-op runs to avoid steady CPU/I/O pressure on idle vaults.
    consecutive_embed_noops = state.consecutive_embed_noops

    # Graceful shutdown
    running = True

    def _shutdown(_signum: int, _frame: object) -> None:
        """Signal handler — flips ``running`` to False on SIGTERM/SIGINT.

        ``_signum``/``_frame`` are the standard signal-callback positional
        slots required by ``signal.signal`` (F19: underscore-prefixed).
        """
        nonlocal running
        logger.info("worker: shutdown signal received")
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Run embed immediately on startup
    consecutive_embed_noops = _run_embed_cycle(deps, state, _transition, consecutive_embed_noops)
    last_embed = time.monotonic()

    # #224 phase 4: one-shot log on pause/resume so we don't spam every 5s.
    previously_paused = False
    # #224 phase 2: same one-shot-log pattern for maintenance-skip episodes.
    previously_skipping_maint = False

    while running:
        previously_paused = _check_paused(deps, _transition, previously_paused)
        if previously_paused:
            continue

        now = time.monotonic()
        effective_embed_interval = compute_embed_interval(schedule.embed, consecutive_embed_noops)

        if now - last_embed >= effective_embed_interval:
            if effective_embed_interval != schedule.embed:
                logger.info(
                    "worker: idle backoff active — embed interval extended to %ds after %d no-op cycle(s)",
                    effective_embed_interval,
                    consecutive_embed_noops,
                )
            consecutive_embed_noops = _run_embed_cycle(deps, state, _transition, consecutive_embed_noops)
            last_embed = now

        # #224 phase 2 — skip-on-noop maintenance gating. After
        # MAINTENANCE_SKIP_NOOP_THRESHOLD consecutive no-op embed cycles
        # the three maintenance scans become pointless work. Embed continues
        # on its (already exponentially-backed-off) cadence, so a single
        # fresh document still resumes everything.
        maintenance_active = consecutive_embed_noops < MAINTENANCE_SKIP_NOOP_THRESHOLD
        previously_skipping_maint = _log_maintenance_toggle(
            maintenance_active, previously_skipping_maint, consecutive_embed_noops
        )

        (
            last_entity,
            last_health,
            last_wikilinks,
            last_connector_sync,
            last_neo4j_drain,
            last_wal_checkpoint,
            last_deadletter_sweep,
            last_rechunk_sweep,
        ) = _maybe_run_maintenance_cycle(
            deps=deps,
            transition=_transition,
            now=now,
            maintenance_active=maintenance_active,
            last=_LastTicks(
                entity=last_entity,
                health=last_health,
                wikilinks=last_wikilinks,
                connector_sync=last_connector_sync,
                neo4j_drain=last_neo4j_drain,
                wal_checkpoint=last_wal_checkpoint,
                deadletter_sweep=last_deadletter_sweep,
                rechunk_sweep=last_rechunk_sweep,
            ),
            schedule=schedule,
            state=state,
        )

        # KFEAT-021 — maintenance-loop tick after the sync cycle. The
        # flag check is the OUTER gate (no DB open when OFF); cadence
        # is the INNER gate. Bit-for-bit pre-KFEAT-021 behaviour when
        # the flag is OFF.
        last_maintenance_tick = maybe_run_maintenance_loop_tick(
            deps=deps.maintenance_loop_deps,
            transition=_transition,
            state=state,
            state_path=deps.state_path,
            write_state_fn=deps.write_state_fn,
            now=time.time(),
            last_tick_at=last_maintenance_tick,
            interval_seconds=maintenance_interval,
        )

        # ADR-036 — entity-summary projector tick. Same OUTER (flag) +
        # INNER (cadence) gate pattern as the maintenance loop above.
        # Bit-for-bit pre-ADR-036 behaviour when
        # entity_summary_indexing_enabled is OFF (the default).
        last_entity_summary_tick = maybe_run_entity_summary_projector_tick(
            deps=deps.entity_summary_projector_deps,
            transition=_transition,
            state=state,
            state_path=deps.state_path,
            write_state_fn=deps.write_state_fn,
            now=time.time(),
            last_tick_at=last_entity_summary_tick,
            interval_seconds=entity_summary_interval,
        )

        # Sleep 60 seconds between checks
        for _ in range(60):
            if not running:
                break
            deps.sleep(1)

    _close_connector_runtime(deps.connector_sync_fn)
    logger.info("kairix worker stopped")


if __name__ == "__main__":
    main()
