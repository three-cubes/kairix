"""Fake Neo4j client for tests — no real Neo4j connection required."""

from __future__ import annotations

_DEFAULT_ENTITIES: list[dict] = [
    {
        "id": "openclaw",
        "name": "OpenClaw",
        "label": "Organisation",
        "vault_path": "entities/openclaw.md",
        "summary": "AI agent platform",
    },
    {
        "id": "acme-partners",
        "name": "Acme Partners",
        "label": "Organisation",
        "vault_path": "entities/acme-partners.md",
        "summary": "Microsoft services partner",
    },
    {
        "id": "alice-smith",
        "name": "Alice Smith",
        "label": "Person",
        "vault_path": "entities/alice-smith.md",
        "summary": "Founder",
    },
    {
        "id": "kairix-project",
        "name": "Kairix",
        "label": "Project",
        "vault_path": "entities/kairix.md",
        "summary": "Hybrid search memory system",
    },
    {
        "id": "example-org",
        "name": "Example Corp",
        "label": "Organisation",
        "vault_path": "entities/example-corp.md",
        "summary": "Technology company",
    },
]


class FakeNeo4jClient:
    """Fake Neo4jClient satisfying the Neo4jClient interface. No real Neo4j required."""

    available: bool = True

    def __init__(self, entities: list[dict] | None = None) -> None:
        self._entities: list[dict] = entities if entities is not None else list(_DEFAULT_ENTITIES)
        # upsert_node call recorder + return-value knob.
        self.upsert_node_calls: list[dict] = []
        self.upsert_node_returns: bool = True

    def cypher(self, query: str, params: dict | None = None) -> list[dict]:
        """Pattern-match query string and return appropriate fake results."""
        if "vault_path IS NULL" in query:
            return []
        if "summary IS NULL" in query:
            return []
        if "COUNT(*)" in query:
            return [{"label": "Organisation", "cnt": len(self._entities)}]
        if "last_seen IS NOT NULL" in query:
            return []
        if "labels(n) AS labels" in query and "count(n) AS count" in query:
            # `kairix entity count` (#259): one row per entity, primary
            # label first. The CLI sums in Python.
            return [{"labels": [e.get("label", "")], "count": 1} for e in self._entities]
        return self._entities

    def find_by_name(self, name: str) -> list[dict]:
        """Case-insensitive match against stored entities by 'name' field."""
        name_lower = name.lower()
        return [e for e in self._entities if e.get("name", "").lower() == name_lower]

    def related_entities(self, entity_id: str, max_hops: int = 2) -> list[dict]:
        """Return related entities — always empty in the fake."""
        return []

    def find_entity(self, name: str) -> dict | None:
        """Find entity by name (case-insensitive). Satisfies GraphRepository protocol."""
        name_lower = name.lower()
        for e in self._entities:
            if e.get("name", "").lower() == name_lower:
                return e
        return None

    def entity_in_degrees(self) -> list[dict]:
        """Return all entities with in-degree data. Satisfies GraphRepository protocol."""
        return [
            {
                "vault_path": e.get("vault_path", ""),
                "name": e.get("name", ""),
                "labels": [e.get("label", "")],
                "in_degree": 1,
            }
            for e in self._entities
        ]

    def upsert_organisation(self, **kwargs) -> dict:
        """Stub — no-op in fake."""
        return kwargs

    def upsert_edge(self, **kwargs) -> None:
        """Stub — no-op in fake."""
        return None

    def upsert_node(self, *args, **kwargs) -> bool:
        """Stub — record the call and return ``self.upsert_node_returns``."""
        self.upsert_node_calls.append({"args": args, "kwargs": kwargs})
        return self.upsert_node_returns
