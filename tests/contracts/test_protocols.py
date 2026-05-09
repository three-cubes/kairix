"""Contract tests: verify fakes and real implementations satisfy domain protocols.

Each protocol defined in kairix.core.protocols gets:
  1. An isinstance() conformance check against its fake
  2. An isinstance() conformance check against the real implementation
  3. Behavioural tests verifying return types and basic functionality
"""

import pytest

from kairix.core.protocols import (
    BoostStrategy,
    DocumentRepository,
    EmbeddingService,
    FusionStrategy,
    GraphRepository,
    IntentClassifier,
    ScoringStrategy,
    SearchLogger,
    VectorRepository,
)
from kairix.core.search.backends import (
    AzureEmbeddingService,
    BM25SearchBackend,
    VectorSearchBackend,
)
from kairix.core.search.boosts import (
    ChunkDateBoost,
    EntityBoost,
    ProceduralBoost,
    TemporalDateBoost,
)
from kairix.core.search.fusion import BM25PrimaryFusion, RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.quality.eval.scorers import (
    SCORERS,
    ExactMatchScorer,
    FuzzyMatchScorer,
    LLMJudgeScorer,
    NDCGScorer,
)
from tests.fakes import (
    FakeBoost,
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeFusion,
    FakeGraphRepository,
    FakeScorer,
    FakeSearchLogger,
    FakeVectorRepository,
)

# ---------------------------------------------------------------------------
# Protocol conformance — isinstance() checks
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestProtocolCompliance:
    """Every fake must satisfy its corresponding protocol via isinstance()."""

    @pytest.mark.contract
    def test_fake_classifier_satisfies_protocol(self):
        assert isinstance(FakeClassifier(), IntentClassifier)

    @pytest.mark.contract
    def test_fake_document_repo_satisfies_protocol(self):
        assert isinstance(FakeDocumentRepository(), DocumentRepository)

    @pytest.mark.contract
    def test_fake_graph_repo_satisfies_protocol(self):
        assert isinstance(FakeGraphRepository(), GraphRepository)

    @pytest.mark.contract
    def test_fake_vector_repo_satisfies_protocol(self):
        assert isinstance(FakeVectorRepository(), VectorRepository)

    @pytest.mark.contract
    def test_fake_embedding_service_satisfies_protocol(self):
        assert isinstance(FakeEmbeddingService(), EmbeddingService)

    @pytest.mark.contract
    def test_fake_fusion_satisfies_protocol(self):
        assert isinstance(FakeFusion(), FusionStrategy)

    @pytest.mark.contract
    def test_fake_boost_satisfies_protocol(self):
        assert isinstance(FakeBoost(), BoostStrategy)

    @pytest.mark.contract
    def test_fake_scorer_satisfies_protocol(self):
        assert isinstance(FakeScorer(), ScoringStrategy)

    @pytest.mark.contract
    def test_fake_search_logger_satisfies_protocol(self):
        assert isinstance(FakeSearchLogger(), SearchLogger)


# ---------------------------------------------------------------------------
# Cross-check: existing implementations vs new protocols
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestRealImplementationCompliance:
    """Verify real repository implementations satisfy their protocols."""

    @pytest.mark.contract
    def test_sqlite_repo_satisfies_protocol(self, tmp_path):
        """SQLiteDocumentRepository satisfies DocumentRepository protocol."""
        from kairix.core.db.repository import SQLiteDocumentRepository

        repo = SQLiteDocumentRepository(db_path=tmp_path / "test.sqlite")
        assert isinstance(repo, DocumentRepository)

    @pytest.mark.contract
    def test_neo4j_repo_satisfies_protocol(self):
        """Neo4jGraphRepository satisfies GraphRepository protocol."""
        from kairix.knowledge.graph.repository import Neo4jGraphRepository

        # Use FakeNeo4jClient as the underlying client for the protocol check
        from tests.fixtures.neo4j_mock import FakeNeo4jClient

        repo = Neo4jGraphRepository(client=FakeNeo4jClient())
        assert isinstance(repo, GraphRepository)

    @pytest.mark.contract
    def test_usearch_repo_satisfies_protocol(self):
        """UsearchVectorRepository satisfies VectorRepository protocol."""
        from unittest.mock import MagicMock

        from kairix.core.search.vector_repository import UsearchVectorRepository

        mock_index = MagicMock()
        mock_index.__len__ = MagicMock(return_value=0)
        repo = UsearchVectorRepository(index=mock_index)
        assert isinstance(repo, VectorRepository)


