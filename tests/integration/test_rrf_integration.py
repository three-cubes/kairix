"""
Integration tests for kairix.core.search.rrf — RRF fusion + boost chain
composition wired through a real SearchPipeline against canonical fakes.

These tests exercise the public surfaces of:
  - rrf() and bm25_primary_fuse() via RRFFusion / BM25PrimaryFusion strategies
  - entity_boost_neo4j() via EntityBoost strategy
  - procedural_boost() via ProceduralBoost strategy
  - temporal_date_boost() via TemporalDateBoost strategy
  - chunk_date_boost() applied directly against pipeline output

The pipeline is constructed exclusively from canonical fakes in tests/fakes.py
(no monkey-patching, no @patch, no inline _Stub/_Fake/_Mock classes).

The end-to-end ordering effects asserted here cover:
  - RRF formula: doc in both lists outranks single-list docs.
  - BM25-primary fusion: BM25 order preserved, vector-only appended below.
  - Entity boost: Neo4j-mentioned doc moves above non-mentioned doc.
  - Procedural boost: runbook path moves above non-runbook on PROCEDURAL intent.
  - Temporal date boost: dated path moves above sibling on TEMPORAL intent.
  - Chunk date boost: recent chunk_date moves above older chunk_date.
  - Composition: boost chain applies in registration order, multiplicative.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.boosts import EntityBoost, ProceduralBoost, TemporalDateBoost
from kairix.core.search.budget import BudgetedResult
from kairix.core.search.config import (
    EntityBoostConfig,
    ProceduralBoostConfig,
    RetrievalConfig,
    TemporalBoostConfig,
)
from kairix.core.search.fusion import BM25PrimaryFusion, RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchPipeline, SearchResult
from kairix.core.search.rrf import RRF_K, FusedResult, chunk_date_boost
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakeVectorRepository,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test data factories
# ---------------------------------------------------------------------------


def _bm25_doc(
    path: str,
    title: str = "T",
    snippet: str = "snippet text",
    collection: str = "vault",
) -> dict[str, Any]:
    """Build a doc that satisfies both DocumentRepository indexing and BM25Result shape.

    FakeDocumentRepository keys docs by ``path`` and returns them verbatim from
    search_fts. RRF reads ``file`` from BM25 dicts, so we set both.
    """
    return {
        "path": path,
        "file": path,
        "title": title,
        "snippet": snippet,
        "content": snippet,
        "collection": collection,
        "score": 1.0,
    }


def _vec_doc(
    path: str,
    distance: float = 0.1,
    title: str = "T",
    snippet: str = "snippet text",
    collection: str = "vault",
) -> dict[str, Any]:
    """Build a vector result dict shaped like VecResult."""
    return {
        "path": path,
        "hash_seq": f"h_{path}",
        "distance": distance,
        "title": title,
        "snippet": snippet,
        "collection": collection,
    }


def _build_pipeline(
    *,
    bm25_docs: list[dict[str, Any]],
    vec_results: list[dict[str, Any]],
    intent: QueryIntent = QueryIntent.SEMANTIC,
    fusion: Any = None,
    boosts: list[Any] | None = None,
    graph: FakeGraphRepository | None = None,
    config: RetrievalConfig | None = None,
) -> SearchPipeline:
    """Compose a SearchPipeline with canonical fakes and explicit collaborators."""
    cfg = config if config is not None else RetrievalConfig.minimal()
    return SearchPipeline(
        classifier=FakeClassifier(intent=intent),
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=bm25_docs)),
        vector=VectorSearchBackend(
            FakeEmbeddingService(),
            FakeVectorRepository(results=vec_results),
        ),
        graph=graph if graph is not None else FakeGraphRepository(available=False),
        fusion=fusion if fusion is not None else RRFFusion(k=cfg.rrf_k),
        boosts=boosts or [],
        logger=None,
        config=cfg,
    )


def _paths_in_order(result: SearchResult) -> list[str]:
    """Extract result paths in pipeline output order."""
    out: list[str] = []
    for r in result.results:
        if isinstance(r, BudgetedResult):
            out.append(r.result.path)
        elif isinstance(r, FusedResult):
            out.append(r.path)
        else:
            out.append(getattr(r, "path", ""))
    return out


def _fused_in_order(result: SearchResult) -> list[FusedResult]:
    """Extract FusedResult objects (unwrap BudgetedResult)."""
    out: list[FusedResult] = []
    for r in result.results:
        if isinstance(r, BudgetedResult):
            out.append(r.result)
        elif isinstance(r, FusedResult):
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# RRF formula end-to-end
# ---------------------------------------------------------------------------


def test_rrf_doc_in_both_lists_outranks_single_list_docs() -> None:
    """A doc appearing in both BM25 and vector lists ranks above docs in one list.

    RRF score for "shared" (rank 1 BM25 + rank 1 vec) = 2/(k+1).
    RRF score for "bm25only" (rank 2 BM25) = 1/(k+2).
    RRF score for "veconly" (rank 2 vec) = 1/(k+2).
    """
    # Order matters: BM25 returns docs in insertion order matching query.
    # We construct BM25 list as [shared, bm25only], vec list as [shared, veconly].
    bm25_docs = [
        _bm25_doc("notes/shared.md", snippet="alpha shared content"),
        _bm25_doc("notes/bm25only.md", snippet="alpha keyword content"),
    ]
    vec_results = [
        _vec_doc("notes/shared.md", distance=0.05),
        _vec_doc("notes/veconly.md", distance=0.10),
    ]

    pipeline = _build_pipeline(bm25_docs=bm25_docs, vec_results=vec_results)
    result = pipeline.search("alpha")

    paths = _paths_in_order(result)
    fused = _fused_in_order(result)
    by_path = {f.path: f for f in fused}

    # All three docs surface
    assert set(paths) == {"notes/shared.md", "notes/bm25only.md", "notes/veconly.md"}
    # Shared doc must be first (higher RRF score)
    assert paths[0] == "notes/shared.md"

    # Verify exact RRF formula values
    expected_shared = 2.0 / (RRF_K + 1)
    expected_bm25only = 1.0 / (RRF_K + 2)
    expected_veconly = 1.0 / (RRF_K + 2)
    assert by_path["notes/shared.md"].rrf_score == pytest.approx(expected_shared, rel=1e-9)
    assert by_path["notes/bm25only.md"].rrf_score == pytest.approx(expected_bm25only, rel=1e-9)
    assert by_path["notes/veconly.md"].rrf_score == pytest.approx(expected_veconly, rel=1e-9)
    # And the formula implies shared > singles
    assert by_path["notes/shared.md"].rrf_score > by_path["notes/bm25only.md"].rrf_score
    assert by_path["notes/shared.md"].rrf_score > by_path["notes/veconly.md"].rrf_score


def test_rrf_in_pipeline_initialises_boosted_score_to_rrf_score() -> None:
    """When no boosts run, boosted_score equals rrf_score for every result."""
    bm25_docs = [_bm25_doc("notes/a.md", snippet="alpha")]
    vec_results = [_vec_doc("notes/b.md")]

    pipeline = _build_pipeline(bm25_docs=bm25_docs, vec_results=vec_results)
    result = pipeline.search("alpha")
    fused = _fused_in_order(result)

    assert len(fused) == 2
    for fr in fused:
        assert fr.boosted_score == pytest.approx(fr.rrf_score, rel=1e-12)


# ---------------------------------------------------------------------------
# BM25-primary fusion end-to-end
# ---------------------------------------------------------------------------


def test_bm25_primary_fusion_preserves_bm25_order_and_appends_vec_only() -> None:
    """BM25PrimaryFusion: BM25 results in BM25 order, vector-only appended below."""
    bm25_docs = [
        _bm25_doc("notes/b1.md", snippet="alpha first"),
        _bm25_doc("notes/b2.md", snippet="alpha second"),
    ]
    vec_results = [
        _vec_doc("notes/b2.md", distance=0.01),  # also in BM25 — should not be re-appended
        _vec_doc("notes/v1.md", distance=0.02),  # vector-only, appended at bottom
    ]

    pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=vec_results,
        fusion=BM25PrimaryFusion(),
    )
    result = pipeline.search("alpha")
    paths = _paths_in_order(result)

    assert paths == ["notes/b1.md", "notes/b2.md", "notes/v1.md"]


def test_bm25_primary_fusion_marks_dual_membership() -> None:
    """A doc in both BM25 and vec keeps BM25 rank but has in_vec=True."""
    bm25_docs = [_bm25_doc("notes/dual.md", snippet="alpha dual")]
    vec_results = [_vec_doc("notes/dual.md", distance=0.01)]

    pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=vec_results,
        fusion=BM25PrimaryFusion(),
    )
    result = pipeline.search("alpha")
    fused = _fused_in_order(result)

    assert len(fused) == 1
    assert fused[0].in_bm25 is True
    assert fused[0].in_vec is True
    assert fused[0].bm25_rank == 1
    assert fused[0].vec_rank == 1


# ---------------------------------------------------------------------------
# Entity boost composition with RRF
# ---------------------------------------------------------------------------


def _entity_row(vault_path: str, in_degree: int, name: str = "", labels: list[str] | None = None) -> dict[str, Any]:
    """Build an entity row in the shape expected by entity_boost_neo4j Cypher."""
    return {
        "vault_path": vault_path,
        "name": name or vault_path.rsplit("/", 1)[-1].replace(".md", ""),
        "labels": labels if labels is not None else ["Concept"],
        "in_degree": in_degree,
    }


def test_entity_boost_after_rrf_promotes_high_in_degree_doc() -> None:
    """A doc with high entity in-degree moves above an equally-ranked doc with none."""
    # Both docs appear ONLY in BM25, at ranks 1 and 2 respectively.
    # Without boost, plain.md (rank 1) would lead. With entity boost on entity.md
    # (high in-degree), entity.md should overtake.
    bm25_docs = [
        _bm25_doc("notes/plain.md", snippet="alpha plain"),
        _bm25_doc("concept/entity.md", snippet="alpha entity"),
    ]
    vec_results: list[dict[str, Any]] = []

    # Calibrate: rrf_score for plain (rank 1) = 1/61, entity (rank 2) = 1/62.
    # Boost factor = 1 + min(0.20 * log(1 + 1.0 * 10), cap-1) = 1 + 0.20*log(11)
    # = 1 + 0.20 * 2.3979 = 1.4796.
    # entity.boosted = (1/62) * 1.4796 ≈ 0.02386.
    # plain.boosted = 1/61 ≈ 0.01639.
    # entity > plain — exactly what we want.
    graph = FakeGraphRepository(
        entities=[_entity_row("concept/entity.md", in_degree=10, labels=["Concept"])],
        available=True,
    )
    config = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=True, factor=0.20, cap=2.0),
        procedural=ProceduralBoostConfig(enabled=False),
        temporal=TemporalBoostConfig(),
    )
    pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=vec_results,
        boosts=[EntityBoost(graph=graph, config=config.entity)],
        graph=graph,
        config=config,
    )
    # Sanity: without boost, plain leads.
    no_boost_pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=vec_results,
        graph=graph,
        config=config,
    )
    no_boost_paths = _paths_in_order(no_boost_pipeline.search("alpha"))
    assert no_boost_paths[0] == "notes/plain.md"  # baseline confirms ordering before boost

    boosted_paths = _paths_in_order(pipeline.search("alpha"))
    assert boosted_paths[0] == "concept/entity.md"
    assert boosted_paths[1] == "notes/plain.md"


def test_entity_boost_no_effect_when_graph_unavailable() -> None:
    """When graph.available is False, EntityBoost leaves RRF order intact."""
    bm25_docs = [
        _bm25_doc("notes/first.md", snippet="alpha"),
        _bm25_doc("concept/entity.md", snippet="alpha"),
    ]
    graph = FakeGraphRepository(
        entities=[_entity_row("concept/entity.md", in_degree=100)],
        available=False,
    )
    config = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=True, factor=0.20, cap=2.0),
        procedural=ProceduralBoostConfig(enabled=False),
        temporal=TemporalBoostConfig(),
    )
    pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=[],
        boosts=[EntityBoost(graph=graph, config=config.entity)],
        graph=graph,
        config=config,
    )
    paths = _paths_in_order(pipeline.search("alpha"))
    # RRF rank-1 doc stays first because boost short-circuits when graph unavailable
    assert paths[0] == "notes/first.md"
    assert paths[1] == "concept/entity.md"


# ---------------------------------------------------------------------------
# Procedural boost composition with RRF
# ---------------------------------------------------------------------------


def test_procedural_boost_promotes_runbook_path_for_procedural_intent() -> None:
    """A runbook path moves above a non-procedural path under PROCEDURAL intent."""
    # Both docs in BM25 only. Runbook is rank 2 (lower RRF). With 1.4x boost
    # it should overtake the rank-1 non-procedural doc.
    # rank 1 = 1/61 ≈ 0.01639
    # rank 2 = 1/62 ≈ 0.01613; * 1.4 = 0.02258 — overtakes rank 1.
    bm25_docs = [
        _bm25_doc("notes/general.md", snippet="alpha general"),
        _bm25_doc("runbooks/how-to-deploy.md", snippet="alpha deploy"),
    ]
    config = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=False),
        procedural=ProceduralBoostConfig(enabled=True, factor=1.4),
        temporal=TemporalBoostConfig(),
    )
    pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=[],
        intent=QueryIntent.PROCEDURAL,
        boosts=[ProceduralBoost(config=config.procedural)],
        config=config,
    )
    paths = _paths_in_order(pipeline.search("alpha"))
    assert paths[0] == "runbooks/how-to-deploy.md"
    assert paths[1] == "notes/general.md"


def test_procedural_boost_does_not_match_unrelated_paths() -> None:
    """Paths not matching procedural patterns get no boost — RRF order preserved."""
    bm25_docs = [
        _bm25_doc("notes/first.md", snippet="alpha"),
        _bm25_doc("notes/second.md", snippet="alpha"),
    ]
    config = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=False),
        procedural=ProceduralBoostConfig(enabled=True, factor=1.4),
        temporal=TemporalBoostConfig(),
    )
    pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=[],
        intent=QueryIntent.PROCEDURAL,
        boosts=[ProceduralBoost(config=config.procedural)],
        config=config,
    )
    paths = _paths_in_order(pipeline.search("alpha"))
    assert paths == ["notes/first.md", "notes/second.md"]


# ---------------------------------------------------------------------------
# Temporal date boost composition with RRF
# ---------------------------------------------------------------------------


def test_temporal_date_boost_promotes_date_matched_path() -> None:
    """A path containing the queried date moves above a sibling without it."""
    # rank 1 = sibling (no date), rank 2 = dated.
    # Without boost: sibling first. With 1.35x: dated overtakes.
    # Snippet contains the query terms ("event 2026-04-17") so the FakeDocumentRepository
    # FTS matcher returns both docs; the temporal boost only re-ranks them.
    bm25_docs = [
        _bm25_doc("daily/sibling.md", snippet="event 2026-04-17 happened here"),
        _bm25_doc("daily/2026-04-17.md", snippet="event 2026-04-17 happened here"),
    ]
    config = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=False),
        procedural=ProceduralBoostConfig(enabled=False),
        temporal=TemporalBoostConfig(date_path_boost_enabled=True, date_path_boost_factor=1.35),
    )
    pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=[],
        intent=QueryIntent.TEMPORAL,
        boosts=[TemporalDateBoost(config=config.temporal)],
        config=config,
    )
    paths = _paths_in_order(pipeline.search("event 2026-04-17"))
    assert paths[0] == "daily/2026-04-17.md"
    assert paths[1] == "daily/sibling.md"


def test_temporal_date_boost_disabled_leaves_order_intact() -> None:
    """When date_path_boost_enabled=False, ordering matches plain RRF."""
    bm25_docs = [
        _bm25_doc("daily/sibling.md", snippet="event 2026-04-17 happened here"),
        _bm25_doc("daily/2026-04-17.md", snippet="event 2026-04-17 happened here"),
    ]
    config = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=False),
        procedural=ProceduralBoostConfig(enabled=False),
        temporal=TemporalBoostConfig(date_path_boost_enabled=False),
    )
    pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=[],
        intent=QueryIntent.TEMPORAL,
        boosts=[TemporalDateBoost(config=config.temporal)],
        config=config,
    )
    paths = _paths_in_order(pipeline.search("event 2026-04-17"))
    # Boost is gated off — rank-1 RRF doc (sibling) leads.
    assert paths == ["daily/sibling.md", "daily/2026-04-17.md"]


# ---------------------------------------------------------------------------
# Chunk date boost — applied to pipeline output to exercise public surface
# ---------------------------------------------------------------------------


def test_chunk_date_boost_reorders_pipeline_output_by_recency() -> None:
    """chunk_date_boost on real pipeline output promotes the recent doc.

    The pipeline doesn't natively wire query_date into context, so we apply
    chunk_date_boost() against the pipeline's FusedResult output directly —
    matching the way hybrid.py composes the boost (see _apply_chunk_date_boost).
    """
    bm25_docs = [
        _bm25_doc("notes/old.md", snippet="alpha activity"),
        _bm25_doc("notes/recent.md", snippet="alpha activity"),
    ]
    config = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=False),
        procedural=ProceduralBoostConfig(enabled=False),
        temporal=TemporalBoostConfig(),
    )
    pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=[],
        intent=QueryIntent.TEMPORAL,
        boosts=[],
        config=config,
    )
    result = pipeline.search("alpha activity")
    fused = _fused_in_order(result)

    # Sanity: baseline ordering = RRF order (old first, recent second).
    assert [f.path for f in fused] == ["notes/old.md", "notes/recent.md"]

    # Stamp chunk_date metadata on the FusedResult objects (TMP-7B wires this
    # at index time; here we set it directly to exercise the boost surface).
    for fr in fused:
        if fr.path == "notes/old.md":
            fr.chunk_date = "2024-01-01"
        elif fr.path == "notes/recent.md":
            fr.chunk_date = "2026-04-15"

    cfg = TemporalBoostConfig(chunk_date_boost_enabled=True, chunk_date_decay_halflife_days=30)
    query_date = datetime.date(2026, 4, 17)
    boosted = chunk_date_boost(fused, query_date, config=cfg)

    assert [f.path for f in boosted] == ["notes/recent.md", "notes/old.md"]


def test_chunk_date_boost_no_op_when_disabled() -> None:
    """chunk_date_boost with disabled config returns input unchanged."""
    bm25_docs = [_bm25_doc("notes/recent.md", snippet="alpha")]
    pipeline = _build_pipeline(bm25_docs=bm25_docs, vec_results=[])
    result = pipeline.search("alpha")
    fused = _fused_in_order(result)
    fused[0].chunk_date = "2026-04-15"

    disabled = TemporalBoostConfig(chunk_date_boost_enabled=False)
    out = chunk_date_boost(fused, datetime.date(2026, 4, 17), config=disabled)
    assert len(out) == 1
    assert out[0].boosted_score == pytest.approx(fused[0].rrf_score, rel=1e-12)


# ---------------------------------------------------------------------------
# Boost chain composition (entity + procedural)
# ---------------------------------------------------------------------------


def test_entity_then_procedural_chain_applies_multiplicatively() -> None:
    """Boost chain is multiplicative: entity boost stacks on top of procedural boost.

    Setup: doc A is rank 1 in RRF (1/61). Doc B is rank 2 (1/62), is procedural
    (1.4x), and has entity in-degree (≈1.48x at in_degree=10). Combined factor
    on B ≈ 2.07x → B vastly overtakes A.
    """
    bm25_docs = [
        _bm25_doc("notes/plain.md", snippet="alpha"),
        _bm25_doc("runbooks/how-to-entity.md", snippet="alpha"),
    ]
    graph = FakeGraphRepository(
        entities=[_entity_row("runbooks/how-to-entity.md", in_degree=10)],
        available=True,
    )
    config = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=True, factor=0.20, cap=2.0),
        procedural=ProceduralBoostConfig(enabled=True, factor=1.4),
        temporal=TemporalBoostConfig(),
    )
    pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=[],
        intent=QueryIntent.PROCEDURAL,
        boosts=[
            EntityBoost(graph=graph, config=config.entity),
            ProceduralBoost(config=config.procedural),
        ],
        graph=graph,
        config=config,
    )
    result = pipeline.search("alpha")
    fused = _fused_in_order(result)
    by_path = {f.path: f for f in fused}

    # Baseline RRF score
    rrf_b = 1.0 / (RRF_K + 2)
    boosted_b = by_path["runbooks/how-to-entity.md"].boosted_score
    rrf_a = 1.0 / (RRF_K + 1)
    boosted_a = by_path["notes/plain.md"].boosted_score

    # plain.md got no entity hit and no procedural hit
    assert boosted_a == pytest.approx(rrf_a, rel=1e-9)
    # runbook got entity * procedural — strictly greater than 1.4x alone
    assert boosted_b > rrf_b * 1.4
    # Final ordering: runbook beats plain
    assert _paths_in_order(result)[0] == "runbooks/how-to-entity.md"


def test_boost_chain_runs_in_registration_order() -> None:
    """Each boost sees the output of the previous boost (boosted_score accumulates)."""
    bm25_docs = [
        _bm25_doc("runbooks/how-to-x.md", snippet="alpha"),
    ]
    graph = FakeGraphRepository(
        entities=[_entity_row("runbooks/how-to-x.md", in_degree=10)],
        available=True,
    )
    cfg = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=True, factor=0.20, cap=2.0),
        procedural=ProceduralBoostConfig(enabled=True, factor=1.4),
        temporal=TemporalBoostConfig(),
    )

    # Pipeline 1: entity then procedural
    pipe_ep = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=[],
        intent=QueryIntent.PROCEDURAL,
        boosts=[EntityBoost(graph=graph, config=cfg.entity), ProceduralBoost(config=cfg.procedural)],
        graph=graph,
        config=cfg,
    )
    # Pipeline 2: procedural only (no entity)
    pipe_p = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=[],
        intent=QueryIntent.PROCEDURAL,
        boosts=[ProceduralBoost(config=cfg.procedural)],
        graph=graph,
        config=cfg,
    )
    fused_ep = _fused_in_order(pipe_ep.search("alpha"))
    fused_p = _fused_in_order(pipe_p.search("alpha"))

    # Both pipelines have the runbook doc. The entity+procedural pipeline should
    # produce a strictly higher boosted_score because entity boost > 1.0.
    score_ep = next(f.boosted_score for f in fused_ep if f.path == "runbooks/how-to-x.md")
    score_p = next(f.boosted_score for f in fused_p if f.path == "runbooks/how-to-x.md")
    assert score_ep > score_p


# ---------------------------------------------------------------------------
# bm25_primary_fuse public surface integration
# ---------------------------------------------------------------------------


def test_bm25_primary_fusion_integrates_with_entity_boost() -> None:
    """BM25-primary fusion + entity boost: entity boost still re-orders."""
    # Both BM25 hits at ranks 1 and 2; entity_boost should overtake rank 1.
    # bm25_primary_fuse: rrf_score = 1/rank → rank 1 = 1.0, rank 2 = 0.5.
    # entity.boost = 1 + 0.20*log(11) ≈ 1.48 → entity boosted_score ≈ 0.74 < 1.0.
    # So with default factor entity does NOT overtake. We need higher factor.
    bm25_docs = [
        _bm25_doc("notes/first.md", snippet="alpha"),
        _bm25_doc("concept/entity.md", snippet="alpha"),
    ]
    graph = FakeGraphRepository(
        entities=[_entity_row("concept/entity.md", in_degree=10000, labels=["Concept"])],
        available=True,
    )
    # Use a high enough cap that entity boosting can overtake bm25_primary's
    # 1/rank scoring (which gives rank 1 = 1.0 and rank 2 = 0.5).
    cfg = RetrievalConfig(
        fusion_strategy="bm25_primary",
        entity=EntityBoostConfig(enabled=True, factor=2.0, cap=3.0),
        procedural=ProceduralBoostConfig(enabled=False),
        temporal=TemporalBoostConfig(),
    )
    pipeline = _build_pipeline(
        bm25_docs=bm25_docs,
        vec_results=[],
        intent=QueryIntent.SEMANTIC,
        fusion=BM25PrimaryFusion(),
        boosts=[EntityBoost(graph=graph, config=cfg.entity)],
        graph=graph,
        config=cfg,
    )
    paths = _paths_in_order(pipeline.search("alpha"))
    assert paths[0] == "concept/entity.md"
    assert paths[1] == "notes/first.md"
