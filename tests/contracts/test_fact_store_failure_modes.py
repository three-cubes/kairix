"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`FactStore`.

Every public method on :class:`kairix.core.protocols.FactStore` has
at least one test here exercising a named failure class
(``raises`` / ``returns_empty``).

The four methods + their contract failure surfaces:

  * ``add`` — idempotent: re-adding the same id is a no-op (the
    "returns_empty" shape — no error, no observable state change).
  * ``search`` — empty-list return for no matches (returns_empty);
    must absorb empty-string queries without raising.
  * ``find_conflicts`` — empty-list return when the (entity,
    attribute) key has no live facts.
  * ``supersede`` — raises KeyError when either id is absent (the
    raises shape pinned by the Protocol docstring).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kairix.core.facts.store import SQLiteFactStore
from kairix.core.protocols import FactStore
from tests.fakes import FakeFactRecord, FakeFactStore

pytestmark = pytest.mark.contract

StoreFactory = Callable[[Path], FactStore]

_IMPLEMENTATIONS: list[tuple[str, StoreFactory]] = [
    ("real", lambda tmp_path: SQLiteFactStore(db_path=tmp_path / "facts.sqlite")),
    ("fake", lambda _tmp_path: FakeFactStore()),
]


def _fact(**fields: Any) -> FakeFactRecord:
    """A FactRecord with grounded provenance (the store rejects none)."""
    return FakeFactRecord(source_turn_ids=("t1",), **fields)


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_add_returns_empty_when_id_already_exists(name: str, factory: StoreFactory, tmp_path: Path) -> None:
    """Re-adding the same id is the Protocol's idempotency contract —
    no exception, no overwrite, no observable change. The "returns
    empty" shape is the absence of a second row.

    Sabotage proof: change ``FakeFactStore.add`` to unconditionally
    overwrite (``self._facts[fact.id] = fact``). Re-ran: the second
    add now wins and the ``value == "first"`` assertion fails. Restored.
    """
    store = factory(tmp_path)
    first = _fact(id="f1", entity="agent-alpha", attribute="role", value="first")
    second = _fact(id="f1", entity="agent-alpha", attribute="role", value="overwrite")
    store.add(first)
    store.add(second)  # idempotent — second add is the no-op

    hits = store.search("agent-alpha role first", top_k=5)
    assert hits
    assert hits[0].record.value == "first"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_search_returns_empty_when_no_match(name: str, factory: StoreFactory, tmp_path: Path) -> None:
    """Empty result for a no-match query — callers iterate without a
    null check.

    Sabotage proof: change ``FakeFactStore.search`` to return ``None``
    when no facts match. Re-ran: the ``== []`` assertion fails.
    Restored.
    """
    store = factory(tmp_path)
    store.add(_fact(id="f1", entity="agent-alpha", attribute="role", value="VP"))
    assert store.search("zztop-no-match-token") == []


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_search_returns_empty_when_namespace_excludes_all_rows(
    name: str, factory: StoreFactory, tmp_path: Path
) -> None:
    """Namespace filtering produces an empty result when no fact lives
    in the requested namespace — distinct from a query-mismatch empty.

    Sabotage proof: change ``FakeFactStore.search`` to ignore the
    namespace filter. Re-ran: the cross-namespace fact leaks into the
    result and the ``== []`` assertion fails. Restored.
    """
    store = factory(tmp_path)
    store.add(
        _fact(
            id="f1",
            entity="agent-alpha",
            attribute="role",
            value="VP",
            namespace="ns-A",
        )
    )
    assert store.search("agent-alpha role", namespace="ns-B") == []


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_find_conflicts_returns_empty_when_key_unknown(name: str, factory: StoreFactory, tmp_path: Path) -> None:
    """Unknown (entity, attribute) key returns ``[]`` — distinguishable
    from "key has live facts" and from a raised error.

    Sabotage proof: change ``FakeFactStore.find_conflicts`` to return
    every fact regardless of key. Re-ran: the assertion fails because
    the unrelated fact leaks in. Restored.
    """
    store = factory(tmp_path)
    store.add(_fact(id="f1", entity="agent-alpha", attribute="role", value="VP"))
    assert store.find_conflicts(entity="agent-zeta", attribute="role") == []


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_supersede_raises_when_old_id_absent(name: str, factory: StoreFactory, tmp_path: Path) -> None:
    """Supersede on an unknown ``old_id`` raises KeyError — Protocol
    docstring explicit. Production callers handle the absent case
    explicitly; silent success would mask a logic bug.

    Sabotage proof (executed): drop the ``_row_exists(conn, old_id)``
    check in ``SQLiteFactStore.supersede``. Re-run: the ``real`` case's
    ``pytest.raises`` sees nothing. Restored.
    """
    store = factory(tmp_path)
    store.add(_fact(id="new", entity="x", attribute="y", value="z"))
    with pytest.raises(KeyError, match="no fact with id 'missing'"):
        store.supersede(old_id="missing", new_id="new")


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_supersede_raises_when_new_id_absent(name: str, factory: StoreFactory, tmp_path: Path) -> None:
    """Mirror of the above for the new_id arm — both raises are
    documented in the Protocol contract.

    Sabotage proof: change ``FakeFactStore.supersede`` to skip the
    new_id check. Re-ran: ``pytest.raises`` sees nothing.  Restored.
    """
    store = factory(tmp_path)
    store.add(_fact(id="old", entity="x", attribute="y", value="z"))
    with pytest.raises(KeyError, match="no fact with id 'missing'"):
        store.supersede(old_id="old", new_id="missing")
