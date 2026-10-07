"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`EntityGraphSink`.

:class:`kairix.core.protocols.EntityGraphSink` has a single public
method — :meth:`buffer` — but covers two distinct failure classes:

  * ``raises`` — the sink's underlying store raised (SQLite error,
    disk full, missing table). The pipeline does NOT wrap the sink call
    in a try/except, so the exception propagates and the per-chunk
    transaction rolls back.
  * ``unavailable`` — the sink's DOWNSTREAM delivery target (Curator
    drain → Neo4j) is unreachable. Staging is durable and decoupled
    from delivery: the sink still stages the signals (``pushed_to_neo4j=0``)
    and connector ingest keeps making forward progress; the drain
    delivers them once the graph recovers (the #334 behaviour).

Every body runs over BOTH the production ``_SqliteEntityGraphSink`` —
the sink :func:`kairix.core.factory.build_connector_pipeline` composes
by default — and the canonical :class:`tests.fakes.FakeEntityGraphSink`
(F43 behavioural parity). Composition follows F47.

Parity finding (PLA-472): the fake's ``available=False`` mode used to
DROP the batch and return 0, while the production sink always stages
durably (it has no knowledge of the downstream graph). The fake was made
faithful — an outage window is counted but the batch is still staged.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest

from kairix.core.db.schema import create_schema
from kairix.core.factory import build_connector_pipeline
from kairix.core.protocols import ChangeEvent
from tests.fakes import FakeChunkWriter, FakeEntityGraphSink, FakeExtractor, FakeSourceConnector

pytestmark = pytest.mark.contract

_ENTITY_BODY = b"# heading\n\nbody text with entities Acme Inc. and Globex Corporation met Alice Smith."
_TABLE_ABSENT = "no such table: entity_signals"


@dataclass
class _Harness:
    """One impl under test: the db the pipeline runs on, the sink to inject
    (``None`` = the production default sink), and how to read back the
    signals the sink staged."""

    db: sqlite3.Connection
    sink: FakeEntityGraphSink | None
    staged: Callable[[], int]
    flip_available: Callable[[], None] = field(default=lambda: None)


def _real_harness(store_healthy: bool, downstream_up: bool) -> _Harness:
    del downstream_up  # the production sink never consults the downstream graph
    db = sqlite3.connect(":memory:")
    create_schema(db)
    if not store_healthy:
        db.execute("DROP TABLE entity_signals")
    return _Harness(
        db=db,
        sink=None,
        staged=lambda: int(db.execute("SELECT COUNT(*) FROM entity_signals WHERE pushed_to_neo4j = 0").fetchone()[0]),
    )


def _fake_harness(store_healthy: bool, downstream_up: bool) -> _Harness:
    db = sqlite3.connect(":memory:")
    create_schema(db)
    sink = (
        FakeEntityGraphSink(available=downstream_up)
        if store_healthy
        else FakeEntityGraphSink(raise_on_stage=sqlite3.OperationalError(_TABLE_ABSENT))
    )
    return _Harness(
        db=db,
        sink=sink,
        staged=lambda: sum(len(batch) for batch in sink.staged),
        flip_available=lambda: sink.set_available(True),
    )


HarnessFactory = Callable[[bool, bool], _Harness]

_IMPLEMENTATIONS: list[tuple[str, HarnessFactory]] = [
    ("real", _real_harness),
    ("fake", _fake_harness),
]


@pytest.fixture
def harnesses() -> Iterator[list[_Harness]]:
    built: list[_Harness] = []
    yield built
    for h in built:
        h.db.close()


def _make_event(item_id: str, modified_at: str = "2026-01-01T00:00:00Z") -> ChangeEvent:
    return ChangeEvent(op="created", item_id=item_id, modified_at=modified_at)


def _run_tick(h: _Harness, writer: FakeChunkWriter, item_id: str, modified_at: str) -> object:
    """F47-compliant: ConnectorPipeline composed via the factory entry point."""
    pipeline = build_connector_pipeline(db=h.db, collection="default", chunk_writer=writer, entity_graph_sink=h.sink)
    source = FakeSourceConnector(
        name="sink-contract",
        events=[_make_event(item_id, modified_at)],
        content={item_id: _ENTITY_BODY},
    )
    return pipeline.run_batch(source, FakeExtractor())


# ---------------------------------------------------------------------------
# EntityGraphSink.buffer — raises (e.g. SQLite IntegrityError, disk full)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_buffer_raises_propagates_and_rolls_back_chunk(
    name: str, factory: HarnessFactory, harnesses: list[_Harness]
) -> None:
    """When ``entity_graph_sink.buffer`` raises, the
    :class:`ConnectorPipeline` does NOT absorb it — the exception
    propagates from ``_process_item`` → ``_process_batch``, which
    rolls back the failing chunk (and re-raises).

    Sabotage proof (executed): in ``_SqliteEntityGraphSink.buffer``
    ``return 0`` before the ``executemany`` (swallowing the store). Re-run:
    the ``real`` case fails because ``pytest.raises`` sees no exception.
    Restored.
    """
    h = factory(False, True)
    harnesses.append(h)
    with pytest.raises(sqlite3.OperationalError, match=_TABLE_ABSENT):
        _run_tick(h, FakeChunkWriter(), "item-001", "2026-01-01T00:00:00Z")

    # The rollback is at the SQLite transaction layer: bronze_records
    # must be empty for the source.
    bronze_count = int(
        h.db.execute("SELECT COUNT(*) FROM bronze_records WHERE source_name = ?", ("sink-contract",)).fetchone()[0]
    )
    assert bronze_count == 0, f"{name}: bronze_records must roll back on sink raise; got {bronze_count} row(s)"


