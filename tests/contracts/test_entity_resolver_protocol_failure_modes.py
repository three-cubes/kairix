"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`EntityResolverProtocol`.

``kairix.quality.contracts.entities.EntityResolverProtocol`` resolves an
entity name to its id and walks relationships. No production class
declares it directly, so the contract is proved over the thin conforming
adapter (``find_by_name`` / ``related_entities``) the existing shape test
uses, driven against a real graph client.

When the graph is unreachable the contract's documented "not found"
shapes must hold — ``resolve`` returns ``None`` and ``related`` returns
``[]`` — rather than raising into the caller that is building context.

One body per method, two graph clients under the adapter (F43 limb 2):

* the real :class:`Neo4jClient` whose ``driver_cls`` seam refuses the
  connection (Neo4j down at startup);
* the canonical :class:`tests.fixtures.neo4j_mock.FakeNeo4jClient` with an
  empty graph.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.knowledge.graph.client import Neo4jClient
from kairix.quality.contracts.entities import EntityResolverProtocol
from tests.fixtures.neo4j_mock import FakeNeo4jClient

pytestmark = pytest.mark.contract


class _RefusingNeo4jDriverModule:
    """Stand-in for the ``neo4j.GraphDatabase`` SDK entry point: the bolt
    endpoint refuses every connection."""

    @staticmethod
    def driver(uri: str, auth: tuple[str, str]) -> Any:
        del auth
        raise ConnectionRefusedError(f"F68 bolt endpoint refused: {uri}")


class _GraphEntityResolver:
    """``EntityResolverProtocol`` over a Neo4j-client-shaped graph."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def resolve(self, name: str) -> str | None:
        rows = self._client.find_by_name(name)
        return str(rows[0]["id"]) if rows else None

    def related(self, entity_id: str, rel_type: str | None = None) -> list[str]:
        del rel_type
        return [str(row.get("id", "")) for row in self._client.related_entities(entity_id)]


_FACTORIES: list[Callable[[], Any]] = [
    lambda: Neo4jClient(
        uri="bolt://neo4j.invalid:7687",
        user="neo4j",
        password="f68-contract",
        driver_cls=_RefusingNeo4jDriverModule,
    ),
    lambda: FakeNeo4jClient(entities=[]),
]
_IDS = ["real-neo4j-client", "fake"]


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_resolve_unavailable_graph_returns_none(factory: Callable[[], Any]) -> None:
    """With the graph unreachable ``resolve`` returns ``None`` (not found).

    Sabotage proof: in ``Neo4jClient.find_by_name`` replace the
    ``if not self._driver: return []`` guard with
    ``if not self._driver: raise RuntimeError("graph down")``. Re-run: the
    real case fails with ``RuntimeError``. Restored.
    """
    resolver = _GraphEntityResolver(factory())
    assert isinstance(resolver, EntityResolverProtocol)

    assert resolver.resolve("Agent Alpha") is None


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_related_unavailable_graph_returns_empty_list(factory: Callable[[], Any]) -> None:
    """With the graph unreachable ``related`` returns ``[]``.

    Sabotage proof: in ``Neo4jClient.related_entities`` replace the
    ``if not self._driver: return []`` guard with
    ``if not self._driver: return [{"id": "stale"}]``. Re-run: the real case
    fails on the empty-list assertion. Restored.
    """
    resolver = _GraphEntityResolver(factory())

    assert resolver.related("agent-alpha") == []
    assert resolver.related("agent-alpha", rel_type="WORKS_WITH") == []
