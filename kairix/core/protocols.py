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


# ---------------------------------------------------------------------------
# Eval-module protocols (#143 Phase 1 — paired with FakeXxx in tests/fakes.py)
#
# These four protocols define the boundary between the eval module and the
# external systems it depends on (LLM chat, vector retrieval, the corpus
# itself). Phase 2a/2b refactor judge.py / hybrid_sweep.py / generate.py /
# gold_builder.py to consume these protocols via constructor injection,
# eliminating the *_fn=None test-substitution kwargs scattered across the
# eval surface today.
# ---------------------------------------------------------------------------


@runtime_checkable
class ChatBackend(Protocol):
    """LLM chat-completion surface — substitutable across Azure / OpenRouter / fakes.

    Wraps the call shape ``kairix._azure.chat_completion`` / OpenAI-API
    chat completions use. The eval module's LLM judge and query generator
    consume this protocol so test code can inject a `FakeChatBackend`
    rather than reaching past `_call_llm` into module-level state.

    Implementations are expected to:
      - Block until the response is complete (no streaming surface here).
      - Apply their own retry / rate-limit policy internally.
      - Raise on credential failure rather than returning empty content.
    """

    def complete(
        self,
        prompt: str,
        *,
        api_key: str,
        endpoint: str,
        deployment: str,
        system: str | None = None,
        temperature: float = 0.0,
        timeout_s: float = 30.0,
    ) -> str: ...


@runtime_checkable
class LLMJudge(Protocol):
    """Pairwise / pointwise relevance judge over (query, document) pairs.

    The judge labels each candidate document for a query with a 0/1/2
    relevance grade. Production implementations call out to an LLM via
    `ChatBackend`; tests use `FakeLLMJudge` returning pre-configured grades.

    Implementations are expected to:
      - Never raise — return all-zero grades on any error.
      - Shuffle candidate order before judging to prevent positional bias.
      - Return a `JudgeResult`-shaped value (query, grades, shuffle_order,
        judge_model, calibration_passed).
    """

    def grade(
        self,
        query: str,
        candidates: list[tuple[str, str]],
        *,
        runs: int = 1,
    ) -> Any: ...

    def calibrate(self) -> bool: ...


@runtime_checkable
class QueryGenerator(Protocol):
    """Synthesises retrieval evaluation queries from a corpus document.

    Production implementations call out to an LLM to generate diverse,
    intent-tagged queries that the source document would be the primary
    answer for. Tests use `FakeQueryGenerator` returning pre-configured
    queries.

    Implementations are expected to:
      - Return between 0 and `n` queries (LLM may produce fewer).
      - Tag each query with one of the configured intent categories.
      - Sanitise the source document content against prompt injection.
    """

    def generate(
        self,
        title: str,
        body: str,
        *,
        n: int,
        categories: list[str],
    ) -> list[Any]: ...


@runtime_checkable
class Retriever(Protocol):
    """Hybrid-search facade for sweep / benchmark / gold-builder callers.

    The eval pipeline retrieves candidate documents via this protocol so
    sweep configurations can be tested against `FakeRetriever` returning
    pre-configured rankings. Production implementations delegate to the
    `SearchPipeline.search` surface but accept the eval-shaped argument
    signature directly.

    Implementations are expected to:
      - Return results in fused-rank order (best first).
      - Honour the `collections` filter when supplied.
      - Surface vec-failed state (e.g. via a `vec_failed: bool` attribute
        on the result) so callers can distinguish "no results" from
        "vector index unavailable".
    """

    def retrieve(
        self,
        query: str,
        *,
        collections: list[str] | None = None,
        cfg: Any = None,
    ) -> Any: ...
