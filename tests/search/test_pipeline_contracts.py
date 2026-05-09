"""Contract-first tests for kairix.core.search.pipeline.SearchPipeline.

Read the docstrings (SearchPipeline.search, SearchResult fields, the
SearchLogger event-shape docs in kairix/core/search/logger.py), write
tests asserting the claims, then run against the live code.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchPipeline, SearchResult
from kairix.core.search.scope import Scope
from tests.fakes import (
    FakeClassifier,
    FakeCollectionResolver,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeFusion,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
)

# ---------------------------------------------------------------------------
# Pipeline-builder helper
# ---------------------------------------------------------------------------


def _build_pipeline(**overrides: Any) -> SearchPipeline:
    defaults: dict[str, Any] = {
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
# Contract: never raises — even when EVERY component raises, returns a SearchResult.
# Docstring: "Never raises — returns SearchResult with empty results on any failure."
# ---------------------------------------------------------------------------


class _RaisingClassifier:
    def classify(self, _query: str) -> QueryIntent:
        raise RuntimeError("classifier blew up")


class _RaisingDocumentRepository:
    """Doc repo whose every method raises — ensures BM25 backend swallows it."""

    def search_fts(self, *_args: Any, **_kwargs: Any) -> list[dict]:
        raise RuntimeError("bm25 backend down")

    def search_fts_weighted(self, *_args: Any, **_kwargs: Any) -> list[dict]:
        raise RuntimeError("bm25 backend down")


class _RaisingFusion:
    def fuse(self, _bm25: list, _vec: list) -> list:
        raise RuntimeError("fusion blew up")


class _RaisingBoost:
    def boost(self, _fused: list, _query: str, _ctx: dict) -> list:
        raise RuntimeError("boost blew up")


class _RaisingLogger:
    def log_search(self, _event: dict) -> None:
        raise RuntimeError("logger blew up")

    def log_query(self, _event: dict) -> None:  # pragma: no cover — pipeline doesn't call log_query
        raise RuntimeError("logger blew up")


@pytest.mark.unit
def test_pipeline_search_never_raises_when_classifier_raises() -> None:
    pipeline = _build_pipeline(classifier=_RaisingClassifier())
    result = pipeline.search("anything")
    # Docstring: "returns SearchResult with empty results on any failure".
    assert isinstance(result, SearchResult)
    # Classifier failed → defaults to SEMANTIC per the code.
    assert result.intent == QueryIntent.SEMANTIC


@pytest.mark.unit
def test_pipeline_search_never_raises_when_bm25_backend_raises() -> None:
    pipeline = _build_pipeline(bm25=BM25SearchBackend(_RaisingDocumentRepository()))
    result = pipeline.search("anything")
    assert isinstance(result, SearchResult)
    assert result.bm25_count == 0


@pytest.mark.unit
def test_pipeline_search_never_raises_when_fusion_raises() -> None:
    """Fusion is currently NOT inside a try/except in SearchPipeline.search.
    The docstring says "Never raises". If fusion raises and the pipeline
    propagates, the docstring contract is broken.
    """
    pipeline = _build_pipeline(fusion=_RaisingFusion())
    # Per the "Never raises" guarantee, this must not propagate.
    result = pipeline.search("anything")
    assert isinstance(result, SearchResult)


@pytest.mark.unit
def test_pipeline_search_never_raises_when_logger_raises() -> None:
    pipeline = _build_pipeline(logger=_RaisingLogger())
    result = pipeline.search("anything")
    assert isinstance(result, SearchResult)


@pytest.mark.unit
def test_pipeline_search_continues_through_chain_when_one_boost_raises() -> None:
    """A failed boost is logged and skipped — subsequent boosts still run."""

    class _CountingBoost:
        def __init__(self) -> None:
            self.calls = 0

        def boost(self, fused: list, _query: str, _ctx: dict) -> list:
            self.calls += 1
            return fused

    counting = _CountingBoost()
    pipeline = _build_pipeline(boosts=[_RaisingBoost(), counting])

    result = pipeline.search("anything")
    assert isinstance(result, SearchResult)
    # The counting boost ran even though the prior one raised.
    assert counting.calls == 1


# ---------------------------------------------------------------------------
# Contract: ENTITY intent without graph returns SearchResult with operator
# diagnostic in result.error.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_returns_neo4j_diagnostic_when_entity_intent_lacks_graph() -> None:
    pipeline = _build_pipeline(
        classifier=FakeClassifier(intent=QueryIntent.ENTITY),
        graph=FakeGraphRepository(available=False),
    )
    result = pipeline.search("tell me about Acme Corp")

    assert result.intent == QueryIntent.ENTITY
    assert result.results == []
    # Operator-facing message must name Neo4j and the env vars to investigate.
    assert "Neo4j" in result.error
    assert "KAIRIX_NEO4J" in result.error


# ---------------------------------------------------------------------------
# Contract: resolver NotImplementedError surfaces as result.error, not crash.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_returns_resolver_error_message_when_resolver_raises_not_implemented() -> None:
    class _NotImplementedResolver:
        def resolve(self, _agent: str | None, _scope: Scope) -> list[str]:
            raise NotImplementedError("scope=all-agents requires an agent registry — none configured")

    pipeline = _build_pipeline(resolver=_NotImplementedResolver())
    result = pipeline.search("anything")

    assert result.results == []
    assert "agent registry" in result.error


# ---------------------------------------------------------------------------
# Contract: collections handling.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_search_uses_explicit_collections_unchanged_skipping_resolver() -> None:
    """When collections is passed explicitly, the resolver is NOT consulted."""
    resolver_calls: list[tuple] = []

    class _SpyResolver:
        def resolve(self, agent, scope):
            resolver_calls.append((agent, scope))
            return ["from-resolver"]

    pipeline = _build_pipeline(resolver=_SpyResolver())
    result = pipeline.search("q", collections=["caller-explicit"])

    assert result.collections == ["caller-explicit"]
    assert resolver_calls == [], "resolver must not be consulted when collections is passed explicitly"


@pytest.mark.unit
def test_pipeline_search_calls_resolver_when_collections_is_none() -> None:
    pipeline = _build_pipeline(
        resolver=FakeCollectionResolver(by_key={(None, Scope.SHARED_AGENT.value): ["resolved-collection"]}),
    )
    result = pipeline.search("q")  # collections defaults to None
    assert result.collections == ["resolved-collection"]


@pytest.mark.unit
def test_pipeline_search_collections_field_is_empty_list_when_none_resolved() -> None:
    """No resolver, no explicit collections → result.collections == []
    (NOT None — the docstring's SearchResult dataclass default is an
    empty list, and downstream consumers iterate this field).
    """
    pipeline = _build_pipeline(resolver=None)
    result = pipeline.search("q")
    assert result.collections == []


# ---------------------------------------------------------------------------
# Contract: vec_failed reflects a genuine FAILURE, not an empty response.
#
# kairix/core/search/logger.py line 17 documents the field as
# "whether vector search FAILED for this query". An empty result list is
# a successful no-match — not a failure.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_vec_failed_is_false_when_vector_returns_empty_results_without_raising() -> None:
    """A vector backend that returns [] (no matches) but does not raise must
    NOT report vec_failed=True — the contract per logger.py docstring is
    "did vector search FAIL", and an empty result is a successful no-match.
    """
    # FakeVectorRepository defaults to no results; FakeEmbeddingService returns
    # a vector. So vector.search returns [] without raising.
    pipeline = _build_pipeline()
    result = pipeline.search("query that matches nothing")

    assert result.vec_count == 0
    assert result.vec_failed is False, (
        "vec_failed must distinguish 'failed' from 'no results' — operator-facing "
        "alerts should not fire when the vector query simply has no matches"
    )


@pytest.mark.unit
def test_pipeline_vec_failed_is_true_when_vector_backend_raises() -> None:
    """When vector.search raises, vec_failed=True (the genuine failure case)."""

    class _RaisingVectorRepo:
        def search(self, *_args: Any, **_kwargs: Any) -> list:
            raise RuntimeError("vector index corrupt")

        def search_with_filter(self, *_args: Any, **_kwargs: Any) -> list:
            raise RuntimeError("vector index corrupt")

    pipeline = _build_pipeline(vector=VectorSearchBackend(FakeEmbeddingService(), _RaisingVectorRepo()))
    result = pipeline.search("anything")
    assert result.vec_failed is True


@pytest.mark.unit
def test_pipeline_vec_failed_is_false_when_skip_vector_is_configured() -> None:
    """When config.skip_vector is True, vector wasn't even attempted —
    vec_failed must be False (it's a skip, not a failure).
    """
    cfg = RetrievalConfig(skip_vector=True)  # frozen dataclass — construct with skip_vector
    pipeline = _build_pipeline(config=cfg)
    result = pipeline.search("anything")
    assert result.vec_failed is False


# ---------------------------------------------------------------------------
# Contract: fallback_used is "BM25 returned nothing AND vector did".
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_fallback_used_is_true_when_bm25_empty_and_vector_returns_results() -> None:
    """fallback_used = "BM25 found nothing but vector did — we're falling
    back to vector". Test setup: BM25 backend returns []; vector backend
    returns one result.
    """

    class _StaticVectorRepo:
        def search(self, *_args: Any, **_kwargs: Any) -> list:
            return [{"path": "/v.md", "title": "v", "snippet": "s", "collection": "c", "distance": 0.1}]

        def search_with_filter(self, *_args: Any, **_kwargs: Any) -> list:
            return [{"path": "/v.md", "title": "v", "snippet": "s", "collection": "c", "distance": 0.1}]

    pipeline = _build_pipeline(vector=VectorSearchBackend(FakeEmbeddingService(), _StaticVectorRepo()))
    result = pipeline.search("anything")

    assert result.bm25_count == 0
    assert result.vec_count == 1
    assert result.fallback_used is True


@pytest.mark.unit
def test_pipeline_fallback_used_is_false_when_bm25_returned_results() -> None:
    """When BM25 found something, we're not in fallback mode — even if vector
    also returned results.
    """
    docs = [{"path": "x.md", "title": "x", "content": "match", "collection": "c"}]
    pipeline = _build_pipeline(bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)))
    result = pipeline.search("match")

    assert result.bm25_count >= 1
    assert result.fallback_used is False


# ---------------------------------------------------------------------------
# Contract: log event carries the documented fields (per logger.py docstring).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_log_event_carries_every_field_documented_in_logger_docstring() -> None:
    """logger.py docstring lists: ts, query_hash, intent, agent, scope,
    collections_searched, bm25_count, vec_count, fused_count, total_tokens,
    latency_ms, vec_failed, fallback_used. Each must appear in the event
    that SearchPipeline emits.
    """
    fake_log = FakeSearchLogger()
    pipeline = _build_pipeline(logger=fake_log)
    pipeline.search("anything", agent="shape", scope=Scope.SHARED_AGENT)

    assert fake_log.events, "expected the logger to receive at least one event"
    event = fake_log.events[0]
    expected_fields = {
        "ts",
        "query_hash",
        "intent",
        "agent",
        "scope",
        "collections_searched",
        "bm25_count",
        "vec_count",
        "fused_count",
        "total_tokens",
        "latency_ms",
        "vec_failed",
        "fallback_used",
    }
    missing = expected_fields - set(event.keys())
    assert not missing, f"log event missing documented fields: {sorted(missing)}; got keys: {sorted(event.keys())}"


@pytest.mark.unit
def test_pipeline_log_event_query_hash_is_12_char_hex_prefix_of_sha256() -> None:
    """The privacy contract: events log a hash, not the raw query. The hash
    must be a 12-char hex prefix of sha256(query) so it's stable across runs
    and operator-grep-able.
    """
    import hashlib

    fake_log = FakeSearchLogger()
    pipeline = _build_pipeline(logger=fake_log)
    query = "what is the deploy procedure"
    pipeline.search(query)

    event = fake_log.events[0]
    expected = hashlib.sha256(query.encode()).hexdigest()[:12]
    assert event["query_hash"] == expected


# ---------------------------------------------------------------------------
# Contract: latency_ms is a non-negative float.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_search_result_latency_ms_is_non_negative() -> None:
    pipeline = _build_pipeline()
    result = pipeline.search("anything")
    assert isinstance(result.latency_ms, float)
    assert result.latency_ms >= 0.0
