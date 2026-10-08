"""Contract: Neo4j entity lookups degrade to "not found" when the graph is down.

``find_by_name`` and ``related_entities`` feed CONTEXTUAL_PREP entity
resolution and cross-entity expansion. When the graph is unreachable they
must return ``[]`` (the documented "not found" shape) rather than raise
into the caller that is building context.

One body per method, two clients (F43 limb 2):

* the real :class:`Neo4jClient` whose ``driver_cls`` seam refuses the
  connection (Neo4j down at startup);
* the canonical :class:`tests.fixtures.neo4j_mock.FakeNeo4jClient` with an
  empty graph.

(Re-homed from the deleted ``EntityResolverProtocol`` failure-mode test,
which proved the same behaviour through a test-local adapter; the
Protocol had no production implementation or consumer.)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.knowledge.graph.client import Neo4jClient
from tests.fixtures.neo4j_mock import FakeNeo4jClient

pytestmark = pytest.mark.contract


class _RefusingNeo4jDriverModule:
    """Stand-in for the ``neo4j.GraphDatabase`` SDK entry point: the bolt
    endpoint refuses every connection."""

    @staticmethod
    def driver(uri: str, auth: tuple[str, str]) -> Any:
        del auth
        raise ConnectionRefusedError(f"bolt endpoint refused: {uri}")


_FACTORIES: list[Callable[[], Any]] = [
    lambda: Neo4jClient(
        uri="bolt://neo4j.invalid:7687",
        user="neo4j",
        password="unavailable-contract",  # pragma: allowlist secret — test fixture value, not a credential
        driver_cls=_RefusingNeo4jDriverModule,
    ),
    lambda: FakeNeo4jClient(entities=[]),
]
_IDS = ["real-neo4j-client", "fake"]


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_find_by_name_unavailable_graph_returns_empty(factory: Callable[[], Any]) -> None:
    """With the graph unreachable ``find_by_name`` returns ``[]`` (not found).

    Sabotage proof: in ``Neo4jClient.find_by_name`` replace the
    ``if not self._driver: return []`` guard with
    ``if not self._driver: raise RuntimeError("graph down")``. Re-run: the
    real case fails with ``RuntimeError``. Restored.
    """
    assert factory().find_by_name("Agent Alpha") == []


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_related_entities_unavailable_graph_returns_empty(factory: Callable[[], Any]) -> None:
    """With the graph unreachable ``related_entities`` returns ``[]``.

    Sabotage proof: in ``Neo4jClient.related_entities`` replace the
    ``if not self._driver: return []`` guard with
    ``if not self._driver: return [{"id": "stale"}]``. Re-run: the real case
    fails on the empty-list assertion. Restored.
    """
    assert factory().related_entities("agent-alpha") == []
