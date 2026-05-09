"""Integration tests: BM25SearchBackend and VectorSearchBackend wired into SearchPipeline.

These tests verify the full backend → fake-repo → SearchPipeline → SearchResult flow
with no @patch / no monkeypatching. Each fake satisfies the relevant Protocol from
``kairix.core.protocols``:

  - ``FakeDocumentRepository`` -> ``DocumentRepository`` (used by ``BM25SearchBackend``)
  - ``FakeEmbeddingService``   -> ``EmbeddingService``   (used by ``VectorSearchBackend``)
  - ``FakeVectorRepository``   -> ``VectorRepository``   (used by ``VectorSearchBackend``)

Contract tests (in tests/contracts/test_protocols.py) cover the adapter → fake-repo
delegation in isolation. These integration tests exercise the broader composition:
backends embedded in a real ``SearchPipeline`` (with real ``RRFFusion`` and real
``apply_budget``) returning a populated ``SearchResult``, including collection scoping,
fallback behaviour, and end-to-end fusion. ``SearchResult.results`` is a list of
``BudgetedResult`` (each wrapping a ``FusedResult``).
"""

from __future__ import annotations

import pytest

from kairix.core.protocols import (
    DocumentRepository,
    EmbeddingService,
    VectorRepository,
)
from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.budget import BudgetedResult
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchPipeline, SearchResult
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test data builders
# ---------------------------------------------------------------------------


def _bm25_doc(
    *,
    path: str,
    title: str,
    content: str,
    collection: str,
) -> dict:
    """Build a document compatible with FakeDocumentRepository.search_fts AND
    with the rrf() fusion contract (which reads ``file`` / ``title`` /
    ``snippet`` / ``collection``)."""
    return {
        "path": path,
        "file": path,  # rrf reads "file"
        "title": title,
        "content": content,
        "snippet": content[:120],  # rrf reads "snippet"
        "collection": collection,
    }


def _vec_hit(
    *,
    path: str,
    distance: float,
    collection: str,
    title: str = "Vector hit",
    snippet: str = "Snippet from vector search.",
) -> dict:
    """Build a vector-search result row in the shape rrf() consumes."""
    return {
        "path": path,
        "distance": distance,
        "collection": collection,
        "title": title,
        "snippet": snippet,
    }


def _result_paths(result: SearchResult) -> list[str]:
    """Extract paths from a SearchResult.results list of BudgetedResult."""
    paths: list[str] = []
    for r in result.results:
        # Each entry is a BudgetedResult(result=FusedResult(path=..., ...), ...).
        assert isinstance(r, BudgetedResult), f"unexpected result type {type(r)!r}"
        paths.append(r.result.path)
    return paths


def _build_pipeline(
    *,
    bm25: BM25SearchBackend,
    vector: VectorSearchBackend,
    intent: QueryIntent = QueryIntent.SEMANTIC,
    logger: FakeSearchLogger | None = None,
    graph_available: bool = False,
) -> SearchPipeline:
    """Compose a SearchPipeline with the given backends + protocol-compliant fakes."""
    return SearchPipeline(
        classifier=FakeClassifier(intent=intent),
        bm25=bm25,
        vector=vector,
        graph=FakeGraphRepository(available=graph_available),
        fusion=RRFFusion(k=60),
        boosts=[],
        logger=logger,
        config=RetrievalConfig.defaults(),
    )


# ---------------------------------------------------------------------------
# BM25SearchBackend wired into a real SearchPipeline
# ---------------------------------------------------------------------------


