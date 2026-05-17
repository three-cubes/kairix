"""Integration: query cache wired into a real SearchPipeline cuts backend traffic.

Boundary chain:
  agent → SearchPipeline.search → QueryResultCache → BM25 backend (fake repo)

The fakes are at the I/O boundary; the cache + pipeline composition are
real. Tests assert on the canonical observables: backend call count, cache
stats, and per-query latency-ms shape.
"""

from __future__ import annotations

import time

import pytest

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

pytestmark = pytest.mark.integration


def _build(cache: QueryResultCache | None) -> tuple[SearchPipeline, FakeDocumentRepository]:
    """Standard rig: real pipeline + canonical fakes; cache optional."""
    doc_repo = FakeDocumentRepository(
        documents=[{"path": "p.md", "title": "T", "content": "alpha bravo charlie", "collection": "c"}]
    )
    pipeline = SearchPipeline(
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
    return pipeline, doc_repo


@pytest.mark.integration
def test_cache_hit_returns_same_result_object_with_recorded_latency() -> None:
    """A cache hit returns the exact result object the miss produced.

    Sabotage: change the cache fast-path to ``return SearchResult(...)``
    fresh-built instead of the stored object, and the identity assertion
    fails. The reason this matters: latency_ms / stage_latency_ms on the
    cached result are what they were on the miss — the operator sees the
    underlying-pipeline cost, not zero — so probe latency reports stay
    interpretable across cache hits and misses.
    """
    cache = QueryResultCache(max_entries=10, max_age_s=60.0)
    pipeline, _ = _build(cache)

    first = pipeline.search("alpha")
    second = pipeline.search("alpha")
    assert second is first, "cache hit should return the SAME SearchResult object"


@pytest.mark.integration
def test_cache_off_means_no_dedupe() -> None:
    """When ``query_cache=None`` the pipeline preserves the original behaviour.

    Sabotage: silently inject a process-shared cache when None is passed
    and this assertion fails — existing tests that explicitly opt out
    of caching (and there are many) start failing because their backend
    isn't being called the expected number of times.
    """
    pipeline, doc_repo = _build(cache=None)

    pipeline.search("alpha")
    pipeline.search("alpha")
    assert len(doc_repo.calls) == 2, f"expected 2 backend calls without cache; got {len(doc_repo.calls)}"


@pytest.mark.integration
def test_cache_age_expiry_evicts_stale_entries() -> None:
    """An entry older than max_age_s is treated as a miss.

    Drives the cache's clock via the public ``clock`` kwarg on
    QueryResultCache — no monkey-patch of ``time.time``. The clock
    returns the test-controlled value each time the cache asks; advancing
    the value past max_age_s makes the next get() see an expired entry.
    Sabotage: drop the age check in QueryResultCache.get and the second
    search returns the stale cached entry, so doc_repo.calls stays at 1
    instead of growing to 2.
    """
    fake_now = [time.time()]
    cache = QueryResultCache(max_entries=10, max_age_s=1.0, clock=lambda: fake_now[0])
    pipeline, doc_repo = _build(cache)

    pipeline.search("alpha")
    assert len(doc_repo.calls) == 1

    # Advance the clock past max_age_s — entry should be treated as expired.
    fake_now[0] += 5.0

    pipeline.search("alpha")
    assert len(doc_repo.calls) == 2, "expired entry should not have been served"


@pytest.mark.integration
def test_cache_max_entries_evicts_oldest_under_pressure() -> None:
    """When the cache fills, the LRU evicts the least-recently-used entry.

    Capacity=2 → after inserting alpha, bravo, charlie, alpha must have
    been evicted (it's the least-recently-used). Re-asking for alpha
    therefore misses the cache and re-hits the backend; re-asking for
    charlie (the most-recently-inserted) hits the cache and does NOT
    grow doc_repo.calls.

    Sabotage: change eviction policy from LRU to FIFO-without-promotion
    and the second-most-recent entry gets evicted instead of the least-
    recently-used one — the post-eviction backend-call count diverges
    from this assertion.
    """
    cache = QueryResultCache(max_entries=2, max_age_s=60.0)
    pipeline, doc_repo = _build(cache)

    pipeline.search("alpha")  # miss, cache=[alpha]
    pipeline.search("bravo")  # miss, cache=[alpha, bravo]
    pipeline.search("charlie")  # miss, cache=[bravo, charlie]; alpha evicted
    assert len(doc_repo.calls) == 3

    pipeline.search("alpha")  # miss (alpha was evicted), cache=[charlie, alpha]
    assert len(doc_repo.calls) == 4

    pipeline.search("alpha")  # hit (alpha just re-inserted), no backend call
    assert len(doc_repo.calls) == 4, "alpha was just inserted; should be a cache hit"


@pytest.mark.integration
def test_cache_does_not_cache_error_results() -> None:
    """A SearchResult with .error set must not be cached.

    Caching errors would lock a transient outage in front of every
    same-key caller until the entry ages out. Sabotage: drop the
    ``not result.error`` guard on the cache.put() in SearchPipeline.search
    and the second call hits the cached error envelope instead of
    re-running the pipeline.
    """
    from kairix.core.search.intent import QueryIntent

    class _AlwaysEntityClassifier:
        def classify(self, _q: str) -> QueryIntent:
            return QueryIntent.ENTITY

    cache = QueryResultCache(max_entries=10, max_age_s=60.0)
    doc_repo = FakeDocumentRepository(documents=[{"path": "p.md", "title": "T", "content": "alpha", "collection": "c"}])
    pipeline = SearchPipeline(
        classifier=_AlwaysEntityClassifier(),
        bm25=BM25SearchBackend(doc_repo),
        vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
        graph=FakeGraphRepository(available=False),  # ENTITY + no graph → error envelope
        fusion=FakeFusion(),
        boosts=[],
        logger=FakeSearchLogger(),
        config=RetrievalConfig.defaults(),
        query_cache=cache,
    )

    first = pipeline.search("anything")
    second = pipeline.search("anything")

    assert first.error  # both produced error envelopes
    assert second.error
    # If the error had been cached, ``second is first`` would hold — it must NOT.
    assert second is not first, "error envelopes must not be cached and reused"
