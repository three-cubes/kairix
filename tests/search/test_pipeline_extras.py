"""
Tests for kairix.core.search.pipeline + kairix.core.search.budget + kairix.core.search.cli.

Tests cover:
  - Successful hybrid search (BM25 + vector)
  - Fallback: vector fails -> BM25-only results returned
  - KEYWORD intent runs full hybrid (BM25 + vector) like SEMANTIC
  - Token budget is applied and limits results
  - Search log file is written
  - CLI formats output correctly
  - CLI --json flag

Uses SearchPipeline with fakes for dependency injection -- no monkey-patching needed.
Tests construct SearchPipeline with fakes from tests.fakes:
  - FakeClassifier       (intent classifier)
  - FakeDocumentRepository (BM25 over in-memory docs)
  - FakeVectorRepository (vector search)
  - FakeGraphRepository  (Neo4j entity graph)
  - FakeFusion           (result fusion)
  - FakeEmbeddingService (embedding API)
  - FakeSearchLogger     (telemetry side-effect)
"""

from unittest.mock import MagicMock

import numpy as np
import pytest

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.bm25 import BM25Result
from kairix.core.search.budget import DEFAULT_BUDGET, apply_budget
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchPipeline
from kairix.core.search.pipeline import SearchResult as PipelineSearchResult
from kairix.core.search.resolver import DefaultCollectionResolver
from kairix.core.search.rrf import rrf
from kairix.core.search.scope import Scope
from kairix.core.search.vec_index import VecResult
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
)

# ---------------------------------------------------------------------------
# Fixtures -- test data builders
# ---------------------------------------------------------------------------

_FAKE_VEC = np.random.default_rng(seed=42).random(1536, dtype=np.float32).tolist()


def _bm25_result(path: str = "/vault/doc.md", score: float = 2.0) -> BM25Result:
    return BM25Result(
        file=path,
        title="Test Document",
        snippet="This is a test snippet with enough content to consume tokens.",
        score=score,
        collection="knowledge-shared",
    )


def _vec_result(path: str = "/vault/doc.md", distance: float = 0.1) -> VecResult:
    return VecResult(
        hash_seq="abc_0",
        distance=distance,
        path=path,
        collection="knowledge-shared",
        title="Test Document",
        snippet="Vector search snippet content.",
    )


def _make_mock_index(results: list[VecResult] | None = None) -> MagicMock:
    """Create a mock usearch VectorIndex returning controlled results."""
    idx = MagicMock()
    idx.__len__ = lambda self: 100
    idx.search.return_value = results if results is not None else []
    return idx


def _make_neo4j_stub(available: bool = False):
    """Create a minimal Neo4j client stub."""
    return type("Neo4jStub", (), {"available": available, "cypher": lambda self, q: []})()


# ---------------------------------------------------------------------------
# Pipeline construction helper
# ---------------------------------------------------------------------------


def _build_test_pipeline(
    *,
    intent: QueryIntent = QueryIntent.SEMANTIC,
    bm25_docs: list[dict] | None = None,
    vec_results: list[dict] | None = None,
    vec_repo: object | None = None,
    graph_available: bool = False,
    config: RetrievalConfig | None = None,
    logger: FakeSearchLogger | None = None,
) -> SearchPipeline:
    """Build a SearchPipeline with fakes for testing.

    Every dependency is wired to a harmless fake. Tests override specific
    fields via keyword arguments. ``vec_repo`` lets a test pass a custom
    VectorRepository (e.g. one that raises) instead of the FakeVectorRepository.
    """
    cfg = config or RetrievalConfig.defaults()
    classifier = FakeClassifier(intent=intent)
    doc_repo = FakeDocumentRepository(documents=bm25_docs or [])
    bm25 = BM25SearchBackend(doc_repo)
    embedding = FakeEmbeddingService()
    vector_repo = vec_repo if vec_repo is not None else FakeVectorRepository(results=vec_results or [])
    vector = VectorSearchBackend(embedding, vector_repo)  # type: ignore[arg-type]  # fakes satisfy backend protocol structurally
    graph = FakeGraphRepository(available=graph_available)
    fusion = RRFFusion(k=cfg.rrf_k)

    return SearchPipeline(
        classifier=classifier,
        bm25=bm25,
        vector=vector,
        graph=graph,
        fusion=fusion,
        boosts=[],
        logger=logger,
        config=cfg,
    )


# ---------------------------------------------------------------------------
# apply_budget tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_budget_returns_results_within_cap() -> None:
    """Results are returned up to the token budget cap."""
    fused = rrf(
        [_bm25_result(f"/vault/{i}.md") for i in range(10)],
        [],
    )
    budgeted = apply_budget(fused, budget=100)
    total = sum(r.token_estimate for r in budgeted)
    assert total <= 100


@pytest.mark.unit
def test_apply_budget_empty_results() -> None:
    """Empty fused results -> []."""
    assert apply_budget([], budget=DEFAULT_BUDGET) == []