class TestBM25BackendInPipeline:
    """BM25SearchBackend (wrapping FakeDocumentRepository) drives BM25 leg of pipeline."""

    @pytest.mark.integration
    def test_bm25_backend_satisfies_protocol(self) -> None:
        """FakeDocumentRepository under BM25SearchBackend satisfies DocumentRepository."""
        repo = FakeDocumentRepository()
        assert isinstance(repo, DocumentRepository)
        backend = BM25SearchBackend(repo)
        assert hasattr(backend, "search")

    @pytest.mark.integration
    def test_bm25_backend_returns_pipeline_results(self) -> None:
        """A query reaching the BM25 backend produces results in SearchResult."""
        docs = [
            _bm25_doc(
                path="vault/architecture.md",
                title="Architecture",
                content="kairix architecture patterns and protocols",
                collection="shared",
            ),
            _bm25_doc(
                path="vault/runbook.md",
                title="Runbook",
                content="operational runbook for restart",
                collection="shared",
            ),
            _bm25_doc(
                path="vault/unrelated.md",
                title="Unrelated",
                content="cooking recipes",
                collection="shared",
            ),
        ]
        repo = FakeDocumentRepository(documents=docs)
        bm25 = BM25SearchBackend(repo)
        pipeline = _build_pipeline(
            bm25=bm25,
            vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
            intent=QueryIntent.KEYWORD,
            logger=FakeSearchLogger(),
        )

        result = pipeline.search("architecture")

        assert isinstance(result, SearchResult)
        assert result.bm25_count == 1, f"BM25 should match exactly one doc; got {result.bm25_count}"
        # The matched doc must propagate all the way through fusion + budget.
        assert _result_paths(result) == ["vault/architecture.md"]

    @pytest.mark.integration
    def test_bm25_backend_respects_collection_filter(self) -> None:
        """The pipeline forwards the resolved collections list to BM25SearchBackend."""
        docs = [
            _bm25_doc(path="a.md", title="A", content="match", collection="alpha"),
            _bm25_doc(path="b.md", title="B", content="match", collection="beta"),
        ]
        bm25 = BM25SearchBackend(FakeDocumentRepository(documents=docs))
        pipeline = _build_pipeline(
            bm25=bm25,
            vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
            intent=QueryIntent.KEYWORD,
        )

        result = pipeline.search("match", collections=["alpha"])

        assert result.bm25_count == 1
        # Only the alpha-collection doc must surface — proves collection arg
        # actually flowed BM25SearchBackend → FakeDocumentRepository.
        assert _result_paths(result) == ["a.md"]


# ---------------------------------------------------------------------------
# VectorSearchBackend wired into a real SearchPipeline
# ---------------------------------------------------------------------------