@pytest.mark.contract
class TestRealDocumentRepositoryBehaviour:
    """Behavioural tests for SQLiteDocumentRepository with a real SQLite DB."""

    @pytest.fixture()
    def repo(self, tmp_path):
        from kairix.core.db import open_db
        from kairix.core.db.repository import SQLiteDocumentRepository
        from kairix.core.db.schema import create_schema

        db_path = tmp_path / "test.sqlite"
        db = open_db(db_path)
        create_schema(db)
        db.close()
        return SQLiteDocumentRepository(db_path=db_path)

    @pytest.mark.contract
    def test_insert_and_get_by_path(self, repo):
        repo.insert_or_update("doc/a.md", "notes", "Doc A", "Hello world content", "hash1")
        doc = repo.get_by_path("doc/a.md")
        assert doc is not None
        assert doc["title"] == "Doc A"
        assert doc["collection"] == "notes"

    @pytest.mark.contract
    def test_get_by_path_returns_none_for_missing(self, repo):
        assert repo.get_by_path("nonexistent.md") is None

    @pytest.mark.contract
    def test_search_fts_returns_results(self, repo, tmp_path):
        """Insert a document with FTS data and verify search_fts finds it."""
        from kairix.core.db import open_db

        db_path = tmp_path / "test.sqlite"
        db = open_db(db_path)
        db.row_factory = None
        # Insert FTS row manually (search_fts queries documents_fts)
        row = db.execute("SELECT id FROM documents WHERE path = 'doc/a.md'").fetchone()
        if row is None:
            # Insert the document first
            repo.insert_or_update(
                "doc/a.md",
                "notes",
                "Doc A",
                "architecture decision record content",
                "hash1",
            )
            row = db.execute("SELECT id FROM documents WHERE path = 'doc/a.md'").fetchone()
        doc_id = row[0]
        db.execute(
            "INSERT OR REPLACE INTO documents_fts (rowid, filepath, title, doc) VALUES (?, ?, ?, ?)",
            (doc_id, "doc/a.md", "Doc A", "architecture decision record content"),
        )
        db.commit()
        db.close()

        results = repo.search_fts("architecture")
        assert isinstance(results, list)
        assert len(results) >= 1
        assert results[0]["file"] == "doc/a.md"

    @pytest.mark.contract
    def test_search_fts_empty_query_returns_empty(self, repo):
        assert repo.search_fts("") == []
        assert repo.search_fts("   ") == []


@pytest.mark.contract
class TestNeo4jGraphRepositoryBehaviour:
    """Behavioural tests for Neo4jGraphRepository wrapping FakeNeo4jClient."""

    @pytest.fixture()
    def repo(self):
        from kairix.knowledge.graph.repository import Neo4jGraphRepository
        from tests.fixtures.neo4j_mock import FakeNeo4jClient

        return Neo4jGraphRepository(client=FakeNeo4jClient())

    @pytest.mark.contract
    def test_available(self, repo):
        assert repo.available is True

    @pytest.mark.contract
    def test_cypher_returns_list(self, repo):
        results = repo.cypher("MATCH (n) RETURN n")
        assert isinstance(results, list)

    @pytest.mark.contract
    def test_find_entity_delegates_to_cypher(self, repo):
        # FakeNeo4jClient.cypher returns all entities for non-special queries
        result = repo.find_entity("OpenClaw")
        assert result is not None
        assert isinstance(result, dict)

    @pytest.mark.contract
    def test_entity_in_degrees_returns_list(self, repo):
        results = repo.entity_in_degrees()
        assert isinstance(results, list)


