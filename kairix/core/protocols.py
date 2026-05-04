"""
Domain protocol definitions for kairix core boundaries.

Each Protocol represents the agreed interface between bounded contexts.
All protocols use @runtime_checkable so contract tests can verify
conformance via isinstance() checks.

Follows the same pattern as kairix.platform.llm.protocol.LLMBackend.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from kairix.core.search.intent import QueryIntent


@runtime_checkable
class IntentClassifier(Protocol):
    """Classifies a search query into a QueryIntent dispatch category."""

    def classify(self, query: str) -> QueryIntent: ...


@runtime_checkable
class DocumentRepository(Protocol):
    """Read/write interface for the document store (SQLite FTS5 backed)."""

    def search_fts(
        self,
        query: str,
        collections: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]: ...

    def get_by_path(self, path: str) -> dict[str, Any] | None: ...

    def get_chunk_dates(self, paths: list[str]) -> dict[str, str]: ...

    def insert_or_update(
        self,
        path: str,
        collection: str,
        title: str,
        content: str,
        content_hash: str,
    ) -> None: ...


@runtime_checkable
class GraphRepository(Protocol):
    """Interface for the entity graph (Neo4j backed)."""

    @property
    def available(self) -> bool: ...

    def find_entity(self, name: str) -> dict[str, Any] | None: ...

    def entity_in_degrees(self) -> list[dict[str, Any]]: ...

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...


@runtime_checkable
class VectorRepository(Protocol):
    """Interface for the vector index (usearch backed)."""

    def search(
        self,
        query_vec: list[float],
        k: int,
        collections: list[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    def add_vectors(self, items: list[tuple[str, list[float]]]) -> int: ...

    def count(self) -> int: ...


@runtime_checkable
class EmbeddingService(Protocol):
    """Text embedding interface (single and batch)."""

    def embed(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class FusionStrategy(Protocol):
    """Fuses BM25 and vector result lists into a single ranked list."""

    def fuse(self, bm25: list[Any], vec: list[Any]) -> list[Any]: ...


@runtime_checkable
class BoostStrategy(Protocol):
    """Post-fusion boost strategy (entity, procedural, temporal, etc.)."""

    def boost(self, results: list[Any], query: str, context: dict[str, Any]) -> list[Any]: ...


@runtime_checkable
class ScoringStrategy(Protocol):
    """Scores retrieved results against gold-standard documents."""

    def score(self, retrieved: list[str], gold: list[dict[str, Any]]) -> float: ...


@runtime_checkable
class SearchLogger(Protocol):
    """Structured logging for search and query events."""

    def log_search(self, event: dict[str, Any]) -> None: ...

    def log_query(self, event: dict[str, Any]) -> None: ...


@runtime_checkable
class CollectionResolver(Protocol):
    """Resolves the collection list for a search call given an agent + scope.

    Returning None means "no collection filter — search everything". Returning
    a non-empty list scopes BM25 and vector backends to those collection names.
    Returning an empty list is equivalent to None.

    Implementations should be constructed at the boundary (factory.py) with
    the loaded CollectionsConfig and any environment-derived extras, so that
    business logic only depends on the Protocol surface (G4: config at boundary).
    """

    def resolve(self, agent: str | None, scope: Any) -> list[str] | None: ...


@runtime_checkable
class AgentRegistry(Protocol):
    """Declarative agent → collection mapping for the multi-agent architecture.

    Used by:
      - CollectionResolver (resolves scope=all-agents / everything to the
        concrete list of agent collection names).
      - Embed pipeline (validates that writes under an agent's write_path
        are being performed by that agent).

    Implementations are constructed once at startup from the YAML config
    (G4: config at boundary). When the YAML has no ``agents:`` section the
    registry is empty and callers get explicit NotImplementedError for
    ALL_AGENTS / EVERYTHING scope so the misconfiguration is loud.
    """

    def list_agents(self) -> list[Any]: ...

    def collection_for(self, name: str) -> str: ...

    def validate_write(self, agent_name: str, path: str) -> bool: ...
