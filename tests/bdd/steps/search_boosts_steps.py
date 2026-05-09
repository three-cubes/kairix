"""Step definitions for search_boosts.feature.

Wires a real SearchPipeline using canonical fakes from tests/fakes.py:
  - FakeClassifier: returns the chosen intent
  - FakeDocumentRepository (scripted-mode via bm25_rows kwarg)
  - FakeVectorRepository
  - FakeGraphRepository
  - FakeSearchLogger

No monkeypatching, no @patch.
"""

from __future__ import annotations

from typing import Any

from pytest_bdd import given, parsers, then, when

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.boosts import EntityBoost, ProceduralBoost
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchPipeline
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
)

# Module-level state (test-scoped, simple)
_state: dict[str, Any] = {}


def _bm25_row(path: str) -> dict[str, Any]:
    return {
        "file": path,
        "title": path.rsplit("/", 1)[-1],
        "snippet": path,
        "score": 1.0,
        "collection": "kb",
    }


def _vec_row(path: str) -> dict[str, Any]:
    return {
        "hash_seq": "h_0",
        "distance": 0.1,
        "path": path,
        "collection": "kb",
        "title": path.rsplit("/", 1)[-1],
        "snippet": path,
    }


@given("a kairix search pipeline configured with the boost chain")
def setup_pipeline_state() -> None:
    _state.clear()
    _state["bm25"] = []
    _state["vec"] = []
    _state["entities"] = []


@given(parsers.parse('a how-to document at "{path}"'))
def given_how_to(path: str) -> None:
    _state["bm25"].append(_bm25_row(path))
    _state["vec"].append(_vec_row(path))


@given(parsers.parse('a generic note at "{path}"'))
def given_generic_note(path: str) -> None:
    _state["bm25"].append(_bm25_row(path))
    _state["vec"].append(_vec_row(path))


@given(parsers.re(r'an entity-canonical document at "(?P<path>[^"]+)" with in-degree (?P<in_degree>\d+)'))
def given_entity_canonical(path: str, in_degree: str) -> None:
    _state["bm25"].append(_bm25_row(path))
    _state["vec"].append(_vec_row(path))
    _state["entities"].append(
        {
            "name": path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
            "vault_path": path,
            "labels": ["concept"],
            "in_degree": int(in_degree),
        }
    )


def _build_and_run(intent: QueryIntent, query: str) -> None:
    cfg = RetrievalConfig.minimal()
    graph_available = bool(_state["entities"])
    graph = FakeGraphRepository(entities=_state["entities"], available=graph_available)

    doc_repo = FakeDocumentRepository(bm25_rows=_state["bm25"])
    bm25 = BM25SearchBackend(doc_repo)

    embedding = FakeEmbeddingService()
    vector_repo = FakeVectorRepository(results=_state["vec"])
    vector = VectorSearchBackend(embedding, vector_repo)

    boosts: list[Any] = []
    if intent == QueryIntent.PROCEDURAL:
        boosts.append(ProceduralBoost())
    if intent == QueryIntent.ENTITY:
        boosts.append(EntityBoost(graph=graph))

    pipe = SearchPipeline(
        classifier=FakeClassifier(intent=intent),
        bm25=bm25,
        vector=vector,
        graph=graph,
        fusion=RRFFusion(k=cfg.rrf_k),
        boosts=boosts,
        logger=FakeSearchLogger(),
        config=cfg,
    )
    _state["result"] = pipe.search(query)


@when(parsers.parse('I run a procedural search for "{query}"'))
def run_procedural_search(query: str) -> None:
    _build_and_run(QueryIntent.PROCEDURAL, query)


@when(parsers.parse('I run an entity search for "{query}"'))
def run_entity_search(query: str) -> None:
    _build_and_run(QueryIntent.ENTITY, query)


@when(parsers.parse('I run a semantic search for "{query}"'))
def run_semantic_search(query: str) -> None:
    _build_and_run(QueryIntent.SEMANTIC, query)


@then(parsers.parse('the top result is "{path}"'))
def top_result_is(path: str) -> None:
    result = _state["result"]
    assert result.results, "pipeline returned no results"
    top = result.results[0].result  # BudgetedResult.result -> FusedResult
    assert top.path == path, (
        f"expected top path {path!r}, got {top.path!r} (full ranking: {[r.result.path for r in result.results]})"
    )


@then("no result has been boost-modified")
def no_result_boost_modified() -> None:
    result = _state["result"]
    assert result.results, "pipeline returned no results"
    for br in result.results:
        fused = br.result
        assert fused.boosted_score == fused.rrf_score, (
            f"{fused.path} was boosted: rrf={fused.rrf_score} boosted={fused.boosted_score}"
        )