# ---------------------------------------------------------------------------
# EntityGraphSink.buffer — unavailable downstream (mirrors #334)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_buffer_unavailable_downstream_signals_stay_in_staging(
    name: str, factory: HarnessFactory, harnesses: list[_Harness]
) -> None:
    """The ``unavailable`` failure class — the downstream write target
    (Curator drain → Neo4j) is unreachable. Ingest is decoupled from
    delivery: the chunk commits, nothing dead-letters, and the signals
    sit durably in staging awaiting the drain.

    Sabotage proof (executed): in ``_SqliteEntityGraphSink.buffer``
    ``return 0`` before the ``executemany``. Re-run: the ``real`` case
    fails because nothing is staged. Restored.
    """
    h = factory(True, False)
    harnesses.append(h)
    writer = FakeChunkWriter()
    result = _run_tick(h, writer, "item-001", "2026-01-01T00:00:00Z")
    assert getattr(result, "processed", None) == 1, name
    assert getattr(result, "dead_lettered", None) == 0, name
    assert len(writer.writes) == 1, f"{name}: the writer must still receive the chunk; got {writer.writes!r}"
    assert h.staged() >= 1, f"{name}: signals must stay staged while the downstream graph is unavailable"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_buffer_recovers_after_unavailable_window_signals_eventually_staged(
    name: str, factory: HarnessFactory, harnesses: list[_Harness]
) -> None:
    """Across an outage window and its recovery, every tick's signals
    are staged — nothing from the outage tick is lost, and the post-
    recovery tick adds to (not replaces) the staged backlog.

    Sabotage proof (executed): in ``_SqliteEntityGraphSink.buffer``
    ``return 0`` before the ``executemany`` (dropping the batch). Re-run:
    the ``real`` case fails on the backlog assertion. Restored.
    """
    h = factory(True, False)
    harnesses.append(h)
    _run_tick(h, FakeChunkWriter(), "item-001", "2026-01-01T00:00:00Z")
    after_outage = h.staged()
    assert after_outage >= 1, name

    h.flip_available()
    _run_tick(h, FakeChunkWriter(), "item-002", "2026-01-02T00:00:00Z")
    assert h.staged() > after_outage, f"{name}: the recovery tick must stage its batch on top of the backlog"