@pytest.mark.contract
class TestExistingImplementations:
    """Verify existing real/fake implementations against new protocols."""

    @pytest.mark.contract
    def test_fake_neo4j_client_satisfies_graph_repo(self):
        """FakeNeo4jClient now satisfies GraphRepository after Phase 2 adaptation.

        Phase 2 added find_entity() and entity_in_degrees() to FakeNeo4jClient.
        """
        from tests.fixtures.neo4j_mock import FakeNeo4jClient

        client = FakeNeo4jClient()
        assert hasattr(client, "cypher")
        assert hasattr(client, "available")
        assert hasattr(client, "find_entity")
        assert hasattr(client, "entity_in_degrees")
        assert isinstance(client, GraphRepository)


# ---------------------------------------------------------------------------
# Behavioural tests — FakeClassifier
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestFakeClassifier:
    @pytest.mark.contract
    def test_returns_configured_intent(self):
        c = FakeClassifier(intent=QueryIntent.TEMPORAL)
        assert c.classify("anything") == QueryIntent.TEMPORAL

    @pytest.mark.contract
    def test_default_intent_is_semantic(self):
        c = FakeClassifier()
        assert c.classify("hello") == QueryIntent.SEMANTIC

    @pytest.mark.contract
    def test_classify_returns_query_intent(self):
        c = FakeClassifier()
        result = c.classify("test")
        assert isinstance(result, QueryIntent)


# ---------------------------------------------------------------------------
# Behavioural tests — FakeDocumentRepository
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestFakeDocumentRepository:
    @pytest.mark.contract
    def test_search_fts_returns_list(self):
        repo = FakeDocumentRepository(
            documents=[
                {
                    "path": "a.md",
                    "title": "A",
                    "content": "hello world",
                    "collection": "test",
                },
            ]
        )
        results = repo.search_fts("hello")
        assert isinstance(results, list)
        assert len(results) == 1

    @pytest.mark.contract
    def test_search_fts_filters_by_collection(self):
        repo = FakeDocumentRepository(
            documents=[
                {
                    "path": "a.md",
                    "title": "A",
                    "content": "hello",
                    "collection": "notes",
                },
                {
                    "path": "b.md",
                    "title": "B",
                    "content": "hello",
                    "collection": "archive",
                },
            ]
        )
        results = repo.search_fts("hello", collections=["notes"])
        assert len(results) == 1
        assert results[0]["collection"] == "notes"

    @pytest.mark.contract
    def test_search_fts_respects_limit(self):
        docs = [{"path": f"{i}.md", "title": str(i), "content": "match", "collection": "c"} for i in range(10)]
        repo = FakeDocumentRepository(documents=docs)
        results = repo.search_fts("match", limit=3)
        assert len(results) == 3

    @pytest.mark.contract
    def test_get_by_path_returns_dict(self):
        repo = FakeDocumentRepository(documents=[{"path": "a.md", "title": "A"}])
        doc = repo.get_by_path("a.md")
        assert doc is not None
        assert doc["title"] == "A"

    @pytest.mark.contract
    def test_get_by_path_returns_none_for_missing(self):
        repo = FakeDocumentRepository()
        assert repo.get_by_path("missing.md") is None

    @pytest.mark.contract
    def test_get_chunk_dates(self):
        repo = FakeDocumentRepository(
            documents=[
                {"path": "a.md", "chunk_date": "2026-01-15"},
                {"path": "b.md"},
            ]
        )
        dates = repo.get_chunk_dates(["a.md", "b.md", "c.md"])
        assert dates == {"a.md": "2026-01-15"}

    @pytest.mark.contract
    def test_insert_or_update(self):
        repo = FakeDocumentRepository()
        repo.insert_or_update("new.md", "col", "New", "content here", "abc123")
        doc = repo.get_by_path("new.md")
        assert doc is not None
        assert doc["title"] == "New"
        assert doc["content"] == "content here"