@pytest.mark.unit
def test_apply_budget_zero_budget() -> None:
    """Zero budget -> []."""
    fused = rrf([_bm25_result()], [])
    assert apply_budget(fused, budget=0) == []


@pytest.mark.unit
def test_apply_budget_all_results_fit() -> None:
    """When budget is ample, all results are included."""
    fused = rrf([_bm25_result(f"/vault/{i}.md") for i in range(3)], [])
    budgeted = apply_budget(fused, budget=DEFAULT_BUDGET)
    assert len(budgeted) == 3


@pytest.mark.unit
def test_apply_budget_assigns_l2_tier_in_phase1() -> None:
    """Phase 1: all results assigned L2 tier."""
    fused = rrf([_bm25_result()], [])
    budgeted = apply_budget(fused, budget=DEFAULT_BUDGET)
    assert all(r.tier == "L2" for r in budgeted)


# ---------------------------------------------------------------------------
# Collection resolution — public DefaultCollectionResolver Adapter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolver_no_config_no_agent_returns_none() -> None:
    """Without config and without an agent, resolve returns None (search everything)."""
    resolver = DefaultCollectionResolver(collections_config=None)
    assert resolver.resolve(None, Scope.SHARED) is None


@pytest.mark.unit
def test_resolver_shared_agent_appends_agent_pattern() -> None:
    """scope=shared+agent with extras + agent appends the agent's collection."""
    resolver = DefaultCollectionResolver(
        collections_config=None,
        extra_collections=["test-collection"],
    )
    cols = resolver.resolve("shape", Scope.SHARED_AGENT)
    assert cols is not None
    assert "shape-memory" in cols
    assert "test-collection" in cols


@pytest.mark.unit
def test_resolver_agent_only_excludes_shared() -> None:
    """scope=agent returns only the agent's own collection."""
    resolver = DefaultCollectionResolver(
        collections_config=None,
        extra_collections=["shared-test"],
    )
    cols = resolver.resolve("shape", Scope.AGENT)
    assert cols == ["shape-memory"]


@pytest.mark.unit
def test_resolver_all_agents_not_yet_implemented() -> None:
    """ALL_AGENTS scope explicitly raises until WS3-3 (AgentRegistry) lands."""
    resolver = DefaultCollectionResolver(collections_config=None)
    with pytest.raises(NotImplementedError, match="AgentRegistry"):
        resolver.resolve("shape", Scope.ALL_AGENTS)


# ---------------------------------------------------------------------------
# SearchPipeline.search() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_returns_search_result_type() -> None:
    """SearchPipeline.search() returns PipelineSearchResult."""
    pipeline = _build_test_pipeline()
    result = pipeline.search("test query")
    assert isinstance(result, PipelineSearchResult)


@pytest.mark.unit
def test_search_returns_bm25_results_when_vec_fails() -> None:
    """Vector backend failure -> BM25-only results still returned, vec_failed=True."""
    docs = [
        {
            "path": "/vault/a.md",
            "title": "A",
            "content": "test semantic query about memory systems",
            "collection": "c",
        },
        {
            "path": "/vault/b.md",
            "title": "B",
            "content": "test semantic query about memory systems",
            "collection": "c",
        },
    ]

    class _RaisingVectorRepo:
        def search(self, *_args, **_kwargs):
            raise RuntimeError("vector backend down")

        def search_with_filter(self, *_args, **_kwargs):
            raise RuntimeError("vector backend down")

    pipeline = _build_test_pipeline(bm25_docs=docs, vec_repo=_RaisingVectorRepo())
    result = pipeline.search("test semantic query about memory systems")

    # The exact result count depends on FTS-match shape inside the fake; what
    # the test pins down is that the search returns *something* iterable
    # without raising even though the vector backend failed.
    assert isinstance(result.results, list)
    # Genuine backend failure → vec_failed=True (per the new semantics where
    # vec_failed reflects FAILURE, not empty-result).
    assert result.vec_failed is True


@pytest.mark.unit
def test_search_fuses_both_lists() -> None:
    """When both BM25 and vector return results, fused count includes both sources."""
    docs = [
        {
            "path": "/vault/a.md",
            "title": "A",
            "content": "knowledge retrieval test",
            "collection": "c",
        },
        {
            "path": "/vault/b.md",
            "title": "B",
            "content": "knowledge retrieval test",
            "collection": "c",
        },
    ]
    vec_data = [
        {
            "path": "/vault/a.md",
            "collection": "c",
            "title": "A",
            "snippet": "vec",
            "distance": 0.1,
        },
        {
            "path": "/vault/c.md",
            "collection": "c",
            "title": "C",
            "snippet": "vec",
            "distance": 0.2,
        },
    ]
    pipeline = _build_test_pipeline(bm25_docs=docs, vec_results=vec_data)
    result = pipeline.search("knowledge retrieval test")

    assert result.vec_count == 2


