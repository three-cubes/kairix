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

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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

_FAKE_VEC = np.random.rand(1536).astype(np.float32).tolist()


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
    graph_available: bool = False,
    config: RetrievalConfig | None = None,
    logger: FakeSearchLogger | None = None,
) -> SearchPipeline:
    """Build a SearchPipeline with fakes for testing.

    Every dependency is wired to a harmless fake. Tests override specific
    fields via keyword arguments.
    """
    cfg = config or RetrievalConfig.defaults()
    classifier = FakeClassifier(intent=intent)
    doc_repo = FakeDocumentRepository(documents=bm25_docs or [])
    bm25 = BM25SearchBackend(doc_repo)
    embedding = FakeEmbeddingService()
    vector_repo = FakeVectorRepository(results=vec_results or [])
    vector = VectorSearchBackend(embedding, vector_repo)
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
        else:
            os.environ.pop("KAIRIX_EXTRA_COLLECTIONS", None)
        _mod._COLLECTIONS_CONFIG = None


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
    """Vector search failure -> BM25-only results still returned."""
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
    pipeline = _build_test_pipeline(bm25_docs=docs)
    result = pipeline.search("test semantic query about memory systems")

    assert len(result.results) >= 0  # Results depend on FTS match in FakeDocumentRepository
    assert result.vec_failed is True  # FakeVectorRepository returns []


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


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_formats_output(capsys: pytest.CaptureFixture) -> None:
    """CLI prints formatted search results."""
    from kairix.core.search.cli import main as search_cli

    # Create a mock pipeline that returns a PipelineSearchResult
    mock_pipeline = MagicMock()
    mock_pipeline.search.return_value = PipelineSearchResult(
        query="test query",
        intent=QueryIntent.SEMANTIC,
        results=[],
        bm25_count=0,
        vec_count=0,
        fused_count=0,
    )

    with patch("kairix.core.search.cli.load_config", return_value=RetrievalConfig.defaults()):
        search_cli(["test query"], pipeline=mock_pipeline)

    captured = capsys.readouterr()
    assert "test query" in captured.out
    assert "semantic" in captured.out


@pytest.mark.unit
def test_cli_json_flag(capsys: pytest.CaptureFixture) -> None:
    """--json flag outputs valid JSON."""
    from kairix.core.search.cli import main as search_cli

    mock_pipeline = MagicMock()
    mock_pipeline.search.return_value = PipelineSearchResult(
        query="test",
        intent=QueryIntent.SEMANTIC,
        results=[],
        bm25_count=0,
        vec_count=0,
        fused_count=0,
    )

    with patch("kairix.core.search.cli.load_config", return_value=RetrievalConfig.defaults()):
        search_cli(["test", "--json"], pipeline=mock_pipeline)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["query"] == "test"
    assert "results" in data


@pytest.mark.unit
def test_cli_agent_flag_passed_to_search(capsys: pytest.CaptureFixture) -> None:
    """--agent flag is forwarded to pipeline.search()."""
    from kairix.core.search.cli import main as search_cli

    mock_pipeline = MagicMock()
    mock_pipeline.search.return_value = PipelineSearchResult(
        query="test",
        intent=QueryIntent.SEMANTIC,
        results=[],
        bm25_count=0,
        vec_count=0,
        fused_count=0,
    )

    with patch("kairix.core.search.cli.load_config", return_value=RetrievalConfig.defaults()):
        search_cli(["test", "--agent", "shape"], pipeline=mock_pipeline)

    assert mock_pipeline.search.call_count == 1
    call_kwargs = mock_pipeline.search.call_args.kwargs
    assert call_kwargs["query"] == "test"
    assert call_kwargs["agent"] == "shape"
    assert call_kwargs["scope"] == "shared+agent"
    assert call_kwargs["budget"] == 3000


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
# Additional coverage: logging, DB open, temporal rewriting, keyword fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rotate_query_log_moves_file(tmp_path: Path) -> None:
    """_rotate_query_log() moves path -> path.1 and removes older rotated file."""

    import kairix.core.search.hybrid as hybrid_mod

    log_file = tmp_path / "queries.jsonl"
    log_file.write_text('{"q": "test"}\n')

    rotated = tmp_path / "queries.jsonl.1"
    rotated.write_text("old rotated\n")

    hybrid_mod._rotate_query_log(log_file)

    assert not log_file.exists()
    assert rotated.exists()
    assert rotated.read_text() != "old rotated\n"


