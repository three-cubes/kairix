"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`ChunkWriter`.

The operational failure class that bites in production is ``raises``:
the writer's underlying store (SQLite ``IntegrityError``, ``DiskFull``,
``Locked``, a missing table, a failing FTS5 rebuild) raises on a write.
The pipeline does NOT wrap the writer call in a try/except — the
exception propagates from ``_process_item`` → ``_process_batch``, which
rolls back the per-batch transaction so chunks, cursor advance, and
Bronze writes all commit-or-rollback together.

Every body runs over BOTH the production SQLite writer (built through
the sanctioned ``legacy_chunk_writer`` surface) and the canonical
:class:`tests.fakes.FakeChunkWriter` (F43 behavioural parity). The real
writer's store failure is produced honestly: it is bound to a
connection whose ``documents`` table does not exist, so SQLite rejects
the statement; the fake's ``raise_on_*`` knobs raise the same
:class:`sqlite3.OperationalError`.

Composition follows F47.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator

import pytest

from kairix.core.connectors.collection_router import legacy_chunk_writer
from kairix.core.db.schema import create_schema
from kairix.core.factory import build_connector_pipeline
from kairix.core.protocols import ChangeEvent, ChunkWriter
from tests.fakes import FakeChunkWriter, FakeEntityGraphSink, FakeExtractor, FakeSourceConnector

pytestmark = pytest.mark.contract

_TABLE_ABSENT = "no such table: documents"

# A factory takes ``(connections, store_healthy)`` — ``store_healthy=False``
# builds a writer whose backing store rejects every statement.
WriterFactory = Callable[[list[sqlite3.Connection], bool], ChunkWriter]


def _real_writer(connections: list[sqlite3.Connection], store_healthy: bool) -> ChunkWriter:
    db = sqlite3.connect(":memory:")
    connections.append(db)
    if store_healthy:
        create_schema(db)
    writer: ChunkWriter = legacy_chunk_writer(db, collection="brand-new-collection")
    return writer


def _fake_writer(_connections: list[sqlite3.Connection], store_healthy: bool) -> ChunkWriter:
    if store_healthy:
        return FakeChunkWriter()
    err = sqlite3.OperationalError(_TABLE_ABSENT)
    return FakeChunkWriter(raise_on_upsert=err, raise_on_delete=err)


_IMPLEMENTATIONS: list[tuple[str, WriterFactory]] = [
    ("real", _real_writer),
    ("fake", _fake_writer),
]


@pytest.fixture
def connections() -> Iterator[list[sqlite3.Connection]]:
    opened: list[sqlite3.Connection] = []
    yield opened
    for db in opened:
        db.close()


def _make_event(item_id: str, modified_at: str = "2026-01-01T00:00:00Z") -> ChangeEvent:
    return ChangeEvent(op="created", item_id=item_id, modified_at=modified_at)


# ---------------------------------------------------------------------------
# ChunkWriter.upsert — raises (e.g. SQLite IntegrityError, FTS5 rebuild error)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_upsert_raises_propagates_and_rolls_back_bronze(
    name: str, factory: WriterFactory, connections: list[sqlite3.Connection]
) -> None:
    """When ``chunk_writer.upsert`` raises, the
    :class:`ConnectorPipeline` does NOT absorb it — the exception
    propagates from ``_process_item`` → ``_process_batch``, which
    rolls back the per-batch transaction (Bronze write included).

    Sabotage proof: in ``ConnectorPipeline._process_item`` wrap the
    ``self._chunk_writer.upsert(...)`` call in
    ``try: ... except Exception: pass``. Re-run: both cases fail because
    ``pytest.raises`` sees no exception AND ``bronze_records`` retains
    the in-flight row instead of being rolled back. Restored.
    """
    db = sqlite3.connect(":memory:")
    connections.append(db)
    create_schema(db)
    source = FakeSourceConnector(
        name="writer-raises",
        events=[_make_event("item-001")],
        content={"item-001": b"body-content"},
    )
    # F47-compliant: ConnectorPipeline composed via the factory entry point.
    pipeline = build_connector_pipeline(
        db=db,
        collection="default",
        chunk_writer=factory(connections, False),
        entity_graph_sink=FakeEntityGraphSink(),
    )

    # The message pins the failure to the writer boundary — the pipeline
    # db itself carries the full schema, so only the writer can raise it.
    with pytest.raises(sqlite3.OperationalError, match=_TABLE_ABSENT):
        pipeline.run_batch(source, FakeExtractor())

    # Per-batch transaction rolled back: bronze_records must be empty
    # for this source. The SQLite-side rollback is the durable contract.
    bronze_count = int(
        db.execute(
            "SELECT COUNT(*) FROM bronze_records WHERE source_name = ?",
            ("writer-raises",),
        ).fetchone()[0]
    )
    assert bronze_count == 0, f"{name}: bronze_records must roll back on writer raise; got {bronze_count} row(s)"


# ---------------------------------------------------------------------------
# F68 failure-mode coverage — delete_by_source_uri (ADR-036, #459 Slice A)
#
# The delete path is structurally simpler than upsert (no per-batch
# transaction here — the caller's transaction owns commit), so the
# contract pinned is "exception propagates" + "empty-state safe".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_delete_by_source_uri_raises_propagates_to_caller(
    name: str, factory: WriterFactory, connections: list[sqlite3.Connection]
) -> None:
    """F68 — when the underlying store raises on delete, the exception
    propagates so the caller's per-batch transaction can roll back.

    Sabotage proof (executed): wrap the body of
    ``_SqliteChunkWriter.delete_by_source_uri`` in ``try/except Exception:
    return 0``. Re-run: the ``real`` case fails (no exception reaches the
    caller, the rollback discipline silently breaks). Restored.
    """
    writer = factory(connections, False)
    with pytest.raises(sqlite3.OperationalError, match=_TABLE_ABSENT):
        writer.delete_by_source_uri("entity://Q1")


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_delete_by_source_uri_returns_empty_for_unknown_collection(
    name: str, factory: WriterFactory, connections: list[sqlite3.Connection]
) -> None:
    """F68 ``returns_empty`` — a delete targeted at a fresh collection
    (URI never written) returns 0 and writes nothing, no exception.

    Locks the safe-on-empty-state contract: callers building a fresh
    projector mid-startup can't crash by delete-then-upsert on the
    first run.

    Sabotage proof: in ``_SqliteChunkWriter.delete_by_source_uri`` return
    ``int(cursor.rowcount or 0) + 1``. Re-run: the ``real`` case fails.
    Restored.
    """
    writer = factory(connections, True)
    assert writer.delete_by_source_uri("entity://Q-never-written") == 0, name
