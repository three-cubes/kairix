"""
Tests for kairix.core.search.pipeline.SearchPipeline.

Tests compose the pipeline from fakes — no @patch, no monkey-patching.
Each test constructs a SearchPipeline with the exact fakes it needs.
"""

import pytest

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchPipeline, SearchResult
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeFusion,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
)

# ---------------------------------------------------------------------------
# Helper: build a test pipeline with sensible defaults
# ---------------------------------------------------------------------------


def _test_pipeline(**overrides) -> SearchPipeline:
    """Build a SearchPipeline with fake defaults. Override any component."""
    defaults = {
        "classifier": FakeClassifier(),
        "bm25": BM25SearchBackend(FakeDocumentRepository()),
        "vector": VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
        "graph": FakeGraphRepository(available=True),
        "fusion": FakeFusion(),
        "boosts": [],
        "logger": FakeSearchLogger(),
        "config": RetrievalConfig.defaults(),
    }
    defaults.update(overrides)
    return SearchPipeline(**defaults)


# ---------------------------------------------------------------------------
# Basic pipeline tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_returns_search_result():
    """SearchPipeline.search() returns a SearchResult."""
    pipeline = _test_pipeline()
    result = pipeline.search("test query")
    assert isinstance(result, SearchResult)


@pytest.mark.unit
def test_pipeline_classifies_intent():
    """Pipeline uses the classifier to determine intent."""
    pipeline = _test_pipeline(classifier=FakeClassifier(intent=QueryIntent.PROCEDURAL))
    result = pipeline.search("how to deploy")
    assert result.intent == QueryIntent.PROCEDURAL


@pytest.mark.unit
def test_pipeline_returns_bm25_results():
    """Pipeline returns BM25 results when documents match."""
    docs = [
        {
            "path": "deploy.md",
            "title": "Deploy Guide",
            "content": "how to deploy the app",
            "collection": "notes",
        },
    ]
    pipeline = _test_pipeline(
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
    )
    result = pipeline.search("deploy")
    assert result.bm25_count == 1


@pytest.mark.unit
def test_pipeline_returns_vector_results():
    """Pipeline returns vector results when vector repo has matches."""
    vec_results = [{"path": "semantic.md", "distance": 0.1, "collection": "c"}]
    pipeline = _test_pipeline(
        vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository(results=vec_results)),
    )
    result = pipeline.search("semantic query")
    assert result.vec_count == 1


@pytest.mark.unit
def test_pipeline_fuses_both_sources():
    """Pipeline fuses BM25 and vector results."""
    docs = [
        {
            "path": "a.md",
            "title": "A",
            "content": "architecture patterns",
            "collection": "c",
        }
    ]
    vec_results = [{"path": "b.md", "distance": 0.1, "collection": "c"}]
    pipeline = _test_pipeline(
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
        vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository(results=vec_results)),
    )
    result = pipeline.search("architecture")
    # FakeFusion concatenates: 1 from BM25 + 1 from vector = 2 fused
    assert result.fused_count == 2


@pytest.mark.unit
def test_pipeline_applies_boosts():
    """Pipeline applies each boost in the chain."""
    boost_calls = []

    class TrackingBoost:
        def boost(self, results, query, context):
            boost_calls.append(query)
            return results

    pipeline = _test_pipeline(boosts=[TrackingBoost(), TrackingBoost()])
    pipeline.search("test query")
    assert len(boost_calls) == 2
    assert all(q == "test query" for q in boost_calls)


@pytest.mark.unit
def test_pipeline_logs_search_event():
    """Pipeline logs a search event via SearchLogger."""
    fake_logger = FakeSearchLogger()
    pipeline = _test_pipeline(logger=fake_logger)
    pipeline.search("test query")
    assert len(fake_logger.events) == 1
    assert "query_hash" in fake_logger.events[0]
    assert "intent" in fake_logger.events[0]


