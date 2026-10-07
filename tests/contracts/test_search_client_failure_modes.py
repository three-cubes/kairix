"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`SearchClient`.

``SearchClient.search(query)`` is the transport seam the search probe
drives. When every retrieval backend behind the client is down, the
production pipeline degrades to an *empty* result (it never raises). The
probe must record those queries as executed — with a per-query stage
record each — and must not count an honest "no results" as a transport
error (that would page operators for a latency gate on an index outage
the recall gate owns).

One body, two implementations (F43 limb 2):

* a client over the real :class:`SearchPipeline` (composed via
  :func:`kairix.core.factory.build_search_pipeline` with BM25 and vector
  repositories that raise), calling it exactly as
  :class:`InProcessSearchClient` does. ``InProcessSearchClient`` itself
  resolves the memoised production factory with no injection seam, so it
  cannot be pointed at a failing pipeline from a test;
* the canonical :class:`tests.fakes.FakeSearchClient` scripted empty.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from kairix.core.factory import QUERY_CACHE_DISABLED, RERANK_DISABLED, FactoryDeps, build_search_pipeline
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.quality.probe import SearchClient, run_probe_search
from tests.fakes import (
    FakeClassifier,
    FakeCollectionResolver,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakePaths,
    FakeSearchClient,
    FakeSearchLogger,
    FakeVectorRepository,
)

pytestmark = pytest.mark.contract


@dataclass(frozen=True)
class _Case:
    """Minimal suite case — the sampler reads ``.id`` / ``.category`` / ``.query``."""

    id: str
    category: str
    query: str


def _suite_loader(_suite: str) -> list[_Case]:
    return [
        _Case(id=f"{cat}-{i}", category=cat, query=f"query {cat} {i}")
        for cat in ("recall", "temporal", "entity", "conceptual", "multi_hop", "procedural")
        for i in range(3)
    ]


class _PipelineSearchClient:
    """``SearchClient`` over a composed pipeline — the ``InProcessSearchClient`` call shape."""

    def __init__(self, pipeline: Any) -> None:
        self._pipeline = pipeline

    def search(self, query: Any) -> Any:
        return self._pipeline.search(query=query.query, agent=query.agent)


def _real_backends_down() -> Any:
    return _PipelineSearchClient(
        build_search_pipeline(
            config=RetrievalConfig.defaults(),
            paths=FakePaths(),
            deps=FactoryDeps(
                classifier_override=FakeClassifier(intent=QueryIntent.SEMANTIC),
                doc_repo_override=FakeDocumentRepository(documents=[], raises=RuntimeError("F68 bm25 index down")),
                vec_repo_override=FakeVectorRepository(results=[], raises=RuntimeError("F68 vector index down")),
                embed_service_override=FakeEmbeddingService(),
                graph_override=FakeGraphRepository(available=True),
                fusion_override=RRFFusion(),
                boosts_override=[],
                logger_override=FakeSearchLogger(),
                resolver_override=FakeCollectionResolver(),
                query_cache_override=QUERY_CACHE_DISABLED,
                reranker_override=RERANK_DISABLED,
            ),
        )
    )


_FACTORIES: list[Callable[[], Any]] = [_real_backends_down, lambda: FakeSearchClient(results=[])]


@pytest.mark.parametrize("factory", _FACTORIES, ids=["real-search-pipeline", "fake"])
def test_search_returns_empty_when_backends_down_probe_records_queries_not_errors(
    factory: Callable[[], Any],
) -> None:
    """Every query returns an empty result: all six execute, each gets a
    per-query stage record, and none is counted as an error.

    Sabotage proof: in ``kairix/quality/probe/runner.py::_per_query_stages``
    add ``if not stage_map: continue`` before the append. Re-run: the fake
    case fails (six queries ran, zero records surfaced) — the failed /
    empty-result rows operators rely on to spot patterns disappear.
    Restored.
    """
    client = factory()
    assert isinstance(client, SearchClient)

    result = run_probe_search(
        suite="f68",
        queries=6,
        suite_loader=_suite_loader,
        searcher=client.search,
        warmup=False,
    )

    assert result.queries == 6
    assert result.errors == 0
    assert len(result.per_query_stages) == 6
    assert all(row["case_id"] for row in result.per_query_stages)
