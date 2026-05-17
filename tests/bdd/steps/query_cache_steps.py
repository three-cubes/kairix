"""Step definitions for query_cache.feature.

Drives a real ``SearchPipeline`` wired with a real ``QueryResultCache`` and
the canonical kairix fakes for the I/O boundaries (FakeDocumentRepository
etc). No @patch on internals, no monkeypatch on kairix modules — the cache
seam is exercised through the public SearchPipeline.search() surface.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.pipeline import SearchPipeline
from kairix.core.search.query_cache import QueryResultCache
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeFusion,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
)

pytestmark = pytest.mark.bdd

# F17: lift repeated phrase fragments to constants.
_PHRASE_PIPELINE_WITH_CACHE = "a search pipeline wired with an in-process query cache"
_PHRASE_COUNTING_BACKEND = "a backend that counts how many times it is asked"


@pytest.fixture
def _qc_state() -> dict[str, Any]:
    """Per-scenario fresh state.

    ``doc_repo`` is the canonical fake whose ``calls`` list tracks every
    BM25 dispatch — this is the observable that pins "did the pipeline
    skip the search backend on cache hit?".
    """
    return {
        "cache": None,
        "doc_repo": None,
        "pipeline": None,
        "results": [],
    }


def _build_pipeline(doc_repo: FakeDocumentRepository, cache: QueryResultCache) -> SearchPipeline:
    """Construct a real SearchPipeline wired with canonical fakes + the cache."""
    return SearchPipeline(
        classifier=FakeClassifier(),
        bm25=BM25SearchBackend(doc_repo),
        vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
        graph=FakeGraphRepository(available=True),
        fusion=FakeFusion(),
        boosts=[],
        logger=FakeSearchLogger(),
        config=RetrievalConfig.defaults(),
        query_cache=cache,
    )


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(_PHRASE_PIPELINE_WITH_CACHE)
def _given_pipeline(_qc_state: dict[str, Any]) -> None:
    """Stand up the cache. Pipeline is constructed lazily after the backend Given."""
    _qc_state["cache"] = QueryResultCache(max_entries=10, max_age_s=60.0)


@given(_PHRASE_COUNTING_BACKEND)
def _given_counting_backend(_qc_state: dict[str, Any]) -> None:
    """Backend = FakeDocumentRepository — its ``.calls`` list IS the counter."""
    _qc_state["doc_repo"] = FakeDocumentRepository(
        documents=[{"path": "p.md", "title": "T", "content": "alpha", "collection": "c"}]
    )
    _qc_state["pipeline"] = _build_pipeline(_qc_state["doc_repo"], _qc_state["cache"])


@given("a pipeline configured to produce an error envelope")
def _given_error_pipeline(_qc_state: dict[str, Any]) -> None:
    """A pipeline whose first SearchResult carries .error set.

    The cleanest deterministic error path is ENTITY intent + no graph
    available — the pipeline short-circuits with ENTITY_GRAPH_UNAVAILABLE
    BEFORE the cache-write at the end of search(). The test pins that
    even though no .error-aware code path is special-cased, the cache
    stays empty because the cache-write is guarded on ``not result.error``.
    """
    from kairix.core.search.intent import QueryIntent

    class _EntityClassifier:
        def classify(self, _q: str) -> QueryIntent:
            return QueryIntent.ENTITY

    doc_repo = FakeDocumentRepository(documents=[])
    _qc_state["doc_repo"] = doc_repo
    _qc_state["pipeline"] = SearchPipeline(
        classifier=_EntityClassifier(),
        bm25=BM25SearchBackend(doc_repo),
        vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
        graph=FakeGraphRepository(available=False),  # ENTITY + no graph → error
        fusion=FakeFusion(),
        boosts=[],
        logger=FakeSearchLogger(),
        config=RetrievalConfig.defaults(),
        query_cache=_qc_state["cache"],
    )


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse('an agent asks the search pipeline for "{query}"'))
def _when_search(_qc_state: dict[str, Any], query: str) -> None:
    """Drive a search call. parsers.parse strips outer quotes from the .feature."""
    result = _qc_state["pipeline"].search(query=query)
    _qc_state["results"].append(result)


@when(parsers.parse('the same agent asks the search pipeline for "{query}" again'))
def _when_search_again(_qc_state: dict[str, Any], query: str) -> None:
    """Same agent → same default agent kwarg → same cache key."""
    result = _qc_state["pipeline"].search(query=query)
    _qc_state["results"].append(result)


@when(parsers.parse('agent "{agent}" asks the search pipeline for "{query}"'))
def _when_search_with_agent(_qc_state: dict[str, Any], agent: str, query: str) -> None:
    """Different agent kwarg → different cache key (asserts the agent dimension)."""
    result = _qc_state["pipeline"].search(query=query, agent=agent)
    _qc_state["results"].append(result)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse("the backend was asked only once"))
def _then_asked_once(_qc_state: dict[str, Any]) -> None:
    """Sabotage: drop the cache fast-path in SearchPipeline.search and the
    second search re-invokes the backend → assertion fires.
    """
    calls = _qc_state["doc_repo"].calls
    assert len(calls) == 1, f"expected 1 backend call after cache hit; got {len(calls)}: {calls}"


@then(parsers.parse("the backend was asked twice"))
def _then_asked_twice(_qc_state: dict[str, Any]) -> None:
    """Sabotage: stop keying cache entries on the agent dimension (or stop
    skipping the cache-write on error envelopes) and the second call hits
    the cache instead of the backend, breaking this assertion.
    """
    calls = _qc_state["doc_repo"].calls
    assert len(calls) == 2, f"expected 2 backend calls (cache must NOT collapse them); got {len(calls)}: {calls}"


@then("the cache stats report one hit and one miss")
def _then_stats_one_hit_one_miss(_qc_state: dict[str, Any]) -> None:
    """Sabotage: skip the stats.hits / stats.misses increments and the
    observable counts diverge from the operator-facing reality.
    """
    stats = _qc_state["cache"].stats()
    assert stats.hits == 1, f"expected 1 hit; got {stats.hits}"
    assert stats.misses == 1, f"expected 1 miss; got {stats.misses}"


@then("the cache contains zero entries")
def _then_cache_empty(_qc_state: dict[str, Any]) -> None:
    """Sabotage: drop the ``not result.error`` guard in SearchPipeline's
    cache-write and the error envelope lands in the cache → assertion fires.
    """
    stats = _qc_state["cache"].stats()
    assert stats.size == 0, f"error envelopes should NOT populate the cache; got size={stats.size}"


@then("the cache stats report zero hits and one miss")
def _then_stats_zero_hits_one_miss(_qc_state: dict[str, Any]) -> None:
    """Sabotage: increment hits on the error path and the operator-facing
    hit-rate gets inflated by failures, masking real cache effectiveness.
    """
    stats = _qc_state["cache"].stats()
    assert stats.hits == 0, f"expected 0 hits on an error-only run; got {stats.hits}"
    assert stats.misses == 1, f"expected 1 miss; got {stats.misses}"
