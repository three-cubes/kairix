"""
Integration tests for the boosts strategies wired into a real SearchPipeline.

These tests exercise the full pipeline — IntentClassifier -> Fusion -> Boost
chain -> Budget — using canonical fakes from tests.fakes for everything that
crosses a boundary (DocumentRepository, VectorRepository, GraphRepository,
EmbeddingService). No monkeypatching, no @patch.

The boost layer is exercised through SearchPipeline.search() rather than via
direct boost.boost() calls so we get coverage of the realistic call site:
the pipeline-built context dict, intent gating, and chained boosts.
"""

from __future__ import annotations

import pytest

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.boosts import (
    ChunkDateBoost,
    EntityBoost,
    ProceduralBoost,
    TemporalDateBoost,
)
from kairix.core.search.config import (
    ProceduralBoostConfig,
    RetrievalConfig,
    TemporalBoostConfig,
)
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchPipeline
from tests.fakes import (
    FakeBoost,
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers — canonical-fakes-only pipeline construction
# ---------------------------------------------------------------------------


def _bm25_row(path: str, title: str = "T", score: float = 1.0) -> dict:
    return {
        "file": path,
        "title": title,
        "snippet": title,
        "score": score,
        "collection": "kb",
    }


def _vec_row(path: str, title: str = "T", distance: float = 0.1) -> dict:
    return {
        "hash_seq": "h_0",
        "distance": distance,
        "path": path,
        "collection": "kb",
        "title": title,
        "snippet": title,
    }


def _build_pipeline(
    *,
    intent: QueryIntent,
    boosts: list,
    bm25_rows: list,
    vec_rows: list,
    graph: FakeGraphRepository | None = None,
    config: RetrievalConfig | None = None,
) -> SearchPipeline:
    """Wire a SearchPipeline using canonical fakes.

    BM25 rows are returned by the fake DocumentRepository.search_fts; they
    must contain the query token (any of them — search_fts matches against
    title+content). The simplest pattern: put the query token in every doc.
    """
    cfg = config or RetrievalConfig.minimal()

    # Canonical fake in scripted mode: bm25_rows is the BM25Result-shaped
    # list returned verbatim by search_fts. See tests/fakes.py.
    doc_repo = FakeDocumentRepository(bm25_rows=bm25_rows)
    bm25 = BM25SearchBackend(doc_repo)

    # Vector backend
    embedding = FakeEmbeddingService()
    vector_repo = FakeVectorRepository(results=vec_rows)
    vector = VectorSearchBackend(embedding, vector_repo)

    # Graph (defaults to unavailable — entity boost is a no-op)
    graph_repo = graph if graph is not None else FakeGraphRepository(available=False)

    # Fusion: RRF (deterministic, simple)
    fusion = RRFFusion(k=cfg.rrf_k)

    return SearchPipeline(
        classifier=FakeClassifier(intent=intent),
        bm25=bm25,
        vector=vector,
        graph=graph_repo,
        fusion=fusion,
        boosts=boosts,
        logger=FakeSearchLogger(),
        config=cfg,
    )


# ---------------------------------------------------------------------------
# EntityBoost wired into SearchPipeline
# ---------------------------------------------------------------------------


class TestEntityBoostInPipeline:
    def test_entity_match_lifts_doc_to_top_one(self) -> None:
        """End-to-end: an entity-matched document overtakes a non-matching
        document with a similar base RRF score.
        """
        graph = FakeGraphRepository(
            entities=[
                {
                    "name": "OpenClaw",
                    "vault_path": "concept/openclaw.md",
                    "labels": ["concept"],
                    "in_degree": 50,
                }
            ],
            available=True,
        )

        # Both documents tied in the BM25/vector inputs at rank 1.
        bm25 = [_bm25_row("notes/other.md"), _bm25_row("concept/openclaw.md")]
        vec = [_vec_row("concept/openclaw.md"), _vec_row("notes/other.md")]

        pipe = _build_pipeline(
            intent=QueryIntent.ENTITY,
            boosts=[EntityBoost(graph=graph)],
            bm25_rows=bm25,
            vec_rows=vec,
            graph=graph,
        )
        result = pipe.search("openclaw")
        assert result.results, "pipeline returned no results"
        # SearchPipeline returns BudgetedResult; the FusedResult lives on .result.
        top = result.results[0].result
        assert top.path == "concept/openclaw.md", (
            f"entity-matched doc should rank top-1, got {[r.result.path for r in result.results]}"
        )

    def test_entity_boost_skipped_when_graph_unavailable(self) -> None:
        """When graph.available is False, EntityBoost is a no-op — original
        RRF order is preserved.
        """
        graph = FakeGraphRepository(available=False)

        bm25 = [_bm25_row("notes/a.md"), _bm25_row("notes/b.md")]
        vec = [_vec_row("notes/a.md"), _vec_row("notes/b.md")]

        pipe = _build_pipeline(
            intent=QueryIntent.SEMANTIC,
            boosts=[EntityBoost(graph=graph)],
            bm25_rows=bm25,
            vec_rows=vec,
            graph=graph,
        )
        result = pipe.search("anything")

        # All rrf scores must equal boosted scores (no boost applied).
        for br in result.results:
            assert br.result.boosted_score == pytest.approx(br.result.rrf_score)


# ---------------------------------------------------------------------------
# ProceduralBoost wired into SearchPipeline
# ---------------------------------------------------------------------------


class TestProceduralBoostInPipeline:
    def test_procedural_doc_lifts_above_generic_doc(self) -> None:
        """End-to-end: a how-to document with the same base score as a
        general document overtakes it after the procedural boost runs.
        """
        bm25 = [_bm25_row("notes/general-decisions.md"), _bm25_row("guides/how-to-deploy.md")]
        vec = [_vec_row("guides/how-to-deploy.md"), _vec_row("notes/general-decisions.md")]

        pipe = _build_pipeline(
            intent=QueryIntent.PROCEDURAL,
            boosts=[ProceduralBoost(config=ProceduralBoostConfig(factor=1.4))],
            bm25_rows=bm25,
            vec_rows=vec,
        )
        result = pipe.search("how to deploy")

        assert result.results, "pipeline returned no results"
        top = result.results[0].result
        assert top.path == "guides/how-to-deploy.md", (
            f"procedural doc should rank top-1, got {[r.result.path for r in result.results]}"
        )

    def test_procedural_boost_disabled_via_config(self) -> None:
        """Disabled procedural boost is a no-op — original RRF order preserved."""
        bm25 = [_bm25_row("notes/general.md"), _bm25_row("guides/how-to-x.md")]
        vec: list = []

        pipe = _build_pipeline(
            intent=QueryIntent.PROCEDURAL,
            boosts=[ProceduralBoost(config=ProceduralBoostConfig(enabled=False))],
            bm25_rows=bm25,
            vec_rows=vec,
        )
        result = pipe.search("how to x")
        # Same boosted score as rrf score = no boost applied.
        for br in result.results:
            assert br.result.boosted_score == pytest.approx(br.result.rrf_score)


# ---------------------------------------------------------------------------
# TemporalDateBoost wired into SearchPipeline
# ---------------------------------------------------------------------------


class TestTemporalDateBoostInPipeline:
    def test_iso_date_in_query_lifts_matching_dated_path(self) -> None:
        """A query with an ISO date promotes the matching dated document."""
        cfg = TemporalBoostConfig(date_path_boost_enabled=True, date_path_boost_factor=1.5)

        bm25 = [_bm25_row("daily/2025-09-01.md"), _bm25_row("daily/2026-04-15.md")]
        vec = [_vec_row("daily/2025-09-01.md"), _vec_row("daily/2026-04-15.md")]

        pipe = _build_pipeline(
            intent=QueryIntent.TEMPORAL,
            boosts=[TemporalDateBoost(config=cfg)],
            bm25_rows=bm25,
            vec_rows=vec,
        )
        result = pipe.search("2026-04-15 standup")

        assert result.results, "no results"
        assert result.results[0].result.path == "daily/2026-04-15.md"

    def test_default_disabled_no_op(self) -> None:
        """Disabled-by-default TemporalDateBoost preserves RRF order."""
        bm25 = [_bm25_row("daily/2025-09-01.md"), _bm25_row("daily/2026-04-15.md")]
        vec: list = []

        pipe = _build_pipeline(
            intent=QueryIntent.TEMPORAL,
            boosts=[TemporalDateBoost()],  # default config = disabled
            bm25_rows=bm25,
            vec_rows=vec,
        )
        result = pipe.search("2026-04-15 standup")

        for br in result.results:
            assert br.result.boosted_score == pytest.approx(br.result.rrf_score)


# ---------------------------------------------------------------------------
# ChunkDateBoost wired into SearchPipeline
# ---------------------------------------------------------------------------


class TestChunkDateBoostInPipeline:
    """ChunkDateBoost reads context['query_date']. SearchPipeline does not
    populate query_date in the context (see kairix/core/search/pipeline.py
    line 159: context = {"intent", "query", "graph"}). So when the boost is
    plugged into the pipeline AS-IS, it is a no-op even when enabled.

    These tests pin that current behaviour so a future patch that wires
    query_date through the pipeline will surface as a *failed* contract on
    these very tests — which is what we want.
    """

    def test_pipeline_without_query_date_keeps_chunk_date_boost_no_op(self) -> None:
        """Pin: even with chunk_date_boost_enabled=True, current pipeline
        does not populate query_date so the boost is a no-op.

        Architectural note: this is a known gap, not a test bug — flagged
        in the integration test suite docstring.
        """
        cfg = TemporalBoostConfig(chunk_date_boost_enabled=True)

        bm25 = [_bm25_row("notes/a.md"), _bm25_row("notes/b.md")]
        vec: list = []

        pipe = _build_pipeline(
            intent=QueryIntent.TEMPORAL,
            boosts=[ChunkDateBoost(config=cfg)],
            bm25_rows=bm25,
            vec_rows=vec,
        )
        result = pipe.search("recent stuff")

        # No query_date in context => boost is a no-op.
        for br in result.results:
            assert br.result.boosted_score == pytest.approx(br.result.rrf_score)


# ---------------------------------------------------------------------------
# Boost chain order: entity then procedural
# ---------------------------------------------------------------------------


class TestBoostChainOrder:
    """The pipeline applies boosts in iteration order — claim from
    pipeline.py line 159-164.
    """

    def test_chained_entity_then_procedural_compound(self) -> None:
        """Entity match THEN procedural pattern => both factors apply
        multiplicatively to boosted_score.
        """
        graph = FakeGraphRepository(
            entities=[
                {
                    "name": "OpenClaw",
                    "vault_path": "guides/how-to-openclaw.md",
                    "labels": ["concept"],
                    "in_degree": 8,
                }
            ],
            available=True,
        )

        bm25 = [_bm25_row("guides/how-to-openclaw.md"), _bm25_row("notes/other.md")]
        vec = [_vec_row("guides/how-to-openclaw.md"), _vec_row("notes/other.md")]

        pipe = _build_pipeline(
            intent=QueryIntent.PROCEDURAL,
            boosts=[
                EntityBoost(graph=graph),
                ProceduralBoost(),
            ],
            bm25_rows=bm25,
            vec_rows=vec,
            graph=graph,
        )
        result = pipe.search("how to use openclaw")

        # The entity-and-procedural-matched doc must be top-1.
        top = result.results[0].result
        assert top.path == "guides/how-to-openclaw.md"
        # And its boosted_score must be strictly greater than the rrf_score
        # by *more* than what procedural alone would produce (factor=1.4).
        assert top.boosted_score > top.rrf_score * 1.4, "compound boost must exceed procedural-only boost"

    def test_fakeboost_in_chain_is_passthrough(self) -> None:
        """FakeBoost (canonical no-op) does not alter scores in the chain."""
        bm25 = [_bm25_row("a.md"), _bm25_row("b.md")]
        vec: list = []

        pipe = _build_pipeline(
            intent=QueryIntent.SEMANTIC,
            boosts=[FakeBoost()],
            bm25_rows=bm25,
            vec_rows=vec,
        )
        result = pipe.search("hello")
        for br in result.results:
            assert br.result.boosted_score == pytest.approx(br.result.rrf_score)


# ---------------------------------------------------------------------------
# Sanity: pipeline composes successfully with the default config + all boosts
# ---------------------------------------------------------------------------


class TestFullBoostStackSmoke:
    def test_all_four_boosts_composed_without_raising(self) -> None:
        """All four boost strategies can be chained together and run on the
        same pipeline without any of them raising — a smoke test for the
        'never raises' contract under the realistic full chain.
        """
        graph = FakeGraphRepository(
            entities=[
                {
                    "name": "Topic",
                    "vault_path": "concept/topic.md",
                    "labels": ["concept"],
                    "in_degree": 3,
                }
            ],
            available=True,
        )

        bm25 = [
            _bm25_row("concept/topic.md"),
            _bm25_row("guides/how-to-x.md"),
            _bm25_row("daily/2026-04-15.md"),
            _bm25_row("notes/general.md"),
        ]
        vec = [_vec_row("notes/general.md")]

        cfg = RetrievalConfig.minimal()
        boosts = [
            EntityBoost(graph=graph),
            ProceduralBoost(),
            TemporalDateBoost(config=TemporalBoostConfig(date_path_boost_enabled=True)),
            ChunkDateBoost(config=TemporalBoostConfig(chunk_date_boost_enabled=True)),
        ]

        pipe = _build_pipeline(
            intent=QueryIntent.TEMPORAL,
            boosts=boosts,
            bm25_rows=bm25,
            vec_rows=vec,
            graph=graph,
            config=cfg,
        )
        result = pipe.search("2026-04-15 topic")

        assert not result.error
        assert len(result.results) == 4

    def test_pipeline_logger_records_search_event(self) -> None:
        """Pipeline writes a search event to the injected SearchLogger when
        boosts run successfully."""
        bm25 = [_bm25_row("a.md")]
        vec: list = []
        logger = FakeSearchLogger()

        cfg = RetrievalConfig.minimal()
        embedding = FakeEmbeddingService()
        vector_repo = FakeVectorRepository(results=vec)
        doc_repo = FakeDocumentRepository(bm25_rows=bm25)

        pipe = SearchPipeline(
            classifier=FakeClassifier(intent=QueryIntent.SEMANTIC),
            bm25=BM25SearchBackend(doc_repo),
            vector=VectorSearchBackend(embedding, vector_repo),
            graph=FakeGraphRepository(available=False),
            fusion=RRFFusion(k=cfg.rrf_k),
            boosts=[ProceduralBoost()],
            logger=logger,
            config=cfg,
        )
        pipe.search("anything")

        assert len(logger.events) == 1, f"expected one search event after pipeline run, got {len(logger.events)}"


# ---------------------------------------------------------------------------
# Cross-cutting: never raises through the pipeline boost chain
# ---------------------------------------------------------------------------


class TestNeverRaisesThroughPipeline:
    def test_garbage_query_does_not_raise(self) -> None:
        cfg = TemporalBoostConfig(date_path_boost_enabled=True)
        bm25 = [_bm25_row("daily/2026-01-01.md")]
        vec: list = []

        pipe = _build_pipeline(
            intent=QueryIntent.TEMPORAL,
            boosts=[TemporalDateBoost(config=cfg)],
            bm25_rows=bm25,
            vec_rows=vec,
        )
        result = pipe.search("!@#$%^&*()")
        assert isinstance(result.results, list)

    def test_chunk_date_boost_with_invalid_chunk_date_does_not_raise(self) -> None:
        """A FusedResult whose chunk_date is malformed is silently skipped."""
        # We can't directly inject chunk_date through bm25_row -> rrf path,
        # but the protocol-level safety is exercised by the contract tests.
        # Here we just confirm the pipeline composes when a ChunkDateBoost
        # is in the chain even when no chunk_date is anywhere in the data.
        cfg = TemporalBoostConfig(chunk_date_boost_enabled=True)
        bm25 = [_bm25_row("a.md")]
        vec: list = []

        pipe = _build_pipeline(
            intent=QueryIntent.TEMPORAL,
            boosts=[ChunkDateBoost(config=cfg)],
            bm25_rows=bm25,
            vec_rows=vec,
        )
        # Should not raise even though no query_date populated and no chunk_date
        result = pipe.search("recent thing")
        assert not result.error