@pytest.mark.unit
def test_pipeline_records_latency():
    """Pipeline records latency in the SearchResult."""
    pipeline = _test_pipeline()
    result = pipeline.search("latency test")
    assert result.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# Entity intent — Neo4j required
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_entity_intent_errors_when_graph_unavailable():
    """ENTITY intent returns error when graph is unavailable."""
    pipeline = _test_pipeline(
        classifier=FakeClassifier(intent=QueryIntent.ENTITY),
        graph=FakeGraphRepository(available=False),
    )
    result = pipeline.search("tell me about Acme Corp")
    assert result.intent == QueryIntent.ENTITY
    assert result.error != ""
    assert "Neo4j" in result.error
    assert result.results == []


@pytest.mark.unit
def test_pipeline_entity_intent_proceeds_when_graph_available():
    """ENTITY intent proceeds when graph is available."""
    pipeline = _test_pipeline(
        classifier=FakeClassifier(intent=QueryIntent.ENTITY),
        graph=FakeGraphRepository(available=True),
    )
    result = pipeline.search("tell me about Acme Corp")
    assert result.intent == QueryIntent.ENTITY
    assert result.error == ""


# ---------------------------------------------------------------------------
# Skip vector
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_skip_vector_returns_bm25_only():
    """skip_vector=True in config means no vector search is run."""
    embed_calls = []

    class TrackingEmbedding:
        def embed(self, text):
            embed_calls.append(text)
            return [0.01] * 10

        def embed_batch(self, texts):
            return [[0.01] * 10 for _ in texts]

    cfg = RetrievalConfig(skip_vector=True)
    docs = [{"path": "a.md", "title": "A", "content": "match", "collection": "c"}]
    pipeline = _test_pipeline(
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
        vector=VectorSearchBackend(TrackingEmbedding(), FakeVectorRepository()),
        config=cfg,
    )
    result = pipeline.search("match")
    assert result.vec_count == 0
    assert result.vec_failed is False
    # embed should NOT have been called
    assert len(embed_calls) == 0


# ---------------------------------------------------------------------------
# Vector failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_vec_failure_marks_vec_failed():
    """A genuine vector backend failure (raised exception) sets vec_failed=True.

    An empty result list is NOT a failure — it's a successful no-match —
    and must NOT trigger vec_failed (operator alerts would otherwise fire on
    every obscure query). See test_pipeline_contracts.py for the full
    distinction.
    """

    class _RaisingVectorRepo:
        def search(self, *_args, **_kwargs):
            raise RuntimeError("vector index corrupt")

        def search_with_filter(self, *_args, **_kwargs):
            raise RuntimeError("vector index corrupt")

    docs = [{"path": "a.md", "title": "A", "content": "match", "collection": "c"}]
    pipeline = _test_pipeline(
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
        vector=VectorSearchBackend(FakeEmbeddingService(), _RaisingVectorRepo()),
    )
    result = pipeline.search("match")
    assert result.vec_failed is True
    assert result.bm25_count == 1


# ---------------------------------------------------------------------------
# No logger
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_works_without_logger():
    """Pipeline works when logger is None."""
    pipeline = _test_pipeline(logger=None)
    result = pipeline.search("test")
    assert isinstance(result, SearchResult)


# ---------------------------------------------------------------------------
# Boost failure resilience
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_continues_when_boost_fails():
    """Pipeline continues when a boost raises an exception."""

    class FailingBoost:
        def boost(self, results, query, context):
            raise RuntimeError("boost failed")

    pipeline = _test_pipeline(boosts=[FailingBoost()])
    result = pipeline.search("test")
    assert isinstance(result, SearchResult)
    assert result.error == ""


# ---------------------------------------------------------------------------
# Collections pass-through
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_passes_collections():
    """Pipeline passes collection filter to backends."""
    docs = [
        {"path": "a.md", "title": "A", "content": "match", "collection": "notes"},
        {"path": "b.md", "title": "B", "content": "match", "collection": "archive"},
    ]
    pipeline = _test_pipeline(
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
    )
    result = pipeline.search("match", collections=["notes"])
    assert result.bm25_count == 1
    assert result.collections == ["notes"]
