"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`DrainGraphRepository`.

Two Protocol members: ``available`` (property) + ``cypher(query, params)``.
Every body runs over BOTH the production drain repository —
:class:`kairix.knowledge.graph.repository.Neo4jGraphRepository` built the
way the drain wires it (``raise_on_error=True``) over a real
:class:`kairix.knowledge.graph.client.Neo4jClient`, its driver injected
through the public ``driver_cls=`` seam with
:class:`tests.fakes.FakeNeo4jDriverClass` — and the canonical
:class:`tests.fakes.FakeDrainGraphRepository` (F43 behavioural parity).

Two failure-class probes:

  * ``unavailable`` — ``available`` returns False when the backend is
    offline; callers (drain tick) skip the tick rather than crash.
  * ``raises`` — a MERGE the backend rejects RAISES, so the drain marks
    the signal failed (``pushed_to_neo4j = -1`` + ``last_push_error``)
    instead of acknowledging it as pushed.

History (PLA-472): the drain used to be wired with the read-path
repository, whose ``Neo4jClient.cypher`` logs and returns ``[]`` on any
error — so a rejected MERGE was acknowledged as pushed and the entity
silently never reached the graph. The drain now gets a strict repository.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import pytest

from kairix.core.curator.drain import run_neo4j_drain_tick
from kairix.core.curator.protocols import DrainGraphRepository
from kairix.core.db.schema import create_schema
from kairix.knowledge.graph.client import Neo4jClient
from kairix.knowledge.graph.repository import Neo4jGraphRepository
from tests.fakes import FakeDrainGraphRepository, FakeNeo4jDriverClass

pytestmark = pytest.mark.contract

_MERGE = "MERGE (n:Entity {name: $value}) RETURN n"

# A factory takes ``(online, rejects_queries)``.
RepoFactory = Callable[[bool, bool], DrainGraphRepository]


def _real_repo(online: bool, rejects_queries: bool) -> DrainGraphRepository:
    driver_cls = FakeNeo4jDriverClass(
        connect_raises=None if online else OSError("F68-neo4j-offline"),
        run_raises=RuntimeError("F68-transaction-aborted") if rejects_queries else None,
    )
    client = Neo4jClient(
        uri="bolt://graph.invalid:7687",
        user="neo4j",
        password="contract-fixture",  # pragma: allowlist secret — test fixture
        driver_cls=driver_cls,
    )
    return Neo4jGraphRepository(client, raise_on_error=True)


def _fake_repo(online: bool, rejects_queries: bool) -> DrainGraphRepository:
    return FakeDrainGraphRepository(available=online, raise_always=rejects_queries)


_IMPLEMENTATIONS: list[tuple[str, RepoFactory]] = [
    ("real", _real_repo),
    ("fake", _fake_repo),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_available_unavailable_when_backend_offline(name: str, factory: RepoFactory) -> None:
    """``available`` returns ``False`` when the backend is unreachable —
    the drain tick reads this BEFORE issuing any cypher and skips
    cleanly. An online backend reports ``True``.

    Sabotage proof (executed): in ``Neo4jClient._connect`` set
    ``self.available = True`` before ``verify_connectivity()``. Re-run:
    the ``real`` case fails. Restored.
    """
    assert factory(False, False).available is False, f"{name}: offline backend must report False"
    assert factory(True, False).available is True, f"{name}: online backend must report True"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_cypher_raises_rejected_merge_marks_the_signal_failed(name: str, factory: RepoFactory) -> None:
    """A MERGE the backend rejects raises, and the drain tick records the
    signal as FAILED — never acknowledged as pushed.

    Sabotage proof (executed): make ``Neo4jGraphRepository.cypher`` ignore
    ``raise_on_error`` (always ``self._client.cypher``). Re-run: the ``real``
    case fails — no raise, ``pushed == 1`` and the row is marked
    ``pushed_to_neo4j = 1`` although the MERGE never landed. Restored. The
    production wiring itself is pinned by the drain unit + curator CLI tests.
    """
    repo = factory(True, True)
    assert repo.available is True, name
    with pytest.raises(RuntimeError):
        repo.cypher(_MERGE, {"value": "alpha"})

    db = sqlite3.connect(":memory:")
    create_schema(db)
    db.execute(
        "INSERT INTO entity_signals (kind, value, source_uri, modified_at, confidence, "
        "sensitivity, pushed_to_neo4j, push_attempt_count) "
        "VALUES ('person', 'agent-alpha', 'vault://agent-alpha.md', '2026-05-20T10:00:00Z', 0.9, 'internal', 0, 0)"
    )

    result = run_neo4j_drain_tick(db, repo, batch_size=10)

    assert (result.pushed, result.failed) == (0, 1), name
    pushed, error = db.execute("SELECT pushed_to_neo4j, last_push_error FROM entity_signals").fetchone()
    assert pushed == -1, name
    assert error, name