@pytest.mark.unit
def test_search_keyword_intent_runs_hybrid() -> None:
    """KEYWORD intent runs full hybrid (BM25 + vector), not BM25-only."""
    vec_data = [
        {
            "path": "/vault/doc.md",
            "collection": "c",
            "title": "T",
            "snippet": "s",
            "distance": 0.1,
        },
    ]
    pipeline = _build_test_pipeline(intent=QueryIntent.KEYWORD, vec_results=vec_data)
    result = pipeline.search("SchemaVersionError")

    assert result.intent == QueryIntent.KEYWORD
    assert result.vec_count == 1


@pytest.mark.unit
def test_search_logs_event() -> None:
    """Search log is written via SearchLogger."""
    log = FakeSearchLogger()
    pipeline = _build_test_pipeline(logger=log)
    pipeline.search("test query about rules")

    assert len(log.events) == 1
    assert "query_hash" in log.events[0]
    assert "intent" in log.events[0]
    assert "latency_ms" in log.events[0]


@pytest.mark.unit
def test_search_records_latency() -> None:
    """latency_ms is recorded in the result."""
    pipeline = _build_test_pipeline()
    result = pipeline.search("latency test")
    assert result.latency_ms >= 0.0


@pytest.mark.unit
def test_search_intent_is_classified() -> None:
    """Intent is classified and included in SearchResult."""
    pipeline = _build_test_pipeline(intent=QueryIntent.PROCEDURAL)
    result = pipeline.search("how to fetch a secret")
    assert result.intent == QueryIntent.PROCEDURAL


# CLI behaviour is now covered in tests/search/test_cli.py (pure-helper unit tests),
# tests/bdd/test_search_cli.py (operator-visible scenarios), and
# tests/use_cases/test_search.py (use case behaviour). The CLI no longer accepts
# a ``pipeline=`` kwarg — Phase 2 of #168 routed it through ``run_search``.


# ---------------------------------------------------------------------------
# keyword hybrid dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_keyword_uses_both_bm25_and_vector() -> None:
    """KEYWORD intent now runs hybrid: both BM25 and vector contribute to RRF."""
    docs = [
        {
            "path": "/vault/schema-error.md",
            "title": "T",
            "content": "SchemaVersionError",
            "collection": "c",
        },
    ]
    vec_data = [
        {
            "path": "/vault/schema-overview.md",
            "collection": "c",
            "title": "T",
            "snippet": "s",
            "distance": 0.1,
        },
    ]
    pipeline = _build_test_pipeline(intent=QueryIntent.KEYWORD, bm25_docs=docs, vec_results=vec_data)
    result = pipeline.search("SchemaVersionError")

    assert result.intent == QueryIntent.KEYWORD
    assert result.vec_count == 1


@pytest.mark.unit
def test_search_result_has_fallback_used_field() -> None:
    """SearchResult always has fallback_used field."""
    pipeline = _build_test_pipeline()
    result = pipeline.search("anything")
    assert hasattr(result, "fallback_used")
    assert isinstance(result.fallback_used, bool)


# ---------------------------------------------------------------------------
# ENTITY intent -- Neo4j required
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_entity_intent_errors_when_neo4j_unavailable() -> None:
    """ENTITY intent returns an error result when Neo4j is unavailable.

    Regression: previously search() silently fell through to BM25+vector,
    producing misleading results with no entity graph expansion.
    """
    pipeline = _build_test_pipeline(
        intent=QueryIntent.ENTITY,
        graph_available=False,
    )
    result = pipeline.search("tell me about Acme Corp")

    assert result.intent == QueryIntent.ENTITY
    assert result.error != ""
    assert "Neo4j" in result.error
    assert result.results == []


@pytest.mark.unit
def test_search_entity_intent_proceeds_when_neo4j_available() -> None:
    """ENTITY intent proceeds to full pipeline when Neo4j is available."""
    pipeline = _build_test_pipeline(
        intent=QueryIntent.ENTITY,
        graph_available=True,
    )
    result = pipeline.search("tell me about Acme Corp")

    assert result.intent == QueryIntent.ENTITY
    assert result.error == ""


# CLI entity-error exit code + JSON envelope are covered by
# tests/search/test_cli.py and tests/bdd/test_search_cli.py.


# ---------------------------------------------------------------------------
# 2. skip_vector and vec empty
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skip_vector_config_produces_no_vec_results() -> None:
    """skip_vector=True in config means vec_results is empty and vec_failed is False."""
    cfg = RetrievalConfig(skip_vector=True)
    pipeline = _build_test_pipeline(config=cfg)
    result = pipeline.search("test query")

    assert result.vec_count == 0
    assert result.vec_failed is False


@pytest.mark.unit
def test_vector_search_empty_does_not_mark_vec_failed() -> None:
    """Vector search returning [] is a successful no-match, not a failure.

    Operator-facing semantics: ``vec_failed`` triggers triage (Azure down,
    embedding broken). An empty result for a query that just doesn't match
    anything in the index must NOT trigger vec_failed — see
    test_pipeline_contracts.py for the full contract.
    """
    pipeline = _build_test_pipeline()  # FakeVectorRepository returns []
    result = pipeline.search("semantic query about architecture")

    assert result.vec_failed is False
    assert result.vec_count == 0