# ---------------------------------------------------------------------------
# Behavioural tests — FakeGraphRepository
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestFakeGraphRepository:
    @pytest.mark.contract
    def test_available_property(self):
        g = FakeGraphRepository(available=True)
        assert g.available is True
        g2 = FakeGraphRepository(available=False)
        assert g2.available is False

    @pytest.mark.contract
    def test_find_entity_returns_dict(self):
        g = FakeGraphRepository(entities=[{"name": "Acme", "label": "Organisation"}])
        result = g.find_entity("Acme")
        assert result is not None
        assert result["label"] == "Organisation"

    @pytest.mark.contract
    def test_find_entity_case_insensitive(self):
        g = FakeGraphRepository(entities=[{"name": "Acme", "label": "Organisation"}])
        assert g.find_entity("acme") is not None
        assert g.find_entity("ACME") is not None

    @pytest.mark.contract
    def test_find_entity_returns_none_for_missing(self):
        g = FakeGraphRepository()
        assert g.find_entity("nonexistent") is None

    @pytest.mark.contract
    def test_cypher_returns_list(self):
        g = FakeGraphRepository(entities=[{"name": "X"}])
        results = g.cypher("MATCH (n) RETURN n")
        assert isinstance(results, list)
        assert len(results) == 1

    @pytest.mark.contract
    def test_entity_in_degrees_returns_list(self):
        g = FakeGraphRepository(entities=[{"name": "A"}, {"name": "B"}])
        results = g.entity_in_degrees()
        assert isinstance(results, list)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Behavioural tests — FakeVectorRepository
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestFakeVectorRepository:
    @pytest.mark.contract
    def test_search_returns_configured_results(self):
        results_data = [
            {"path": "a.md", "distance": 0.1, "collection": "c"},
            {"path": "b.md", "distance": 0.2, "collection": "c"},
        ]
        v = FakeVectorRepository(results=results_data)
        results = v.search([0.0] * 10, k=5)
        assert len(results) == 2

    @pytest.mark.contract
    def test_search_respects_k(self):
        results_data = [{"path": f"{i}.md", "distance": 0.1 * i, "collection": "c"} for i in range(10)]
        v = FakeVectorRepository(results=results_data)
        assert len(v.search([0.0], k=3)) == 3

    @pytest.mark.contract
    def test_search_filters_by_collection(self):
        results_data = [
            {"path": "a.md", "distance": 0.1, "collection": "notes"},
            {"path": "b.md", "distance": 0.2, "collection": "archive"},
        ]
        v = FakeVectorRepository(results=results_data)
        results = v.search([0.0], k=10, collections=["notes"])
        assert len(results) == 1

    @pytest.mark.contract
    def test_add_vectors_returns_count(self):
        v = FakeVectorRepository()
        count = v.add_vectors([("hash1", [0.1, 0.2]), ("hash2", [0.3, 0.4])])
        assert count == 2

    @pytest.mark.contract
    def test_count(self):
        v = FakeVectorRepository()
        v.add_vectors([("h1", [0.1])])
        assert v.count() >= 1


# ---------------------------------------------------------------------------
# Behavioural tests — FakeEmbeddingService
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestFakeEmbeddingService:
    @pytest.mark.contract
    def test_embed_returns_float_list(self):
        e = FakeEmbeddingService()
        vec = e.embed("hello")
        assert isinstance(vec, list)
        assert all(isinstance(x, float) for x in vec)

    @pytest.mark.contract
    def test_embed_returns_configured_dim(self):
        e = FakeEmbeddingService(dim=128)
        assert len(e.embed("test")) == 128

    @pytest.mark.contract
    def test_embed_batch_returns_list_of_lists(self):
        e = FakeEmbeddingService()
        results = e.embed_batch(["a", "b", "c"])
        assert len(results) == 3
        assert all(isinstance(v, list) for v in results)

    @pytest.mark.contract
    def test_embed_deterministic(self):
        e = FakeEmbeddingService()
        assert e.embed("x") == e.embed("y")  # same fixed vector


# ---------------------------------------------------------------------------
# Behavioural tests — FakeFusion, FakeBoost, FakeScorer, FakeSearchLogger
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestFakeFusion:
    @pytest.mark.contract
    def test_fuse_concatenates(self):
        f = FakeFusion()
        assert f.fuse([1, 2], [3, 4]) == [1, 2, 3, 4]

    @pytest.mark.contract
    def test_fuse_empty_inputs(self):
        f = FakeFusion()
        assert f.fuse([], []) == []