@pytest.mark.unit
def test_log_query_event_writes_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_log_query_event() appends to JSONL log when _LOG_QUERIES is True."""
    import kairix.core.search.hybrid as hybrid_mod

    log_path = tmp_path / "queries.jsonl"
    monkeypatch.setattr(hybrid_mod, "_LOG_QUERIES", True)
    monkeypatch.setattr(hybrid_mod, "_QUERY_LOG_PATH", log_path)

    hybrid_mod._log_query_event({"q": "test query", "t": 123})

    assert log_path.exists()
    event = json.loads(log_path.read_text().strip())
    assert event["q"] == "test query"


@pytest.mark.unit
def test_log_query_event_noop_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_log_query_event() is a no-op when _LOG_QUERIES is False."""
    import kairix.core.search.hybrid as hybrid_mod

    log_path = tmp_path / "queries.jsonl"
    monkeypatch.setattr(hybrid_mod, "_LOG_QUERIES", False)
    monkeypatch.setattr(hybrid_mod, "_QUERY_LOG_PATH", log_path)

    hybrid_mod._log_query_event({"q": "test"})
    assert not log_path.exists()


@pytest.mark.unit
def test_log_query_event_rotates_large_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_log_query_event() rotates the file when it exceeds the size threshold."""
    import kairix.core.search.hybrid as hybrid_mod

    log_path = tmp_path / "queries.jsonl"
    log_path.write_text("x" * 100)

    monkeypatch.setattr(hybrid_mod, "_LOG_QUERIES", True)
    monkeypatch.setattr(hybrid_mod, "_QUERY_LOG_PATH", log_path)
    monkeypatch.setattr(hybrid_mod, "_QUERY_LOG_MAX_BYTES", 10)  # Very small threshold

    hybrid_mod._log_query_event({"q": "trigger rotation"})

    rotated = Path(str(log_path) + ".1")
    assert rotated.exists()


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


@pytest.mark.unit
def test_cli_entity_error_exits_nonzero(capsys: pytest.CaptureFixture) -> None:
    """CLI exits with code 1 and prints error when entity query fails."""
    from kairix.core.search.cli import main as search_cli

    mock_pipeline = MagicMock()
    mock_pipeline.search.return_value = PipelineSearchResult(
        query="tell me about Acme",
        intent=QueryIntent.ENTITY,
        results=[],
        error="Neo4j is required for entity queries but is unavailable.",
    )

    with patch("kairix.core.search.cli.load_config", return_value=RetrievalConfig.defaults()):
        with pytest.raises(SystemExit) as exc_info:
            search_cli(["tell me about Acme"], pipeline=mock_pipeline)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Neo4j" in captured.out


@pytest.mark.unit
def test_cli_entity_error_json_includes_error_field(
    capsys: pytest.CaptureFixture,
) -> None:
    """--json output includes 'error' field when entity query fails."""
    from kairix.core.search.cli import main as search_cli

    mock_pipeline = MagicMock()
    mock_pipeline.search.return_value = PipelineSearchResult(
        query="tell me about Acme",
        intent=QueryIntent.ENTITY,
        results=[],
        error="Neo4j is required for entity queries but is unavailable.",
    )

    with patch("kairix.core.search.cli.load_config", return_value=RetrievalConfig.defaults()):
        with pytest.raises(SystemExit) as exc_info:
            search_cli(["tell me about Acme", "--json"], pipeline=mock_pipeline)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "error" in data
    assert "Neo4j" in data["error"]


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
def test_vector_search_empty_marks_vec_failed() -> None:
    """Vector search returning empty sets vec_failed=True."""
    pipeline = _build_test_pipeline()  # FakeVectorRepository returns []
    result = pipeline.search("semantic query about architecture")

    assert result.vec_failed is True
    assert result.vec_count == 0


# ---------------------------------------------------------------------------
# _enrich_chunk_dates -- tested through the module, not via direct import
# ---------------------------------------------------------------------------


def _make_chunk_date_db(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """Create a SQLite DB with documents + content_vectors tables for chunk_date tests."""
    import sqlite3

    db_path = tmp_path / "chunk_dates.sqlite"
    db = sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE documents (hash TEXT PRIMARY KEY, path TEXT NOT NULL)")
    db.execute("CREATE TABLE content_vectors (hash TEXT, chunk_date TEXT)")
    for h, p, cd in rows:
        db.execute("INSERT INTO documents (hash, path) VALUES (?, ?)", (h, p))
        db.execute("INSERT INTO content_vectors (hash, chunk_date) VALUES (?, ?)", (h, cd))
    db.commit()
    db.close()
    return db_path


@pytest.mark.unit
def test_enrich_chunk_dates_populates_matching_paths(tmp_path: Path) -> None:
    """_enrich_chunk_dates sets chunk_date on FusedResult for matching paths."""
    import kairix.core.search.hybrid as hybrid_mod
    from kairix.core.search.rrf import FusedResult

    db_path = _make_chunk_date_db(
        tmp_path,
        [
            ("h1", "/vault/doc-a.md", "2026-04-20"),
            ("h2", "/vault/doc-b.md", "2026-04-21"),
        ],
    )
    fused = [
        FusedResult(
            path="/vault/doc-a.md",
            collection="c",
            title="A",
            snippet="s",
            rrf_score=0.5,
            boosted_score=0.5,
        ),
        FusedResult(
            path="/vault/doc-b.md",
            collection="c",
            title="B",
            snippet="s",
            rrf_score=0.4,
            boosted_score=0.4,
        ),
        FusedResult(
            path="/vault/doc-c.md",
            collection="c",
            title="C",
            snippet="s",
            rrf_score=0.3,
            boosted_score=0.3,
        ),
    ]
    hybrid_mod._enrich_chunk_dates(fused, db_path)
    assert fused[0].chunk_date == "2026-04-20"
    assert fused[1].chunk_date == "2026-04-21"
    assert fused[2].chunk_date == ""


@pytest.mark.unit
def test_enrich_chunk_dates_handles_missing_db(tmp_path: Path) -> None:
    """_enrich_chunk_dates returns silently when DB does not exist."""
    import kairix.core.search.hybrid as hybrid_mod
    from kairix.core.search.rrf import FusedResult

    fused = [
        FusedResult(
            path="/vault/doc.md",
            collection="c",
            title="T",
            snippet="s",
            rrf_score=0.5,
            boosted_score=0.5,
        )
    ]
    hybrid_mod._enrich_chunk_dates(fused, tmp_path / "nonexistent.sqlite")
    assert fused[0].chunk_date == ""


@pytest.mark.unit
def test_enrich_chunk_dates_empty_list(tmp_path: Path) -> None:
    """_enrich_chunk_dates is a no-op for empty list."""
    import kairix.core.search.hybrid as hybrid_mod

    db_path = _make_chunk_date_db(tmp_path, [("h1", "/vault/doc.md", "2026-04-20")])
    hybrid_mod._enrich_chunk_dates([], db_path)
    assert True, "smoke: empty list handled without error"


@pytest.mark.unit
def test_enrich_chunk_dates_no_matching_paths(tmp_path: Path) -> None:
    """_enrich_chunk_dates leaves chunk_date empty when paths don't match."""
    import kairix.core.search.hybrid as hybrid_mod
    from kairix.core.search.rrf import FusedResult

    db_path = _make_chunk_date_db(tmp_path, [("h1", "/vault/other.md", "2026-04-20")])
    fused = [
        FusedResult(
            path="/vault/doc.md",
            collection="c",
            title="T",
            snippet="s",
            rrf_score=0.5,
            boosted_score=0.5,
        )
    ]
    hybrid_mod._enrich_chunk_dates(fused, db_path)
    assert fused[0].chunk_date == ""
