"""
Integration tests for kairix.core.search.planner.

Wires ``QueryPlanner`` end-to-end against the canonical fakes from
``tests.fakes`` and asserts the decomposed sub-queries reach a downstream
search function and that the entity-graph context flows from a populated
``FakePlannerGraphClient`` into the prompt seen by ``FakeLLMBackend``.

Goals:

  - Confirm the planner's decompose → retrieve_and_merge boundary actually
    composes: every sub-query the LLM returned must be invoked downstream.
  - Confirm the Neo4j graph layer is genuinely wired into the LLM dispatch
    path: when a populated graph is supplied, the prompt the LLM receives
    contains the entity context block.
  - Confirm RRF dedupes identical results across sub-queries when the
    decompose flow drives the retrieval.

No monkeypatching, no @patch, no inline stubs. Every fake comes from
``tests.fakes`` and satisfies the production Protocol it stands in for
(``LLMBackend`` for the chat layer; the duck-typed Neo4jClient surface
for the graph layer).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from kairix.core.search.planner import QueryPlanner
from tests.fakes import FakeLLMBackend, FakePlannerGraphClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _Result:
    """Minimal search result with .path attribute (the surface RRF needs)."""

    path: str


# ---------------------------------------------------------------------------
# decompose → retrieve_and_merge end-to-end.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_decompose_then_retrieve_invokes_search_for_every_sub_query() -> None:
    """Every sub-query produced by decompose must reach the downstream search.

    Wiring: FakeLLMBackend → planner.decompose → planner.retrieve_and_merge
    → search_fn closure that records every query it saw.
    """
    backend = FakeLLMBackend(chat_response='["alpha facts", "beta facts", "gamma facts"]')
    planner = QueryPlanner()

    seen_queries: list[str] = []
    results_by_query = {
        "alpha facts": [_Result(path="alpha.md")],
        "beta facts": [_Result(path="beta.md")],
        "gamma facts": [_Result(path="gamma.md")],
    }

    def search_fn(q: str) -> list[_Result]:
        seen_queries.append(q)
        return results_by_query.get(q, [])

    sub_queries = planner.decompose("compare alpha beta gamma", llm_backend=backend)
    merged = planner.retrieve_and_merge(sub_queries, search_fn, top_k_per_sub=5, final_top_k=5)

    assert sub_queries == ["alpha facts", "beta facts", "gamma facts"]
    assert sorted(seen_queries) == ["alpha facts", "beta facts", "gamma facts"]
    paths = sorted(r.path for r in merged)
    assert paths == ["alpha.md", "beta.md", "gamma.md"]


@pytest.mark.integration
def test_simple_query_passthrough_only_calls_search_once() -> None:
    """A single-sub-query decompose must result in exactly one downstream call."""
    backend = FakeLLMBackend(chat_response='["just one sub-query"]')
    planner = QueryPlanner()
    seen: list[str] = []

    def search_fn(q: str) -> list[_Result]:
        seen.append(q)
        return [_Result(path="solo.md")]

    subs = planner.decompose("simple query", llm_backend=backend)
    merged = planner.retrieve_and_merge(subs, search_fn)

    assert subs == ["just one sub-query"]
    assert seen == ["just one sub-query"]
    assert [r.path for r in merged] == ["solo.md"]


@pytest.mark.integration
def test_rrf_dedupes_shared_results_across_sub_queries() -> None:
    """When two sub-queries return overlapping docs, RRF must dedupe and rank."""
    backend = FakeLLMBackend(chat_response='["q one", "q two"]')
    planner = QueryPlanner()
    shared = _Result(path="shared.md")
    only_a = _Result(path="only-a.md")
    only_b = _Result(path="only-b.md")

    def search_fn(q: str) -> list[_Result]:
        if q == "q one":
            return [shared, only_a]
        if q == "q two":
            return [shared, only_b]
        return []

    subs = planner.decompose("q whatever", llm_backend=backend)
    merged = planner.retrieve_and_merge(subs, search_fn, top_k_per_sub=5, final_top_k=6)

    paths = [r.path for r in merged]
    assert paths.count("shared.md") == 1
    assert "only-a.md" in paths
    assert "only-b.md" in paths
    # shared appears in both lists at the same rank → RRF gives it the
    # highest score → rank 1.
    assert paths[0] == "shared.md"


# ---------------------------------------------------------------------------
# Neo4j graph context flowing into the LLM prompt end-to-end.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_populated_graph_injects_entity_context_into_llm_prompt() -> None:
    """End-to-end: graph entities must appear in the prompt the LLM receives.

    Wiring: FakePlannerGraphClient (populated) + FakeLLMBackend → decompose.
    Inspect what the LLM saw via FakeLLMBackend.chat_calls.
    """
    backend = FakeLLMBackend(chat_response='["techcorp ai work", "buildco infra work"]')
    graph = FakePlannerGraphClient(
        entities_by_word={
            "techcorp": [{"id": "tc", "name": "TechCorp"}],
            "buildco": [{"id": "bc", "name": "BuildCo"}],
        },
        related_by_id={
            "tc": [{"name": "GlobalTech"}, {"name": "ResearchHub"}],
            "bc": [{"name": "InfraOps"}],
        },
        available=True,
    )
    planner = QueryPlanner()

    planner.decompose(
        "compare TechCorp and BuildCo offerings",
        neo4j_client=graph,
        llm_backend=backend,
    )

    assert backend.chat_calls, "LLM must have been called"
    prompt = backend.chat_calls[0]["messages"][0]["content"]
    # Entity context header
    assert "Known entities related to this query:" in prompt
    # Both entities and at least one related name from each must appear
    assert "TechCorp" in prompt
    assert "BuildCo" in prompt
    assert "GlobalTech" in prompt
    assert "InfraOps" in prompt
    # And the original query must still be in the prompt
    assert "compare TechCorp and BuildCo offerings" in prompt
    # Graph was consulted for both entity words
    assert "TechCorp" in graph.find_calls
    assert "BuildCo" in graph.find_calls


@pytest.mark.integration
def test_unavailable_graph_does_not_consult_neo4j() -> None:
    """available=False must short-circuit before any find_by_name call."""
    backend = FakeLLMBackend(chat_response='["sub one", "sub two"]')
    graph = FakePlannerGraphClient(
        entities_by_word={"techcorp": [{"id": "tc", "name": "TechCorp"}]},
        related_by_id={"tc": [{"name": "GlobalTech"}]},
        available=False,
    )
    planner = QueryPlanner()

    planner.decompose(
        "compare TechCorp and other things",
        neo4j_client=graph,
        llm_backend=backend,
    )

    assert graph.find_calls == [], "available=False must skip neo4j entirely"
    prompt = backend.chat_calls[0]["messages"][0]["content"]
    assert "Known entities related to this query:" not in prompt


@pytest.mark.integration
def test_graph_with_no_matches_falls_back_to_plain_prompt() -> None:
    """available=True but no entity matches → plain prompt, no header."""
    backend = FakeLLMBackend(chat_response='["sub query value"]')
    graph = FakePlannerGraphClient(entities_by_word={}, related_by_id={}, available=True)
    planner = QueryPlanner()

    planner.decompose("query about unknown topic items", neo4j_client=graph, llm_backend=backend)

    prompt = backend.chat_calls[0]["messages"][0]["content"]
    assert "Known entities related to this query:" not in prompt
    assert "query about unknown topic items" in prompt