@pytest.mark.contract
class TestFakeBoost:
    @pytest.mark.contract
    def test_boost_returns_unmodified(self):
        b = FakeBoost()
        items = [{"score": 1.0}]
        # `is` is intentional — the contract is that no-op boost returns the
        # same list object, not just an equal one. SonarCloud python:S6738
        # flags this; rule is a false positive in this case.
        assert b.boost(items, "q", {}) is items  # NOSONAR(python:S6738) — identity check is the contract


@pytest.mark.contract
class TestFakeScorer:
    @pytest.mark.contract
    def test_returns_configured_score(self):
        s = FakeScorer(score=0.75)
        assert s.score(["a.md"], [{"path": "a.md"}]) == pytest.approx(0.75)

    @pytest.mark.contract
    def test_default_score_is_one(self):
        s = FakeScorer()
        assert s.score([], []) == pytest.approx(1.0)


@pytest.mark.contract
class TestFakeSearchLogger:
    @pytest.mark.contract
    def test_log_search_captures_event(self):
        logger = FakeSearchLogger()
        logger.log_search({"query": "test", "results": 5})
        assert len(logger.events) == 1
        assert logger.events[0]["query"] == "test"

    @pytest.mark.contract
    def test_log_query_captures_event(self):
        logger = FakeSearchLogger()
        logger.log_query({"query": "hello"})
        assert len(logger.events) == 1

    @pytest.mark.contract
    def test_multiple_events_accumulated(self):
        logger = FakeSearchLogger()
        logger.log_search({"a": 1})
        logger.log_query({"b": 2})
        assert len(logger.events) == 2


# ---------------------------------------------------------------------------
# Phase 3: FusionStrategy implementations
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestFusionStrategyImplementations:
    """Real FusionStrategy implementations satisfy the protocol."""

    @pytest.mark.contract
    def test_rrf_fusion_satisfies_protocol(self):
        assert isinstance(RRFFusion(), FusionStrategy)

    @pytest.mark.contract
    def test_bm25_primary_fusion_satisfies_protocol(self):
        assert isinstance(BM25PrimaryFusion(), FusionStrategy)

    @pytest.mark.contract
    def test_rrf_fusion_custom_k(self):
        f = RRFFusion(k=30)
        assert f._k == 30

    @pytest.mark.contract
    def test_rrf_fusion_fuse_empty(self):
        f = RRFFusion()
        assert f.fuse([], []) == []

    @pytest.mark.contract
    def test_bm25_primary_fusion_fuse_empty(self):
        f = BM25PrimaryFusion()
        assert f.fuse([], []) == []


# ---------------------------------------------------------------------------
# Phase 3: BoostStrategy implementations
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestBoostStrategyImplementations:
    """Real BoostStrategy implementations satisfy the protocol."""

    @pytest.mark.contract
    def test_entity_boost_satisfies_protocol(self):
        graph = FakeGraphRepository(available=False)
        assert isinstance(EntityBoost(graph=graph), BoostStrategy)

    @pytest.mark.contract
    def test_procedural_boost_satisfies_protocol(self):
        assert isinstance(ProceduralBoost(), BoostStrategy)

    @pytest.mark.contract
    def test_temporal_date_boost_satisfies_protocol(self):
        assert isinstance(TemporalDateBoost(), BoostStrategy)

    @pytest.mark.contract
    def test_chunk_date_boost_satisfies_protocol(self):
        assert isinstance(ChunkDateBoost(), BoostStrategy)

    @pytest.mark.contract
    def test_entity_boost_empty_results(self):
        graph = FakeGraphRepository(available=False)
        b = EntityBoost(graph=graph)
        assert b.boost([], "q", {}) == []

    @pytest.mark.contract
    def test_procedural_boost_empty_results(self):
        b = ProceduralBoost()
        assert b.boost([], "q", {}) == []

    @pytest.mark.contract
    def test_temporal_date_boost_empty_results(self):
        b = TemporalDateBoost()
        assert b.boost([], "q", {}) == []

    @pytest.mark.contract
    def test_chunk_date_boost_empty_results(self):
        b = ChunkDateBoost()
        assert b.boost([], "q", {}) == []


