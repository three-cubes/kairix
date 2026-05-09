"""Step definitions for search_chunk_date_recency.feature.

Two scenarios:

1. End-to-end: a TEMPORAL query with a date in the query lifts the doc whose
   chunk_date metadata matches that date. Drives the SearchPipeline directly
   with the same boost-chain composition factory.build_search_pipeline()
   produces in production. Requires (a) factory wires the temporal boosts AND
   (b) pipeline extracts query_date into the boost context.

2. Contract: factory.build_search_pipeline() includes TemporalDateBoost +
   ChunkDateBoost in the boost chain when their respective config flags are
   enabled. Documents the wiring contract for #157.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.boosts import (
    ChunkDateBoost,
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
from kairix.core.search.pipeline import SearchPipeline, SearchResult
from tests.fakes import (
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
    RealClassifierAdapter,
)


@dataclass
class _RecencyCtx:
    pipeline: SearchPipeline | None = None
    classifier: RealClassifierAdapter | None = None
    config: RetrievalConfig | None = None
    last_result: SearchResult | None = None
    docs: list[dict[str, Any]] = field(default_factory=list)
    factory_built_boosts: list[Any] | None = None


@pytest.fixture
def recency_ctx() -> _RecencyCtx:
    return _RecencyCtx()


def _build_factory_style_boosts(cfg: RetrievalConfig, graph: FakeGraphRepository) -> list[Any]:
    """Mirror factory.build_search_pipeline()'s boost-registration logic.

    Production wiring: every enabled boost in cfg.entity / cfg.procedural /
    cfg.temporal contributes a strategy adapter to the chain.
    """
    boosts: list[Any] = []
    if cfg.entity.enabled:
        boosts.append(EntityBoost(graph=graph, config=cfg.entity))
    if cfg.procedural.enabled:
        boosts.append(ProceduralBoost(config=cfg.procedural))
    if cfg.temporal.date_path_boost_enabled:
        boosts.append(TemporalDateBoost(config=cfg.temporal))
    if cfg.temporal.chunk_date_boost_enabled:
        boosts.append(ChunkDateBoost(config=cfg.temporal))
    return boosts


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("a search pipeline wired the way factory.build_search_pipeline wires it")
def _build_factory_style_pipeline(recency_ctx: _RecencyCtx) -> None:
    recency_ctx.classifier = RealClassifierAdapter()
    # Empty pipeline first; documents added in the next step.


@given("the chunk_date boost is enabled in the production config")
def _enable_chunk_date_boost(recency_ctx: _RecencyCtx) -> None:
    recency_ctx.config = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=False),
        procedural=ProceduralBoostConfig(enabled=False),
        temporal=TemporalBoostConfig(
            date_path_boost_enabled=False,
            chunk_date_boost_enabled=True,
        ),
    )


# ---------------------------------------------------------------------------
# Scenario 1: end-to-end recency reorder
# ---------------------------------------------------------------------------


@given(parsers.parse("documents in the index:"))
def _populate_recency_index(recency_ctx: _RecencyCtx, datatable: list[list[str]]) -> None:
    headers = datatable[0]
    rows = datatable[1:]
    docs: list[dict[str, Any]] = []
    for row in rows:
        doc = dict(zip(headers, row, strict=True))
        # Substitute the table fields into BM25Result-shape so substring match
        # in FakeDocumentRepository finds them.
        doc["title"] = doc.get("title", doc["path"])
        doc["content"] = doc.pop("snippet", doc.get("content", ""))
        doc["collection"] = "vault"
        docs.append(doc)
    recency_ctx.docs[:] = docs

    assert recency_ctx.config is not None, "background must set the config first"
    cfg = recency_ctx.config

    graph = FakeGraphRepository(available=False)
    boosts = _build_factory_style_boosts(cfg, graph)
    recency_ctx.pipeline = SearchPipeline(
        classifier=recency_ctx.classifier,
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
        vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
        graph=graph,
        fusion=RRFFusion(k=60),
        boosts=boosts,
        logger=FakeSearchLogger(),
        config=cfg,
    )


@when(parsers.parse('the operator searches "{query}"'))
def _operator_searches(recency_ctx: _RecencyCtx, query: str) -> None:
    assert recency_ctx.pipeline is not None
    recency_ctx.last_result = recency_ctx.pipeline.search(query)


@then(parsers.parse('the classified intent is "{intent}"'))
def _assert_classified_intent_recency(recency_ctx: _RecencyCtx, intent: str) -> None:
    assert recency_ctx.last_result is not None
    assert recency_ctx.last_result.intent.value == intent, (
        f"expected intent {intent!r}, got {recency_ctx.last_result.intent.value!r}"
    )


@then(parsers.parse('the recency-matched doc "{path}" is the top result'))
def _assert_recency_matched_top(recency_ctx: _RecencyCtx, path: str) -> None:
    assert recency_ctx.last_result is not None
    results = recency_ctx.last_result.results
    assert results, (
        f"pipeline returned 0 results for the recency query "
        f"(intent={recency_ctx.last_result.intent}, error={recency_ctx.last_result.error!r})"
    )
    top = results[0].result.path
    assert top == path, (
        f"expected the recency-matched doc {path!r} on top; got {top!r}. "
        f"Full ranking: {[r.result.path for r in results]}"
    )


@then(parsers.parse('the older doc "{path}" ranks below it'))
def _assert_older_below(recency_ctx: _RecencyCtx, path: str) -> None:
    assert recency_ctx.last_result is not None
    paths = [r.result.path for r in recency_ctx.last_result.results]
    assert path in paths, f"expected {path!r} in results; got {paths}"
    assert paths[0] != path, f"the older doc {path!r} ranked first — recency boost did not fire"


# ---------------------------------------------------------------------------
# Scenario 2: factory wiring contract for #157
# ---------------------------------------------------------------------------


@when("the production factory builds a pipeline with temporal boosts enabled")
def _factory_builds_with_temporal(recency_ctx: _RecencyCtx) -> None:
    """Inspect what kairix.core.factory.build_search_pipeline produces."""
    cfg = RetrievalConfig(
        fusion_strategy="rrf",
        entity=EntityBoostConfig(enabled=False),
        procedural=ProceduralBoostConfig(enabled=False),
        temporal=TemporalBoostConfig(
            date_path_boost_enabled=True,
            chunk_date_boost_enabled=True,
        ),
    )

    # We cannot drive the real factory.build_search_pipeline here — it opens
    # Azure / Neo4j / SQLite. Instead, exercise its boost-chain
    # registration logic by dispatching it via a focused helper so
    # the contract assertion runs against the same wiring rules.
    from kairix.core.factory import select_boosts

    graph = FakeGraphRepository(available=False)
    recency_ctx.factory_built_boosts = select_boosts(cfg, graph)


@then("the resulting boost chain includes TemporalDateBoost")
def _assert_chain_has_temporal_date_boost(recency_ctx: _RecencyCtx) -> None:
    assert recency_ctx.factory_built_boosts is not None
    assert any(isinstance(b, TemporalDateBoost) for b in recency_ctx.factory_built_boosts), (
        f"factory boost chain missing TemporalDateBoost — operator's date_path_boost_enabled config is dead. "
        f"Got: {[type(b).__name__ for b in recency_ctx.factory_built_boosts]}"
    )


@then("the resulting boost chain includes ChunkDateBoost")
def _assert_chain_has_chunk_date_boost(recency_ctx: _RecencyCtx) -> None:
    assert recency_ctx.factory_built_boosts is not None
    assert any(isinstance(b, ChunkDateBoost) for b in recency_ctx.factory_built_boosts), (
        f"factory boost chain missing ChunkDateBoost — operator's chunk_date_boost_enabled config is dead. "
        f"Got: {[type(b).__name__ for b in recency_ctx.factory_built_boosts]}"
    )
