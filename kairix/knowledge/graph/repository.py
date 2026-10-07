"""Neo4j-backed GraphRepository implementation.

Wraps Neo4jClient behind the GraphRepository protocol so callers depend
on the protocol rather than the concrete client.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kairix.knowledge.graph.client import Neo4jClient

logger = logging.getLogger(__name__)


class Neo4jGraphRepository:
    """GraphRepository implementation backed by Neo4j.

    Satisfies kairix.core.protocols.GraphRepository (read path: ``cypher``
    returns ``[]`` on failure) and, with ``raise_on_error=True``,
    kairix.core.curator.protocols.DrainGraphRepository (write path:
    ``cypher`` raises, so a rejected MERGE is never acknowledged as pushed).
    """

    def __init__(self, client: Neo4jClient, *, raise_on_error: bool = False) -> None:
        self._client = client
        self._raise_on_error = raise_on_error

    @property
    def available(self) -> bool:
        return self._client.available

    def find_entity(self, name: str) -> dict[str, Any] | None:
        """Find an entity by name (case-insensitive).

        Returns a dict with id, label, name, vault_path, summary, or None.
        """
        results = self._client.cypher(
            "MATCH (n) WHERE toLower(n.name) = toLower($name) "
            "RETURN n.id AS id, labels(n)[0] AS label, n.name AS name, "
            "n.vault_path AS vault_path, n.summary AS summary "
            "LIMIT 1",
            {"name": name},
        )
        return results[0] if results else None

    def entity_in_degrees(self) -> list[dict[str, Any]]:
        """Return all entities with their MENTIONS in-degree."""
        return self._client.cypher(
            "MATCH (n) WHERE n.vault_path IS NOT NULL AND n.vault_path <> '' "
            "OPTIONAL MATCH ()-[:MENTIONS]->(n) "
            "RETURN n.vault_path AS vault_path, n.name AS name, "
            "labels(n) AS labels, count(*) AS in_degree"
        )

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute an arbitrary Cypher query.

        Returns ``[]`` on failure, or raises when built with
        ``raise_on_error=True`` (the drain's write path).
        """
        if self._raise_on_error:
            return self._client.cypher_or_raise(query, params)
        return self._client.cypher(query, params)