# ---------------------------------------------------------------------------
# Phase 3: ScoringStrategy implementations
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestScoringStrategyImplementations:
    """Real ScoringStrategy implementations satisfy the protocol."""

    @pytest.mark.contract
    def test_exact_match_scorer_satisfies_protocol(self):
        assert isinstance(ExactMatchScorer(), ScoringStrategy)

    @pytest.mark.contract
    def test_fuzzy_match_scorer_satisfies_protocol(self):
        assert isinstance(FuzzyMatchScorer(), ScoringStrategy)

    @pytest.mark.contract
    def test_ndcg_scorer_satisfies_protocol(self):
        assert isinstance(NDCGScorer(), ScoringStrategy)

    @pytest.mark.contract
    def test_llm_judge_scorer_satisfies_protocol(self):
        assert isinstance(LLMJudgeScorer(), ScoringStrategy)

    @pytest.mark.contract
    def test_exact_match_scorer_empty_gold(self):
        s = ExactMatchScorer()
        assert s.score(["a.md"], []) == pytest.approx(0.0)

    @pytest.mark.contract
    def test_fuzzy_match_scorer_empty_gold(self):
        s = FuzzyMatchScorer()
        assert s.score(["a.md"], []) == pytest.approx(0.0)

    @pytest.mark.contract
    def test_ndcg_scorer_empty_gold(self):
        s = NDCGScorer()
        assert s.score(["a.md"], []) == pytest.approx(0.0)

    @pytest.mark.contract
    def test_exact_match_scorer_hit(self):
        s = ExactMatchScorer(top_k=5)
        assert s.score(["docs/readme.md"], [{"path": "readme.md"}]) == pytest.approx(1.0)

    @pytest.mark.contract
    def test_fuzzy_match_scorer_hit(self):
        s = FuzzyMatchScorer(top_k=10)
        assert s.score(["docs/readme.md"], [{"path": "readme.md"}]) == pytest.approx(1.0)

    @pytest.mark.contract
    def test_ndcg_scorer_perfect(self):
        s = NDCGScorer(k=5)
        gold = [{"path": "a.md", "relevance": 2}]
        assert s.score(["a.md"], gold) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Phase 3: Scorer registry
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestScorerRegistry:
    """SCORERS registry maps names to ScoringStrategy classes."""

    @pytest.mark.contract
    def test_registry_contains_expected_keys(self):
        assert set(SCORERS.keys()) == {"exact", "fuzzy", "ndcg", "llm"}

    @pytest.mark.contract
    def test_all_registry_entries_are_scoring_strategies(self):
        from tests.fakes import FakeChatBackend

        for name, cls in SCORERS.items():
            instance = cls(chat_backend=FakeChatBackend(responses=["0.5"])) if name == "llm" else cls()
            assert isinstance(instance, ScoringStrategy), f"{name} does not satisfy ScoringStrategy"


# ---------------------------------------------------------------------------
# Phase 4: Adapter contract tests — BM25SearchBackend, VectorSearchBackend, AzureEmbeddingService
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestBM25SearchBackendAdapter:
    """BM25SearchBackend wraps DocumentRepository.search_fts correctly."""

    @pytest.mark.contract
    def test_search_delegates_to_doc_repo(self):
        docs = [
            {
                "path": "a.md",
                "title": "A",
                "content": "architecture patterns",
                "collection": "notes",
            },
        ]
        repo = FakeDocumentRepository(documents=docs)
        backend = BM25SearchBackend(repo)
        results = backend.search("architecture")
        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0]["path"] == "a.md"

    @pytest.mark.contract
    def test_search_passes_collections(self):
        docs = [
            {"path": "a.md", "title": "A", "content": "match", "collection": "notes"},
            {"path": "b.md", "title": "B", "content": "match", "collection": "archive"},
        ]
        repo = FakeDocumentRepository(documents=docs)
        backend = BM25SearchBackend(repo)
        results = backend.search("match", collections=["notes"])
        assert len(results) == 1
        assert results[0]["collection"] == "notes"

    @pytest.mark.contract
    def test_search_passes_limit(self):
        docs = [{"path": f"{i}.md", "title": str(i), "content": "match", "collection": "c"} for i in range(10)]
        repo = FakeDocumentRepository(documents=docs)
        backend = BM25SearchBackend(repo)
        results = backend.search("match", limit=3)
        assert len(results) == 3

    @pytest.mark.contract
    def test_search_returns_empty_for_no_match(self):
        repo = FakeDocumentRepository(documents=[{"path": "a.md", "content": "hello", "collection": "c"}])
        backend = BM25SearchBackend(repo)
        results = backend.search("nonexistent term xyz")
        assert results == []

    @pytest.mark.contract
    def test_search_with_empty_repo(self):
        backend = BM25SearchBackend(FakeDocumentRepository())
        assert backend.search("anything") == []


