"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`DrainGraphRepository`.

Two Protocol members: ``available`` (property) + ``cypher(query, params)``.
Every body runs over BOTH the production drain repository —
:class:`kairix.knowledge.graph.repository.Neo4jGraphRepository` over a
real :class:`kairix.knowledge.graph.client.Neo4jClient`, its driver
injected through the public ``driver_cls=`` seam with
:class:`tests.fakes.FakeNeo4jDriverCls` — and the canonical
:class:`tests.fakes.FakeDrainGraphRepository` (F43 behavioural parity).

Two failure-class probes:

  * ``unavailable`` — ``available`` returns False when the backend is
    offline; callers (drain tick) skip the tick rather than crash.
  * ``returns_empty`` — when the backend rejects a query, production
    ``Neo4jClient.cypher`` logs a WARNING and returns ``[]`` (never
    raises).

Parity finding (PLA-472): the previous contract asserted that ``cypher``
RAISES on a rejected query, proved only against the fake's
``raise_always`` knob. The production drain repository never raises —
so the drain's per-row "failed" accounting (driven in tests by
``raise_always`` / ``raise_on_value``) is unreachable in production and a
rejected MERGE is indistinguishable from an applied one. The fake gained a
faithful ``reject_silently`` mode; this contract pins the real behaviour.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.core.curator.protocols import DrainGraphRepository
from kairix.knowledge.graph.client import Neo4jClient
from kairix.knowledge.graph.repository import Neo4jGraphRepository
from tests.fakes import FakeDrainGraphRepository, FakeNeo4jDriverCls

pytestmark = pytest.mark.contract

_MERGE = "MERGE (n:Entity {name: $value}) RETURN n"

# A factory takes ``(online, rejects_queries)``.
RepoFactory = Callable[[bool, bool], DrainGraphRepository]


def _real_repo(online: bool, rejects_queries: bool) -> DrainGraphRepository:
    driver_cls = FakeNeo4jDriverCls(
        connect_error=None if online else OSError("F68-neo4j-offline"),
        run_error=RuntimeError("F68-transaction-aborted") if rejects_queries else None,
    )
    client = Neo4jClient(
        uri="bolt://graph.invalid:7687",
        user="neo4j",
        password="contract-fixture",  # pragma: allowlist secret — test fixture
        driver_cls=driver_cls,
    )
    return Neo4jGraphRepository(client)


def _fake_repo(online: bool, rejects_queries: bool) -> DrainGraphRepository:
    return FakeDrainGraphRepository(available=online, reject_silently=rejects_queries)


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
def test_cypher_returns_empty_when_backend_rejects_query(name: str, factory: RepoFactory) -> None:
    """A query the backend rejects surfaces as ``[]`` — the production
    client logs and swallows driver errors (see the module docstring's
    PLA-472 finding).

    Sabotage proof (executed): in ``Neo4jClient.cypher`` re-raise from the
    ``except Exception`` branch. Re-run: the ``real`` case fails with the
    injected RuntimeError. Restored.
    """
    repo = factory(True, True)
    assert repo.available is True, name
    assert repo.cypher(_MERGE, {"value": "alpha"}) == [], name
