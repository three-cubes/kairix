"""Step definitions for search_intent_gated_boosts.feature.

Constructs a SearchPipeline using ONLY canonical fakes (Protocol-compliant)
and the production boost chain. The boost chain is constructed the way
``kairix.core.factory.build_search_pipeline`` builds it — via the
production EntityBoost / ProceduralBoost / TemporalDateBoost adapters
exactly as production wires them. No IntentGatedBoost wrappers, no test
wrappers — if production fires the wrong boost on the wrong intent, the
scenario fails.

F46-clean: composed via ``kairix.core.factory.build_search_pipeline`` with
``paths=FakePaths(...)`` and ``deps=FactoryDeps(...)`` overrides — the
production boost-chain adapters are threaded via ``boosts_override``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.factory import QUERY_CACHE_DISABLED, RERANK_DISABLED, FactoryDeps, build_search_pipeline
from kairix.core.search.boosts import EntityBoost, ProceduralBoost, TemporalDateBoost
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.pipeline import SearchPipeline, SearchResult
from tests.fakes import (
    FakeCollectionResolver,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakePaths,
    FakeSearchLogger,
    FakeVectorRepository,
    RealClassifierAdapter,
)


@dataclass
class _IntentBoostCtx:
    pipeline: SearchPipeline | None = None
    classifier: RealClassifierAdapter | None = None
    last_result: SearchResult | None = None
    docs: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def intent_boost_ctx() -> _IntentBoostCtx:
    return _IntentBoostCtx()


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


def _build_intent_gated_pipeline(
    classifier: RealClassifierAdapter,
    docs: list[dict[str, Any]],
    graph: FakeGraphRepository,
) -> SearchPipeline:
    """Build the intent-gated pipeline through the production factory.

    The boost chain mirrors what kairix.core.factory.build_search_pipeline
    produces today: each strategy adapter from kairix.core.search.boosts.
    No intent-gating wrappers — we want to observe whether each adapter's
    internal "intent guard" actually fires.
    """
    return build_search_pipeline(
        config=RetrievalConfig.defaults(),
        paths=FakePaths(),
        deps=FactoryDeps(
            classifier_override=classifier,
            doc_repo_override=FakeDocumentRepository(documents=docs),
            embed_service_override=FakeEmbeddingService(),
            vec_repo_override=FakeVectorRepository(),
            graph_override=graph,
            fusion_override=RRFFusion(k=60),
            boosts_override=[EntityBoost(graph=graph), ProceduralBoost(), TemporalDateBoost()],
            logger_override=FakeSearchLogger(),
            resolver_override=FakeCollectionResolver(),
            query_cache_override=QUERY_CACHE_DISABLED,
            # No assertion reads the reranked order: wire rerank off so the
            # first search never imports torch / downloads the cross-encoder
            # model from the Hugging Face hub (#493 flake root cause).
            reranker_override=RERANK_DISABLED,
        ),
    )


@given("a search pipeline with the production boost chain wired by factory.build_search_pipeline")
def _build_pipeline_with_production_boost_chain(intent_boost_ctx: _IntentBoostCtx) -> None:
    intent_boost_ctx.classifier = RealClassifierAdapter()
    intent_boost_ctx.pipeline = _build_intent_gated_pipeline(
        classifier=intent_boost_ctx.classifier,
        docs=intent_boost_ctx.docs,
        graph=FakeGraphRepository(available=False),
    )


@given(parsers.parse("a vault with these documents:"))
def _populate_vault_table(intent_boost_ctx: _IntentBoostCtx, datatable: list[list[str]]) -> None:
    headers = datatable[0]
    rows = datatable[1:]
    docs: list[dict[str, Any]] = []
    for row in rows:
        doc = dict(zip(headers, row, strict=True))
        doc["collection"] = "vault"
        docs.append(doc)
    intent_boost_ctx.docs[:] = docs
    # Re-construct the pipeline with the populated repo.
    assert intent_boost_ctx.classifier is not None, "Background must run before this step"
    intent_boost_ctx.pipeline = _build_intent_gated_pipeline(
        classifier=intent_boost_ctx.classifier,
        docs=docs,
        graph=FakeGraphRepository(available=False),
    )


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse('the intent-gated pipeline searches "{query}"'))
def _execute_search(intent_boost_ctx: _IntentBoostCtx, query: str) -> None:
    assert intent_boost_ctx.pipeline is not None
    intent_boost_ctx.last_result = intent_boost_ctx.pipeline.search(query)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse('the gated pipeline classifies the intent as "{intent}"'))
def _assert_intent(intent_boost_ctx: _IntentBoostCtx, intent: str) -> None:
    assert intent_boost_ctx.last_result is not None
    assert intent_boost_ctx.last_result.intent.value == intent, (
        f"expected intent {intent!r}, got {intent_boost_ctx.last_result.intent.value!r}"
    )


@then(parsers.parse('the gated pipeline top result is "{path}"'))
def _assert_top_result(intent_boost_ctx: _IntentBoostCtx, path: str) -> None:
    assert intent_boost_ctx.last_result is not None
    results = intent_boost_ctx.last_result.results
    assert results, (
        f"expected at least one result for query — pipeline returned 0 "
        f"(intent={intent_boost_ctx.last_result.intent}, error={intent_boost_ctx.last_result.error!r})"
    )
    top_path = results[0].result.path
    assert top_path == path, (
        f"expected top result {path!r}; got {top_path!r}. Full ranking: {[r.result.path for r in results]}"
    )


@then(parsers.parse('the runbook "{path}" does not appear in the top {k:d}'))
def _assert_runbook_not_in_top_k(intent_boost_ctx: _IntentBoostCtx, path: str, k: int) -> None:
    assert intent_boost_ctx.last_result is not None
    top_k_paths = [r.result.path for r in intent_boost_ctx.last_result.results[:k]]
    assert path not in top_k_paths, (
        f"runbook {path!r} should NOT appear in top {k} for a SEMANTIC query — "
        f"the procedural boost lifted it. Got top-{k}: {top_k_paths}"
    )


@then(parsers.parse("the dated incident log does not appear in the top {k:d}"))
def _assert_dated_log_not_in_top_k(intent_boost_ctx: _IntentBoostCtx, k: int) -> None:
    assert intent_boost_ctx.last_result is not None
    top_k_paths = [r.result.path for r in intent_boost_ctx.last_result.results[:k]]
    dated = [p for p in top_k_paths if "2026-04-15" in p]
    assert not dated, (
        f"dated log appeared in top {k} for a non-TEMPORAL query — "
        f"the temporal boost lifted it. Got top-{k}: {top_k_paths}"
    )