@pytest.mark.contract
class TestVectorSearchBackendAdapter:
    """VectorSearchBackend wraps EmbeddingService + VectorRepository correctly."""

    @pytest.mark.contract
    def test_search_embeds_and_queries(self):
        vec_results = [{"path": "a.md", "distance": 0.1, "collection": "c"}]
        embedding = FakeEmbeddingService()
        vector_repo = FakeVectorRepository(results=vec_results)
        backend = VectorSearchBackend(embedding, vector_repo)
        results = backend.search("semantic query")
        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0]["path"] == "a.md"

    @pytest.mark.contract
    def test_search_raises_when_embedding_returns_no_vector(self):
        """An empty embedding from the EmbeddingService is a failure — the
        backend now propagates instead of silently returning ``[]``, so the
        pipeline's ``vec_failed`` flag honestly reflects backend health.
        Previously the silent-empty return masked broken embeddings as
        successful no-match queries.
        """

        class _FailingEmbedding:
            def embed(self, text: str) -> list[float]:
                return []

            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                return [[] for _ in texts]

        vector_repo = FakeVectorRepository(results=[{"path": "a.md"}])
        backend = VectorSearchBackend(_FailingEmbedding(), vector_repo)
        with pytest.raises(RuntimeError, match="embedding service returned no vector"):
            backend.search("query")

    @pytest.mark.contract
    def test_search_passes_collections(self):
        vec_results = [
            {"path": "a.md", "distance": 0.1, "collection": "notes"},
            {"path": "b.md", "distance": 0.2, "collection": "archive"},
        ]
        embedding = FakeEmbeddingService()
        vector_repo = FakeVectorRepository(results=vec_results)
        backend = VectorSearchBackend(embedding, vector_repo)
        results = backend.search("query", collections=["notes"])
        assert len(results) == 1

    @pytest.mark.contract
    def test_search_passes_limit(self):
        vec_results = [{"path": f"{i}.md", "distance": 0.1, "collection": "c"} for i in range(10)]
        embedding = FakeEmbeddingService()
        vector_repo = FakeVectorRepository(results=vec_results)
        backend = VectorSearchBackend(embedding, vector_repo)
        results = backend.search("query", limit=3)
        assert len(results) == 3

    @pytest.mark.contract
    def test_search_with_empty_vector_repo(self):
        embedding = FakeEmbeddingService()
        vector_repo = FakeVectorRepository()
        backend = VectorSearchBackend(embedding, vector_repo)
        assert backend.search("anything") == []


@pytest.mark.contract
class TestAzureEmbeddingServiceAdapter:
    """AzureEmbeddingService satisfies EmbeddingService protocol."""

    @pytest.mark.contract
    def test_satisfies_embedding_service_protocol(self):
        svc = AzureEmbeddingService()
        assert isinstance(svc, EmbeddingService)

    @pytest.mark.contract
    def test_has_embed_method(self):
        svc = AzureEmbeddingService()
        assert callable(svc.embed)

    @pytest.mark.contract
    def test_has_embed_batch_method(self):
        svc = AzureEmbeddingService()
        assert callable(svc.embed_batch)
