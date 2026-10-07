"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`SearchBackendProtocol`.

``kairix.quality.contracts.search.SearchBackendProtocol`` is the
quality-layer search contract (``search(query, agent, limit) ->
list[result]``). The production :class:`SearchPipeline` returns a richer
``SearchResult`` envelope, so the contract is proved over a thin
conforming adapter that projects the envelope's budgeted rows to the
contract's ranked list.

When every retrieval backend fails (BM25 index and vector index both
down) the backend must return an empty ranked list — never raise into
the agent tool call — so the caller can report "no results" honestly.

One body, two pipelines under the adapter (F43 limb 2):

* the real :class:`SearchPipeline` composed via
  :func:`kairix.core.factory.build_search_pipeline` with BM25 and vector
  repositories that raise;
* the canonical :class:`tests.fakes.FakeSearchPipeline` scripted empty.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.factory import QUERY_CACHE_DISABLED, RERANK_DISABLED, FactoryDeps, build_search_pipeline
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.quality.contracts.search import SearchBackendProtocol
from tests.fakes import (
    FakeClassifier,
    FakeCollectionResolver,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakePaths,
    FakeSearchLogger,
    FakeSearchPipeline,
    FakeVectorRepository,
)

pytestmark = pytest.mark.contract


class _PipelineSearchBackend:
    """``SearchBackendProtocol`` over a ``SearchPipeline``-shaped object."""

    def __init__(self, pipeline: Any) -> None:
        self._pipeline = pipeline

    def search(self, query: str, agent: str | None = None, limit: int = 10) -> list[Any]:
        envelope = self._pipeline.search(query, agent=agent)
        return [row.result for row in envelope.results][:limit]


def _real_pipeline_backends_down() -> Any:
    return build_search_pipeline(
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


_FACTORIES: list[Callable[[], Any]] = [_real_pipeline_backends_down, lambda: FakeSearchPipeline([])]


@pytest.mark.parametrize("factory", _FACTORIES, ids=["real-search-pipeline", "fake"])
def test_search_unavailable_backends_returns_empty_ranked_list(factory: Callable[[], Any]) -> None:
    """Both retrieval backends raise; the backend returns ``[]``.

    Sabotage proof: in ``SearchPipeline._dispatch_backends`` (or the BM25
    worker it submits) remove the ``except Exception`` that logs
    "pipeline: BM25 search failed" so the error propagates. Re-run: the
    real case fails with ``RuntimeError: F68 bm25 index down``. Restored.
    """
    backend = _PipelineSearchBackend(factory())
    assert isinstance(backend, SearchBackendProtocol)

    assert backend.search("how do I deploy the worker?") == []
    assert backend.search("how do I deploy the worker?", agent="agent-alpha", limit=3) == []
