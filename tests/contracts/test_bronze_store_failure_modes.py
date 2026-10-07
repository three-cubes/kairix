"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`BronzeStore`.

Three Protocol methods (``write`` / ``read`` / ``replay``). The shipped
:class:`kairix.core.connectors.streaming_bronze.StreamingBronzeStore`
exposes a documented failure surface — its ``read`` ALWAYS raises
:class:`BronzeNotPersistedError` because streaming bronze does not
retain raw bytes. ``write`` propagates SQLite errors when the schema is
absent; ``replay`` returns the empty iterator when no rows match the
``source_name`` filter.

Every body runs over BOTH the production store (on an in-memory SQLite
connection) and the canonical :class:`tests.fakes.FakeBronzeStore`
(F43 behavioural parity).

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator

import pytest

from kairix.core.connectors.streaming_bronze import BronzeNotPersistedError, StreamingBronzeStore
from kairix.core.db.schema import create_schema
from kairix.core.protocols import BronzeRef, BronzeStore
from tests.fakes import FakeBronzeStore

pytestmark = pytest.mark.contract

# A factory takes ``schema_present`` — False models a store whose
# backing table does not exist (the write-rejection failure class).
StoreFactory = Callable[[bool], BronzeStore]


@pytest.fixture
def connections() -> Iterator[list[sqlite3.Connection]]:
    opened: list[sqlite3.Connection] = []
    yield opened
    for db in opened:
        db.close()


def _real_factory(connections: list[sqlite3.Connection]) -> StoreFactory:
    def _build(schema_present: bool) -> BronzeStore:
        db = sqlite3.connect(":memory:")
        connections.append(db)
        if schema_present:
            create_schema(db)
        return StreamingBronzeStore(db=db)

    return _build


def _fake_factory(_connections: list[sqlite3.Connection]) -> StoreFactory:
    def _build(schema_present: bool) -> BronzeStore:
        if schema_present:
            return FakeBronzeStore()
        return FakeBronzeStore(raise_on_write=sqlite3.OperationalError("no such table: bronze_records"))

    return _build


_IMPLEMENTATIONS: list[tuple[str, Callable[[list[sqlite3.Connection]], StoreFactory]]] = [
    ("real", _real_factory),
    ("fake", _fake_factory),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_write_raises_when_schema_absent(
    name: str,
    factory: Callable[[list[sqlite3.Connection]], StoreFactory],
    connections: list[sqlite3.Connection],
) -> None:
    """``write`` issues an INSERT on ``bronze_records``; if the table
    doesn't exist (schema not created) the SQLite layer raises
    :class:`sqlite3.OperationalError` — the Protocol surface must
    propagate, not swallow.

    Sabotage proof (executed): wrap the ``self._db.execute(...)`` in
    ``StreamingBronzeStore.write`` in ``try/except sqlite3.OperationalError:
    pass``. Re-run: the ``real`` case fails because ``pytest.raises``
    sees no exception. Restored.
    """
    store = factory(connections)(False)
    with pytest.raises(sqlite3.OperationalError, match="bronze_records"):
        store.write("src-alpha", "item-001", b"body", "text/plain")


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_read_raises_bronze_not_persisted_on_streaming_ref(
    name: str,
    factory: Callable[[list[sqlite3.Connection]], StoreFactory],
    connections: list[sqlite3.Connection],
) -> None:
    """``read`` ALWAYS raises — it's the documented "streaming bronze
    doesn't retain bytes" failure shape. The re-extract path must route
    through ``connector.fetch`` instead. Holds even for a ref the store
    itself just wrote.

    Sabotage proof: in :class:`StreamingBronzeStore.read` change
    ``raise BronzeNotPersistedError(...)`` to ``return (b"", ref.mime)``.
    Re-run: the ``real`` case fails because no exception is raised.
    Restored.
    """
    store = factory(connections)(True)
    written = store.write("src-alpha", "item-001", b"body", "text/plain")
    assert written.raw_path is None, f"{name}: streaming refs carry no raw_path"
    ref = BronzeRef(
        source_name="src-alpha",
        item_id="item-001",
        raw_path=None,
        mime="text/plain",
        fetched_at="2026-01-01T00:00:00Z",
    )
    for probe in (ref, written):
        with pytest.raises(BronzeNotPersistedError, match="streaming bronze does not retain raw bytes"):
            store.read(probe)


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_replay_returns_empty_when_no_records_for_source(
    name: str,
    factory: Callable[[list[sqlite3.Connection]], StoreFactory],
    connections: list[sqlite3.Connection],
) -> None:
    """``replay`` yields zero items when no ``bronze_records`` row has
    ``source_name == requested``. Callers must distinguish "no fetch"
    (empty iterator) from "fetch failed" (raises). Empty is the
    observable proof.

    Sabotage proof: in :class:`StreamingBronzeStore.replay` change the
    SQL ``WHERE source_name = ?`` to ``WHERE 1=1``. Re-run: the ``real``
    case fails because the inserted ``src-other`` row leaks into the
    result. Restored.
    """
    store = factory(connections)(True)
    # Write one record for a DIFFERENT source so we can prove the filter
    # narrows correctly (sabotage-provable empty).
    store.write("src-other", "item-001", b"body", "text/plain")
    rows = list(store.replay("src-alpha"))
    assert rows == [], f"{name}: empty source must yield empty iterator; got {rows!r}"
    # Positive control: the other source's row IS replayable.
    assert [r.item_id for r in store.replay("src-other")] == ["item-001"], name