class TestVectorBackendInPipeline:
    """VectorSearchBackend (wrapping FakeEmbedding + FakeVectorRepository) drives vector leg."""

    @pytest.mark.integration
    def test_vector_backend_components_satisfy_protocols(self) -> None:
        """Composition pieces satisfy EmbeddingService and VectorRepository."""
        emb = FakeEmbeddingService()
        repo = FakeVectorRepository()
        assert isinstance(emb, EmbeddingService)
        assert isinstance(repo, VectorRepository)
        backend = VectorSearchBackend(emb, repo)
        assert hasattr(backend, "search")

    @pytest.mark.integration
    def test_vector_backend_returns_pipeline_results(self) -> None:
        """Vector backend results flow through the pipeline to SearchResult."""
        vec_results = [
            _vec_hit(path="sem-a.md", distance=0.1, collection="shared"),
            _vec_hit(path="sem-b.md", distance=0.2, collection="shared"),
        ]
        vector = VectorSearchBackend(
            FakeEmbeddingService(),
            FakeVectorRepository(results=vec_results),
        )
        pipeline = _build_pipeline(
            bm25=BM25SearchBackend(FakeDocumentRepository()),
            vector=vector,
            intent=QueryIntent.SEMANTIC,
        )

        result = pipeline.search("semantic search query")

        assert result.vec_count == 2
        # Both vector hits surface (different paths, no BM25 hits to dedupe with).
        assert sorted(_result_paths(result)) == ["sem-a.md", "sem-b.md"]
        # vec_failed should be False — vector returned non-empty
        assert result.vec_failed is False

    @pytest.mark.integration
    def test_vector_backend_empty_embedding_short_circuits(self) -> None:
        """An EmbeddingService that returns [] yields vec_count=0 and no exception.

        Uses ``FakeEmbeddingService(vector=[])`` from tests/fakes.py — the
        canonical way to exercise the empty-embedding branch of
        ``VectorSearchBackend.search``.
        """
        empty_embedding = FakeEmbeddingService(vector=[])
        # Sanity: protocol-compliant.
        assert isinstance(empty_embedding, EmbeddingService)
        assert empty_embedding.embed("anything") == []

        vector = VectorSearchBackend(
            empty_embedding,
            FakeVectorRepository(results=[_vec_hit(path="never.md", distance=0.1, collection="c")]),
        )
        pipeline = _build_pipeline(
            bm25=BM25SearchBackend(FakeDocumentRepository()),
            vector=vector,
            intent=QueryIntent.SEMANTIC,
        )

        result = pipeline.search("query")

        assert result.vec_count == 0
        assert result.vec_failed is True
        # The "never.md" vector hit must NOT appear — embedding bailed.
        assert "never.md" not in _result_paths(result)
        assert _result_paths(result) == []

    @pytest.mark.integration
    def test_vector_backend_respects_collection_filter(self) -> None:
        """Pipeline forwards collections through VectorSearchBackend.search."""
        vec_results = [
            _vec_hit(path="a.md", distance=0.1, collection="alpha"),
            _vec_hit(path="b.md", distance=0.2, collection="beta"),
        ]
        vector = VectorSearchBackend(
            FakeEmbeddingService(),
            FakeVectorRepository(results=vec_results),
        )
        pipeline = _build_pipeline(
            bm25=BM25SearchBackend(FakeDocumentRepository()),
            vector=vector,
            intent=QueryIntent.SEMANTIC,
        )

        result = pipeline.search("query", collections=["beta"])

        assert result.vec_count == 1
        assert _result_paths(result) == ["b.md"]


# ---------------------------------------------------------------------------
# Both backends together — exercises full hybrid path
# ---------------------------------------------------------------------------


class TestBothBackendsInPipeline:
    """Both backends populated — verifies hybrid composition end-to-end."""

    @pytest.mark.integration
    def test_hybrid_backends_produce_combined_results(self) -> None:
        docs = [
            _bm25_doc(
                path="bm25-only.md",
                title="BM25 Hit",
                content="literal keyword match",
                collection="shared",
            ),
        ]
        vec_results = [
            _vec_hit(path="vec-only.md", distance=0.1, collection="shared"),
        ]
        pipeline = _build_pipeline(
            bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
            vector=VectorSearchBackend(
                FakeEmbeddingService(),
                FakeVectorRepository(results=vec_results),
            ),
            intent=QueryIntent.SEMANTIC,
            logger=FakeSearchLogger(),
        )

        result = pipeline.search("literal")

        assert result.bm25_count == 1
        assert result.vec_count == 1
        # RRFFusion merges into a single FusedResult per path; both should
        # appear because the docs do not share a path.
        assert sorted(_result_paths(result)) == ["bm25-only.md", "vec-only.md"]

    @pytest.mark.integration
    def test_pipeline_logger_records_backend_counts(self) -> None:
        """The injected SearchLogger receives the bm25_count / vec_count
        produced by the backends — proves the integration logged backend output."""
        docs = [
            _bm25_doc(
                path="logged.md",
                title="Logged",
                content="payload",
                collection="shared",
            ),
        ]
        vec_results = [_vec_hit(path="logged-vec.md", distance=0.1, collection="shared")]
        log = FakeSearchLogger()
        pipeline = _build_pipeline(
            bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
            vector=VectorSearchBackend(
                FakeEmbeddingService(),
                FakeVectorRepository(results=vec_results),
            ),
            intent=QueryIntent.SEMANTIC,
            logger=log,
        )

        pipeline.search("payload")

        assert len(log.events) == 1
        event = log.events[0]
        assert event["bm25_count"] == 1
        assert event["vec_count"] == 1
