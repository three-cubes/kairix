"""
Integration tests for SearchPipeline composed end-to-end with canonical fakes.

Each test wires a real SearchPipeline through real fusion + real boost
adapters, with backend / graph / logger / classifier supplied by canonical
fakes from tests/fakes.py. No @patch, no monkeypatch, no inline fakes —
boundaries cross multiple components from query in to SearchResult out.

Coverage focus:
  - SEMANTIC, KEYWORD, PROCEDURAL, TEMPORAL, ENTITY intent dispatch
  - RRF fusion with mixed BM25 + vector inputs
  - Boost chain ordering (entity -> procedural -> temporal-date) and
    intent gating (procedural boost no-op for non-PROCEDURAL intent)
  - SearchResult field semantics: intent, results, fallback_used, vec_failed,
    bm25_count, vec_count, fused_count, collections
  - Never-raises invariant under combined component failures
  - Logger receives serialised event with stable schema
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.core.protocols import GraphRepository
from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.boosts import (
    EntityBoost,
    ProceduralBoost,
    TemporalDateBoost,
)
from kairix.core.search.config import (
    EntityBoostConfig,
    ProceduralBoostConfig,
    RetrievalConfig,
    TemporalBoostConfig,
)
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchPipeline
from kairix.core.search.scope import Scope
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
# Fixture data — 5-10 docs spanning runbook, journal, semantic, entity files
# ---------------------------------------------------------------------------


def _corpus() -> list[dict[str, Any]]:
    """Return 8 canonical documents covering the boost-pattern surface area.

    Each doc has both `path` (FakeDocumentRepository keying) and `file`
    (BM25 result key consumed by RRFFusion) so the BM25 -> RRF -> boost
    chain works on real data.
    """
    return [
        {
            "path": "runbooks/how-to-deploy.md",
            "file": "runbooks/how-to-deploy.md",
            "collection": "shared",
            "title": "How to Deploy",
            "snippet": "Step-by-step deployment runbook for Kairix services.",
            "content": "deploy kairix services with confidence",
            "score": 0.9,
        },
        {
            "path": "runbooks/runbook-incident-response.md",
            "file": "runbooks/runbook-incident-response.md",
            "collection": "shared",
            "title": "Incident Response",
            "snippet": "Runbook for incident response.",
            "content": "incident response procedure",
            "score": 0.8,
        },
        {
            "path": "journal/2026-05-09-release.md",
            "file": "journal/2026-05-09-release.md",
            "collection": "shared",
            "title": "Release Journal 2026-05-09",
            "snippet": "Journal entry for release on 2026-05-09.",
            "content": "release notes for 2026-05-09",
            "score": 0.7,
        },
        {
            "path": "journal/2026-04-12-meeting.md",
            "file": "journal/2026-04-12-meeting.md",
            "collection": "shared",
            "title": "Meeting Notes 2026-04-12",
            "snippet": "Meeting notes from 2026-04-12.",
            "content": "meeting notes from April",
            "score": 0.6,
        },
        {
            "path": "concepts/architecture.md",
            "file": "concepts/architecture.md",
            "collection": "shared",
            "title": "Architecture Patterns",
            "snippet": "Protocols, pipelines, factories, repositories.",
            "content": "architecture patterns and design principles",
            "score": 0.5,
        },
        {
            "path": "person/jane-doe.md",
            "file": "person/jane-doe.md",
            "collection": "shared",
            "title": "Jane Doe",
            "snippet": "Profile for Jane Doe — Engineering Lead.",
            "content": "jane doe engineering lead profile",
            "score": 0.4,
        },
        {
            "path": "agent-alpha/notes.md",
            "file": "agent-alpha/notes.md",
            "collection": "agent-alpha",
            "title": "Alpha Notes",
            "snippet": "Notes scoped to agent-alpha collection.",
            "content": "agent-alpha working notes",
            "score": 0.3,
        },
        {
            "path": "agent-beta/notes.md",
            "file": "agent-beta/notes.md",
            "collection": "agent-beta",
            "title": "Beta Notes",
            "snippet": "Notes scoped to agent-beta collection.",
            "content": "agent-beta working notes",
            "score": 0.2,
        },
    ]


def _vec_results() -> list[dict[str, Any]]:
    """Return vector search results — overlapping + disjoint with BM25 corpus.

    First entry is BM25-disjoint (vector-only). Second entry overlaps with
    a BM25 doc (architecture.md) so RRF can fuse a shared document.
    """
    return [
        {
            "hash_seq": "abc-1",
            "distance": 0.10,
            "path": "concepts/semantic-only.md",
            "collection": "shared",
            "title": "Semantic Only",
            "snippet": "A document only the vector index knows about.",
        },
        {
            "hash_seq": "abc-2",
            "distance": 0.20,
            "path": "concepts/architecture.md",
            "collection": "shared",
            "title": "Architecture Patterns",
            "snippet": "Protocols, pipelines, factories, repositories.",
        },
    ]


# ---------------------------------------------------------------------------
# Pipeline factory — composes a real SearchPipeline with canonical fakes
# ---------------------------------------------------------------------------


def _build_pipeline(
    *,
    intent: QueryIntent = QueryIntent.SEMANTIC,
    docs: list[dict[str, Any]] | None = None,
    vec: list[dict[str, Any]] | None = None,
    graph_available: bool = True,
    boosts_enabled: bool = True,
    config: RetrievalConfig | None = None,
    logger: FakeSearchLogger | None = None,
) -> tuple[SearchPipeline, FakeSearchLogger, FakeGraphRepository]:
    """Compose a SearchPipeline with the boost chain wired end-to-end.

    Returns (pipeline, logger, graph) so tests can introspect the logger and
    graph after .search() runs.
    """
    docs = _corpus() if docs is None else docs
    vec = _vec_results() if vec is None else vec
    cfg = config if config is not None else RetrievalConfig.defaults()

    doc_repo = FakeDocumentRepository(documents=docs)
    vec_repo = FakeVectorRepository(results=vec)
    embed = FakeEmbeddingService(dim=8)
    graph = FakeGraphRepository(available=graph_available)
    fake_logger = logger if logger is not None else FakeSearchLogger()

    boost_chain: list[Any] = []
    if boosts_enabled:
        boost_chain = [
            EntityBoost(graph, EntityBoostConfig(enabled=True)),
            ProceduralBoost(ProceduralBoostConfig(enabled=True)),
            TemporalDateBoost(
                TemporalBoostConfig(date_path_boost_enabled=True),
            ),
        ]

    pipeline = SearchPipeline(
        classifier=FakeClassifier(intent=intent),
        bm25=BM25SearchBackend(doc_repo),
        vector=VectorSearchBackend(embed, vec_repo),
        graph=graph,
        fusion=RRFFusion(k=60),
        boosts=boost_chain,
        logger=fake_logger,
        config=cfg,
    )
    return pipeline, fake_logger, graph


# ---------------------------------------------------------------------------
# SEMANTIC intent — full BM25 + vector fusion via RRF
# ---------------------------------------------------------------------------


def test_semantic_query_fuses_bm25_and_vector_into_ranked_results() -> None:
    """SEMANTIC query: both backends return results, RRF fuses, budget caps."""
    pipeline, logger, _ = _build_pipeline(intent=QueryIntent.SEMANTIC)

    result = pipeline.search("architecture patterns")

    # Intent
    assert result.intent == QueryIntent.SEMANTIC

    # BM25: corpus contains "architecture patterns" (concepts/architecture.md)
    assert result.bm25_count == 1, f"expected 1 BM25 hit for 'architecture patterns', got {result.bm25_count}"
    # Vector returned 2 configured hits
    assert result.vec_count == 2, f"expected 2 vec hits, got {result.vec_count}"

    # RRF fuses overlap → 2 distinct docs (architecture.md shared, semantic-only.md vector-only)
    assert result.fused_count == 2, f"expected 2 fused (1 overlap + 1 vector-only), got {result.fused_count}"

    # Budget keeps results — non-empty since there's content
    assert len(result.results) == 2

    # Diagnostic invariants
    assert result.fallback_used is False, "BM25 returned hits → no fallback"
    assert result.vec_failed is False
    assert result.error == ""
    assert result.latency_ms >= 0.0
    assert result.collections == [], "collections=None passed → empty list in result"

    # Logger captured event with full schema
    assert len(logger.events) == 1
    event = logger.events[0]
    assert event["intent"] == "semantic"
    assert event["bm25_count"] == 1
    assert event["vec_count"] == 2
    assert event["fused_count"] == 2
    assert event["fallback_used"] is False
    assert event["vec_failed"] is False


# ---------------------------------------------------------------------------
# KEYWORD intent — same fusion path, no rerank/temporal/procedural boost effect
# ---------------------------------------------------------------------------


def test_keyword_query_returns_results_without_temporal_or_procedural_modification() -> None:
    """KEYWORD intent: boost chain runs but neither procedural nor temporal
    matches a non-procedural / non-dated query, so the score order from RRF
    survives untouched."""
    pipeline, _, _ = _build_pipeline(intent=QueryIntent.KEYWORD)

    result = pipeline.search("architecture")

    assert result.intent == QueryIntent.KEYWORD
    assert result.bm25_count == 1  # architecture.md content match
    assert result.vec_count == 2
    assert result.fused_count == 2
    assert len(result.results) == 2
    # Top result must be the doc that's in BOTH BM25 and vec (architecture.md)
    top = result.results[0]
    assert top.result.path == "concepts/architecture.md"
    assert top.result.in_bm25 is True
    assert top.result.in_vec is True


# ---------------------------------------------------------------------------
# PROCEDURAL intent — procedural boost lifts how-to / runbook paths
# ---------------------------------------------------------------------------


def test_procedural_query_boosts_runbook_paths_to_top_of_results() -> None:
    """PROCEDURAL intent: ProceduralBoost multiplies boosted_score for paths
    matching how-to-/runbook- patterns, lifting them above non-procedural
    results returned at higher RRF rank."""
    docs = _corpus()
    # Vector returns a non-procedural doc with stronger raw rank
    vec = [
        {
            "hash_seq": "v-1",
            "distance": 0.05,
            "path": "concepts/architecture.md",
            "collection": "shared",
            "title": "Architecture",
            "snippet": "non-procedural content.",
        },
    ]
    pipeline, _, _ = _build_pipeline(
        intent=QueryIntent.PROCEDURAL,
        docs=docs,
        vec=vec,
    )

    result = pipeline.search("deploy")  # matches how-to-deploy.md content

    assert result.intent == QueryIntent.PROCEDURAL
    assert result.bm25_count == 1
    assert result.fused_count == 2

    # how-to-deploy.md must be ranked above architecture.md after procedural boost
    paths = [br.result.path for br in result.results]
    assert "runbooks/how-to-deploy.md" in paths
    deploy_idx = paths.index("runbooks/how-to-deploy.md")
    arch_idx = paths.index("concepts/architecture.md")
    assert deploy_idx < arch_idx, f"procedural boost should lift how-to-deploy above architecture, got order {paths}"


# ---------------------------------------------------------------------------
# TEMPORAL intent — date-path boost lifts dated journal entries
# ---------------------------------------------------------------------------


def test_temporal_query_with_iso_date_boosts_matching_journal_entry() -> None:
    """TEMPORAL intent + ISO date in query: TemporalDateBoost lifts journal
    entry whose path contains the queried date above unrelated docs.

    The vector backend returns BOTH the dated journal entry and a generic
    architecture doc. With the ISO date "2026-05-09" in the query, the
    temporal boost multiplies the dated doc's boosted_score, lifting it
    above the architecture doc that came back at a stronger raw vector rank.
    """
    vec = [
        # Architecture doc: best raw vector rank (distance 0.05)
        {
            "hash_seq": "v-arch",
            "distance": 0.05,
            "path": "concepts/architecture.md",
            "collection": "shared",
            "title": "Architecture",
            "snippet": "Generic non-temporal content.",
        },
        # Dated journal: weaker raw vector rank (distance 0.30)
        {
            "hash_seq": "v-dated",
            "distance": 0.30,
            "path": "journal/2026-05-09-release.md",
            "collection": "shared",
            "title": "Release Journal 2026-05-09",
            "snippet": "Journal entry for release on 2026-05-09.",
        },
    ]
    pipeline, _, _ = _build_pipeline(intent=QueryIntent.TEMPORAL, vec=vec)

    result = pipeline.search("2026-05-09")

    assert result.intent == QueryIntent.TEMPORAL
    # BM25 matches "2026-05-09" substring in journal entry content
    assert result.bm25_count == 1
    assert result.vec_count == 2

    # Both docs in fused output; dated journal must be ranked first AFTER boost
    paths = [br.result.path for br in result.results]
    assert "journal/2026-05-09-release.md" in paths
    assert paths[0] == "journal/2026-05-09-release.md", (
        f"temporal date boost should put the dated journal first, got {paths}"
    )


# ---------------------------------------------------------------------------
# ENTITY intent — graph unavailable → structured error result
# ---------------------------------------------------------------------------


def test_entity_query_with_unavailable_graph_returns_neo4j_error() -> None:
    """ENTITY intent with graph.available=False short-circuits to an error
    result. BM25/vector are not even invoked."""
    pipeline, logger, _ = _build_pipeline(
        intent=QueryIntent.ENTITY,
        graph_available=False,
    )

    result = pipeline.search("tell me about Jane Doe")

    assert result.intent == QueryIntent.ENTITY
    assert "Neo4j" in result.error
    assert result.results == []
    assert result.bm25_count == 0
    assert result.vec_count == 0
    # Short-circuit means logger is NOT called for ENTITY-without-graph
    assert logger.events == []


def test_entity_query_with_available_graph_runs_full_pipeline() -> None:
    """ENTITY intent with graph.available=True proceeds through fusion + boosts."""
    pipeline, logger, _ = _build_pipeline(
        intent=QueryIntent.ENTITY,
        graph_available=True,
    )

    result = pipeline.search("Jane Doe")

    assert result.intent == QueryIntent.ENTITY
    assert result.error == ""
    assert result.bm25_count >= 1
    # Logger emitted event because we didn't short-circuit
    assert len(logger.events) == 1
    assert logger.events[0]["intent"] == "entity"


# ---------------------------------------------------------------------------
# Fallback semantics — BM25 empty + vec non-empty → fallback_used=True
# ---------------------------------------------------------------------------


def test_fallback_used_when_bm25_empty_but_vector_has_results() -> None:
    """fallback_used is True iff BM25 returned no results AND vector did."""
    # Use a query that BM25 cannot match against any corpus content, but
    # vec_repo always returns its configured results regardless of query.
    pipeline, logger, _ = _build_pipeline(intent=QueryIntent.SEMANTIC)

    result = pipeline.search("zzz_no_substring_match_zzz")

    assert result.bm25_count == 0
    assert result.vec_count == 2  # vector still returned its configured docs
    assert result.fallback_used is True
    assert result.vec_failed is False
    # Logger event mirrors the SearchResult fallback flag
    assert logger.events[0]["fallback_used"] is True


def test_fallback_not_used_when_both_backends_empty() -> None:
    """fallback_used is False when BOTH BM25 and vector return empty."""
    pipeline, _, _ = _build_pipeline(
        intent=QueryIntent.SEMANTIC,
        vec=[],  # empty vector results
    )

    result = pipeline.search("zzz_no_match")

    assert result.bm25_count == 0
    assert result.vec_count == 0
    assert result.fallback_used is False
    # Empty vec_results from a successful vector call ≠ vec_failed.
    # vec_failed is reserved for genuine backend failures (an exception),
    # not "vector search ran fine and returned 0 hits".
    assert result.vec_failed is False


# ---------------------------------------------------------------------------
# Vector skip — config.skip_vector=True bypasses vector entirely
# ---------------------------------------------------------------------------


def test_skip_vector_config_disables_vector_path_and_unsets_vec_failed() -> None:
    """skip_vector=True → vec_count=0 AND vec_failed=False (didn't try, didn't fail)."""
    cfg = RetrievalConfig(skip_vector=True, fusion_strategy="rrf")
    pipeline, logger, _ = _build_pipeline(
        intent=QueryIntent.SEMANTIC,
        config=cfg,
    )

    result = pipeline.search("architecture")

    assert result.bm25_count == 1
    assert result.vec_count == 0
    assert result.vec_failed is False, "skip_vector=True must NOT mark vec_failed — we didn't try"
    # fallback_used is False because BM25 returned hits
    assert result.fallback_used is False
    assert logger.events[0]["vec_failed"] is False


# ---------------------------------------------------------------------------
# Collection scoping — explicit collections list narrows backends
# ---------------------------------------------------------------------------


def test_explicit_collections_list_filters_bm25_and_vector_results() -> None:
    """Pipeline forwards collections list to backends and surfaces it in result."""
    pipeline, logger, _ = _build_pipeline(intent=QueryIntent.SEMANTIC)

    result = pipeline.search("notes", collections=["agent-alpha"])

    # FakeDocumentRepository filters by collection — only agent-alpha matches
    assert result.bm25_count == 1
    assert result.results[0].result.path == "agent-alpha/notes.md"
    # Vector results have collection="shared" so they're filtered out
    assert result.vec_count == 0
    # Result + logger reflect the scope
    assert result.collections == ["agent-alpha"]
    assert logger.events[0]["collections_searched"] == ["agent-alpha"]


# ---------------------------------------------------------------------------
# Combined-failure invariant — pipeline NEVER raises
# ---------------------------------------------------------------------------


class _ExplodingClassifier:
    """Classifier that always raises — pipeline must catch and default."""

    def classify(self, query: str) -> QueryIntent:
        raise RuntimeError("classifier blew up")


class _ExplodingBoost:
    """Boost that always raises — pipeline must catch and skip."""

    def boost(self, results: list, query: str, context: dict) -> list:
        raise RuntimeError("boost blew up")


class _ExplodingLogger:
    """Logger that raises on every call — pipeline must swallow."""

    def log_search(self, event: dict) -> None:
        raise RuntimeError("log_search blew up")

    def log_query(self, event: dict) -> None:
        raise RuntimeError("log_query blew up")


def test_pipeline_never_raises_under_combined_component_failures() -> None:
    """Classifier raises, every boost raises, logger raises — pipeline still
    returns a SearchResult with intent defaulted to SEMANTIC and results
    populated from BM25+vector+RRF."""
    docs = _corpus()
    vec = _vec_results()

    pipeline = SearchPipeline(
        classifier=_ExplodingClassifier(),
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
        vector=VectorSearchBackend(
            FakeEmbeddingService(dim=8),
            FakeVectorRepository(results=vec),
        ),
        graph=FakeGraphRepository(available=True),
        fusion=RRFFusion(k=60),
        boosts=[_ExplodingBoost(), _ExplodingBoost(), _ExplodingBoost()],
        logger=_ExplodingLogger(),
        config=RetrievalConfig.defaults(),
    )

    # Must NOT raise
    result = pipeline.search("architecture")

    # Classifier failure → SEMANTIC default
    assert result.intent == QueryIntent.SEMANTIC
    # BM25 + vector still ran, RRF still fused
    assert result.bm25_count == 1
    assert result.vec_count == 2
    assert result.fused_count == 2
    assert len(result.results) == 2
    # Logger explosion was swallowed — result still returned cleanly
    assert result.error == ""


# ---------------------------------------------------------------------------
# Boost chain ordering — entity boost runs BEFORE procedural and temporal
# ---------------------------------------------------------------------------


class _OrderRecordingBoost:
    """Boost that records its name in a shared list and passes results through."""

    def __init__(self, name: str, log: list[str]) -> None:
        self._name = name
        self._log = log

    def boost(self, results: list, query: str, context: dict) -> list:
        self._log.append(self._name)
        return results


def test_boost_chain_runs_in_declared_order() -> None:
    """Boosts run in the order passed to SearchPipeline.boosts — earlier
    boosts feed their output into later ones."""
    log: list[str] = []
    pipeline = SearchPipeline(
        classifier=FakeClassifier(intent=QueryIntent.SEMANTIC),
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=_corpus())),
        vector=VectorSearchBackend(
            FakeEmbeddingService(dim=8),
            FakeVectorRepository(results=_vec_results()),
        ),
        graph=FakeGraphRepository(available=True),
        fusion=RRFFusion(k=60),
        boosts=[
            _OrderRecordingBoost("entity", log),
            _OrderRecordingBoost("procedural", log),
            _OrderRecordingBoost("temporal", log),
        ],
        logger=FakeSearchLogger(),
        config=RetrievalConfig.defaults(),
    )

    pipeline.search("architecture")

    assert log == ["entity", "procedural", "temporal"], f"boost chain must run in declared order, got {log}"


# ---------------------------------------------------------------------------
# Scope serialisation in logger event
# ---------------------------------------------------------------------------


def test_logger_event_serialises_scope_and_agent_for_observability() -> None:
    """Logger event carries serialised scope value and agent, the schema the
    multi-agent observability dashboard consumes."""
    pipeline, logger, _ = _build_pipeline(intent=QueryIntent.SEMANTIC)

    pipeline.search(
        "architecture",
        scope=Scope.ALL_AGENTS,
        agent="agent-alpha",
    )

    assert len(logger.events) == 1
    event = logger.events[0]
    assert event["scope"] == "all-agents"
    assert event["agent"] == "agent-alpha"
    # Stable schema: keys present even when values default
    for key in (
        "query_hash",
        "intent",
        "bm25_count",
        "vec_count",
        "fused_count",
        "total_tokens",
        "latency_ms",
        "vec_failed",
        "fallback_used",
        "ts",
    ):
        assert key in event, f"logger event missing key {key!r}"


# ---------------------------------------------------------------------------
# CollectionResolver protocol — operator misconfiguration surfaces as error
# ---------------------------------------------------------------------------


class _MisconfiguredResolver:
    """CollectionResolver that raises NotImplementedError for unknown scope.

    Mirrors production behaviour when ALL_AGENTS scope is requested but the
    AgentRegistry has no agents configured.
    """

    def resolve(self, agent: str | None, scope: Any) -> list[str] | None:
        raise NotImplementedError("ALL_AGENTS scope requires agents in agent-registry config")


def test_resolver_misconfiguration_surfaces_as_search_error() -> None:
    """Resolver raising NotImplementedError → SearchResult.error populated,
    pipeline does NOT continue to BM25/vector."""
    docs = _corpus()
    pipeline = SearchPipeline(
        classifier=FakeClassifier(intent=QueryIntent.SEMANTIC),
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
        vector=VectorSearchBackend(
            FakeEmbeddingService(dim=8),
            FakeVectorRepository(results=_vec_results()),
        ),
        graph=FakeGraphRepository(available=True),
        fusion=RRFFusion(k=60),
        boosts=[],
        logger=FakeSearchLogger(),
        resolver=_MisconfiguredResolver(),
        config=RetrievalConfig.defaults(),
    )

    result = pipeline.search("architecture", scope=Scope.ALL_AGENTS)

    assert "ALL_AGENTS" in result.error or "agents" in result.error
    assert result.results == []
    assert result.bm25_count == 0
    assert result.vec_count == 0


# ---------------------------------------------------------------------------
# Graph-context propagation — graph repo is available to boosts via context
# ---------------------------------------------------------------------------


class _ContextInspectingBoost:
    """Boost that captures the context dict for assertion."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] | None = None

    def boost(self, results: list, query: str, context: dict) -> list:
        self.captured = dict(context)
        return results


def test_pipeline_passes_intent_query_and_graph_in_boost_context() -> None:
    """Boost context dict contains intent, query, graph — the keys boosts
    rely on for intent gating and entity lookup."""
    inspector = _ContextInspectingBoost()
    graph: GraphRepository = FakeGraphRepository(available=True)
    pipeline = SearchPipeline(
        classifier=FakeClassifier(intent=QueryIntent.PROCEDURAL),
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=_corpus())),
        vector=VectorSearchBackend(
            FakeEmbeddingService(dim=8),
            FakeVectorRepository(results=_vec_results()),
        ),
        graph=graph,
        fusion=RRFFusion(k=60),
        boosts=[inspector],
        logger=FakeSearchLogger(),
        config=RetrievalConfig.defaults(),
    )

    pipeline.search("how to deploy")

    assert inspector.captured is not None
    assert inspector.captured["intent"] == QueryIntent.PROCEDURAL
    assert inspector.captured["query"] == "how to deploy"
    assert inspector.captured["graph"] is graph
