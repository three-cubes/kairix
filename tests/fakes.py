"""
Fake implementations of core domain protocols for testing.

Each fake is:
  - Simple (in-memory data structures)
  - Configurable (accepts test data in constructor)
  - Protocol-compliant (implements all methods from kairix.core.protocols)

These fakes are the canonical test doubles for contract and unit tests.
"""

from __future__ import annotations

from typing import Any

from kairix.core.search.intent import QueryIntent


class FakeClassifier:
    """Fake IntentClassifier that returns a fixed intent."""

    def __init__(self, intent: QueryIntent = QueryIntent.SEMANTIC) -> None:
        self.intent = intent

    def classify(self, query: str) -> QueryIntent:
        return self.intent


class FakeDocumentRepository:
    """In-memory document store keyed by path."""

    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self._docs: dict[str, dict[str, Any]] = {}
        for doc in documents or []:
            path = doc.get("path", "")
            self._docs[path] = doc

    def search_fts(
        self,
        query: str,
        collections: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        results = []
        query_lower = query.lower()
        for doc in self._docs.values():
            if collections and doc.get("collection") not in collections:
                continue
            content = doc.get("content", "") + " " + doc.get("title", "")
            if query_lower in content.lower():
                results.append(doc)
            if len(results) >= limit:
                break
        return results

    def get_by_path(self, path: str) -> dict[str, Any] | None:
        return self._docs.get(path)

    def get_chunk_dates(self, paths: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in paths:
            doc = self._docs.get(path)
            if doc and "chunk_date" in doc:
                result[path] = doc["chunk_date"]
        return result

    def insert_or_update(
        self,
        path: str,
        collection: str,
        title: str,
        content: str,
        content_hash: str,
    ) -> None:
        self._docs[path] = {
            "path": path,
            "collection": collection,
            "title": title,
            "content": content,
            "content_hash": content_hash,
        }


class FakeGraphRepository:
    """In-memory entity graph keyed by name."""

    def __init__(
        self,
        entities: list[dict[str, Any]] | None = None,
        available: bool = True,
    ) -> None:
        self._available = available
        self._entities: dict[str, dict[str, Any]] = {}
        for entity in entities or []:
            name = entity.get("name", entity.get("id", ""))
            self._entities[name.lower()] = entity

    @property
    def available(self) -> bool:
        return self._available

    def find_entity(self, name: str) -> dict[str, Any] | None:
        return self._entities.get(name.lower())

    def entity_in_degrees(self) -> list[dict[str, Any]]:
        return list(self._entities.values())

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return list(self._entities.values())


class FakeVectorRepository:
    """In-memory vector store that returns configured results."""

    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self._results: list[dict[str, Any]] = results or []
        self._vectors: list[tuple[str, list[float]]] = []

    def search(
        self,
        query_vec: list[float],
        k: int,
        collections: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if collections:
            filtered = [r for r in self._results if r.get("collection") in collections]
            return filtered[:k]
        return self._results[:k]

    def add_vectors(self, items: list[tuple[str, list[float]]]) -> int:
        self._vectors.extend(items)
        return len(items)

    def count(self) -> int:
        return len(self._vectors) + len(self._results)


class FakeEmbeddingService:
    """Deterministic embedding service that returns a fixed vector."""

    def __init__(self, vector: list[float] | None = None, dim: int = 1536) -> None:
        self._vector = vector or [0.01] * dim

    def embed(self, text: str) -> list[float]:
        return list(self._vector)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vector) for _ in texts]


class FakeEmbedProvider:
    """Deterministic EmbedProvider — captures call args for assertion.

    Implements ``kairix.platform.llm.embed_provider.EmbedProvider``:
    ``embed_batch(texts, *, model, dims) -> list[list[float]]``.
    """

    def __init__(self, vector: list[float] | None = None, dim: int = 3) -> None:
        self._vector = vector or [0.0, 0.6, 0.8]
        self._dim = dim
        self.calls: list[dict[str, Any]] = []

    def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
        self.calls.append({"texts": list(texts), "model": model, "dims": dims})
        return [list(self._vector) for _ in texts]


class FakeFusion:
    """Pass-through fusion: concatenates BM25 and vector results."""

    def fuse(self, bm25: list[Any], vec: list[Any]) -> list[Any]:
        return bm25 + vec


class FakeBoost:
    """No-op boost: returns results unmodified."""

    def boost(self, results: list[Any], query: str, context: dict[str, Any]) -> list[Any]:
        return results


class FakeScorer:
    """Fixed-score scorer for testing."""

    def __init__(self, score: float = 1.0) -> None:
        self._score = score

    def score(self, retrieved: list[str], gold: list[dict[str, Any]]) -> float:
        return self._score


class FakeSearchLogger:
    """In-memory search logger that captures events."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_search(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def log_query(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class FakeCollectionResolver:
    """In-memory CollectionResolver that returns configured lists per (agent, scope) key.

    Constructed with a mapping from (agent_or_None, scope_value) tuples to
    collection lists. Anything not in the map returns None.
    """

    def __init__(self, by_key: dict[tuple[str | None, str], list[str] | None] | None = None) -> None:
        self._by_key: dict[tuple[str | None, str], list[str] | None] = dict(by_key or {})

    def resolve(self, agent: str | None, scope: Any) -> list[str] | None:
        scope_value = scope.value if hasattr(scope, "value") else str(scope)
        return self._by_key.get((agent, scope_value))


class FakeAgentRegistry:
    """In-memory AgentRegistry constructed from a list of agent dicts.

    Each entry is a dict with at least ``name`` and ``collection``; optional
    ``write_path`` and ``read_only`` mirror AgentDef in the production
    Adapter. Tests use this rather than ConfigDrivenAgentRegistry so they
    don't have to construct the full YAML pipeline.
    """

    def __init__(self, agents: list[dict[str, Any]] | None = None) -> None:
        self._agents = list(agents or [])

    def list_agents(self) -> list[Any]:
        # Returns dict-like entries; resolver only needs .collection attribute,
        # so wrap each in a minimal namespace-style object.
        class _Agent:
            def __init__(self, d: dict[str, Any]) -> None:
                self.name = d["name"]
                self.collection = d.get("collection", f"{d['name']}-memory")
                self.write_path = d.get("write_path", "")
                self.read_only = d.get("read_only", False)

        return [_Agent(a) for a in self._agents]

    def collection_for(self, name: str) -> str:
        for a in self._agents:
            if a["name"] == name:
                return str(a.get("collection", f"{name}-memory"))
        raise KeyError(f"unknown agent {name!r}")

    def validate_write(self, agent_name: str, path: str) -> bool:
        for a in self._agents:
            if a["name"] == agent_name and not a.get("read_only", False):
                wp = a.get("write_path", "")
                if not wp:
                    return False
                return path == wp or path.startswith(wp.rstrip("/") + "/")
        return False
