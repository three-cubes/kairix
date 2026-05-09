"""
Contract tests for kairix.core.search.planner.

Asserts the documented contracts of the public planner surface:

  - ``decompose_query`` (via ``QueryPlanner.decompose``) returns ``[query]``
    on every documented failure path: empty / unparseable LLM response, JSON
    that is not a list, list out of bounds, LLM raise.
  - ``decompose`` injects neo4j entity context into the prompt when the
    client is wired and reports ``available=True``.
  - ``neo4j_graph_context`` returns ``None`` when the graph is unavailable
    or there are no entities, and a formatted string when entities and
    relationships are present.
  - "Never raises" claim: every helper exception surfaces as the documented
    fallback (``[query]`` for ``decompose``, ``None`` for the context
    builder), so a broken Neo4j or LLM cannot crash the search pipeline.

Drives every assertion through the public planner surface
(``QueryPlanner``, ``neo4j_graph_context``). Uses Protocol-compliant fakes
from ``tests.fakes`` (``FakeLLMBackend`` for
``kairix.platform.llm.protocol.LLMBackend``; ``FakePlannerGraphClient``
for the duck-typed Neo4jClient surface — ``available`` /
``find_by_name`` / ``related_entities``).

No monkeypatching, no inline stubs, no private-fn imports.
"""

from __future__ import annotations

import pytest

from kairix.core.search.planner import QueryPlanner, neo4j_graph_context
from kairix.platform.llm.protocol import LLMBackend
from tests.fakes import FakeLLMBackend, FakePlannerGraphClient

# ---------------------------------------------------------------------------
# Protocol conformance — anchors the fakes to the production protocol.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_fake_llm_backend_satisfies_llm_protocol() -> None:
    """FakeLLMBackend must satisfy kairix.platform.llm.protocol.LLMBackend."""
    assert isinstance(FakeLLMBackend(), LLMBackend)


# ---------------------------------------------------------------------------
# decompose() fallback contracts.
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestDecomposeFallbackContract:
    def test_empty_llm_response_returns_query_singleton(self) -> None:
        """Empty chat response must fall back to ``[query]`` per docstring."""
        backend = FakeLLMBackend(chat_response="")
        planner = QueryPlanner()

        result = planner.decompose("operator query", llm_backend=backend)

        assert result == ["operator query"]
        assert len(backend.chat_calls) == 1, "LLM must still be called once"

    def test_unparseable_response_falls_back_to_query(self) -> None:
        """JSON-parse failures with no extractable quoted strings → ``[query]``."""
        backend = FakeLLMBackend(chat_response="this is prose, not JSON")
        planner = QueryPlanner()

        result = planner.decompose("operator query", llm_backend=backend)

        assert result == ["operator query"]

    def test_json_object_not_list_falls_back(self) -> None:
        """Per docstring: only a 1-3 list of strings is accepted."""
        backend = FakeLLMBackend(chat_response='{"sub": "not a list"}')
        planner = QueryPlanner()

        result = planner.decompose("operator query", llm_backend=backend)

        assert result == ["operator query"]

    def test_list_too_long_falls_back(self) -> None:
        """Lists longer than 3 sub-queries violate the contract → ``[query]``."""
        backend = FakeLLMBackend(chat_response='["a", "b", "c", "d", "e"]')
        planner = QueryPlanner()

        result = planner.decompose("operator query", llm_backend=backend)

        assert result == ["operator query"]

    def test_empty_list_falls_back(self) -> None:
        """The contract requires 1 <= len(subs) <= 3."""
        backend = FakeLLMBackend(chat_response="[]")
        planner = QueryPlanner()

        result = planner.decompose("operator query", llm_backend=backend)

        assert result == ["operator query"]

    def test_llm_raise_surfaces_as_query_singleton(self) -> None:
        """Any LLM exception must surface as ``[query]`` — never raise."""
        backend = FakeLLMBackend(chat_raises=RuntimeError("API down"))
        planner = QueryPlanner()

        # Sabotage-prove anchor: this must NOT raise.
        result = planner.decompose("operator query", llm_backend=backend)

        assert result == ["operator query"]


# ---------------------------------------------------------------------------
# decompose() success contracts.
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestDecomposeSuccessContract:
    def test_valid_two_sub_queries_returned_verbatim(self) -> None:
        """A 2-element list of non-empty strings must round-trip unchanged."""
        backend = FakeLLMBackend(chat_response='["sub query one", "sub query two"]')
        planner = QueryPlanner()

        result = planner.decompose("compare X and Y", llm_backend=backend)

        assert result == ["sub query one", "sub query two"]

    def test_non_string_items_are_filtered(self) -> None:
        """Per the implementation contract, non-string items are dropped."""
        backend = FakeLLMBackend(chat_response='["good", 42, "also good"]')
        planner = QueryPlanner()

        result = planner.decompose("query", llm_backend=backend)

        assert result == ["good", "also good"]


# ---------------------------------------------------------------------------
# decompose() neo4j context wiring contract.
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestDecomposeNeo4jContract:
    def test_entity_context_injected_into_prompt(self) -> None:
        """When neo4j has matching entities, the prompt must carry their context.

        Drives the contract through the public surface: we wire a populated
        ``FakePlannerGraphClient`` and inspect the messages the LLM saw via
        ``FakeLLMBackend.chat_calls``.
        """
        backend = FakeLLMBackend(chat_response='["sub one", "sub two"]')
        graph = FakePlannerGraphClient(
            entities_by_word={
                "techcorp": [{"id": "tc", "name": "TechCorp"}],
            },
            related_by_id={
                "tc": [{"name": "GlobalTech"}, {"name": "BuilderCo"}],
            },
            available=True,
        )
        planner = QueryPlanner()

        planner.decompose("compare TechCorp and BuilderCo", neo4j_client=graph, llm_backend=backend)

        assert backend.chat_calls, "LLM must be called"
        prompt = backend.chat_calls[0]["messages"][0]["content"]
        assert "Known entities related to this query:" in prompt
        assert "TechCorp" in prompt
        assert "GlobalTech" in prompt

    def test_no_entity_context_when_client_unavailable(self) -> None:
        """available=False must skip the neo4j fetch and use the plain prompt."""
        backend = FakeLLMBackend(chat_response='["sub"]')
        graph = FakePlannerGraphClient(
            entities_by_word={"techcorp": [{"id": "tc", "name": "TechCorp"}]},
            related_by_id={"tc": [{"name": "GlobalTech"}]},
            available=False,
        )
        planner = QueryPlanner()

        planner.decompose("compare TechCorp things", neo4j_client=graph, llm_backend=backend)

        prompt = backend.chat_calls[0]["messages"][0]["content"]
        assert "Known entities related to this query:" not in prompt
        assert graph.find_calls == [], "find_by_name must not be called when unavailable"

    def test_no_entity_context_when_no_entities_found(self) -> None:
        """Empty entity lookups must produce the plain prompt, not a half-built one."""
        backend = FakeLLMBackend(chat_response='["sub"]')
        graph = FakePlannerGraphClient(entities_by_word={}, related_by_id={}, available=True)
        planner = QueryPlanner()

        planner.decompose("compare unknown things together", neo4j_client=graph, llm_backend=backend)

        prompt = backend.chat_calls[0]["messages"][0]["content"]
        assert "Known entities related to this query:" not in prompt


# ---------------------------------------------------------------------------
# neo4j_graph_context() contracts.
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestNeo4jGraphContextContract:
    def test_returns_none_when_no_entities(self) -> None:
        """No matches → None per docstring."""
        graph = FakePlannerGraphClient(entities_by_word={}, related_by_id={})

        assert neo4j_graph_context("anything goes here", graph) is None

    def test_returns_none_when_entities_have_no_relationships(self) -> None:
        """Entities found but no related_entities → None (no context lines)."""
        graph = FakePlannerGraphClient(
            entities_by_word={"alpha": [{"id": "a", "name": "Alpha"}]},
            related_by_id={"a": []},
        )

        assert neo4j_graph_context("Alpha topic overview", graph) is None

    def test_returns_formatted_context_when_entities_present(self) -> None:
        """Populated graph must produce the documented header + bullet format."""
        graph = FakePlannerGraphClient(
            entities_by_word={"alpha": [{"id": "a", "name": "Alpha"}]},
            related_by_id={"a": [{"name": "Beta"}, {"name": "Gamma"}]},
        )

        ctx = neo4j_graph_context("Alpha topic overview", graph)

        assert ctx is not None
        assert ctx.startswith("Known entities related to this query:")
        assert "Alpha" in ctx
        assert "Beta" in ctx
        assert "Gamma" in ctx
        # Documented arrow format
        assert "Alpha →" in ctx

    def test_find_by_name_exception_surfaces_as_none(self) -> None:
        """``Never raises`` claim: find_by_name failures must not propagate."""
        graph = FakePlannerGraphClient(
            entities_by_word={"alpha": [{"id": "a", "name": "Alpha"}]},
            find_raises=RuntimeError("neo4j down"),
        )

        # Sabotage-prove anchor: must NOT raise.
        result = neo4j_graph_context("Alpha topic overview", graph)

        assert result is None

    def test_related_entities_exception_surfaces_as_none(self) -> None:
        """``Never raises``: related_entities failures must not propagate."""
        graph = FakePlannerGraphClient(
            entities_by_word={"alpha": [{"id": "a", "name": "Alpha"}]},
            related_raises=RuntimeError("neo4j timeout"),
        )

        # Must NOT raise.
        result = neo4j_graph_context("Alpha topic overview", graph)

        assert result is None

    def test_self_referential_relationships_filtered(self) -> None:
        """An entity's relationship to itself must not appear in the context."""
        graph = FakePlannerGraphClient(
            entities_by_word={"alpha": [{"id": "a", "name": "Alpha"}]},
            related_by_id={"a": [{"name": "Alpha"}, {"name": "Beta"}]},
        )

        ctx = neo4j_graph_context("Alpha topic overview", graph)

        assert ctx is not None
        # Header line itself contains "Alpha →"; the related list must NOT
        # repeat "Alpha" as a related name (i.e. no "→ Alpha" or
        # "Alpha, Alpha"). Verify by inspecting the bullet line directly.
        bullet = next((line for line in ctx.splitlines() if line.startswith("- ")), "")
        # The related list (everything after the arrow) must not contain Alpha.
        _, _, related_part = bullet.partition("→")
        assert "Alpha" not in related_part
        assert "Beta" in related_part


# ---------------------------------------------------------------------------
# "Never raises" cross-cutting contract: a totally broken graph + LLM combo
# must still produce ``[query]`` so the search pipeline keeps moving.
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestPlannerNeverRaisesContract:
    def test_decompose_swallows_neo4j_exception_and_completes(self) -> None:
        """Even if the graph throws, decompose must finish and call the LLM."""
        backend = FakeLLMBackend(chat_response='["safe sub query one", "safe sub query two"]')
        graph = FakePlannerGraphClient(
            entities_by_word={"alpha": [{"id": "a", "name": "Alpha"}]},
            find_raises=RuntimeError("graph crashed"),
            available=True,
        )
        planner = QueryPlanner()

        result = planner.decompose("Alpha and Beta plus more", neo4j_client=graph, llm_backend=backend)

        assert result == ["safe sub query one", "safe sub query two"]
        assert len(backend.chat_calls) == 1

    def test_decompose_with_broken_graph_and_broken_llm_falls_back(self) -> None:
        """Both layers broken → ``[query]`` (no exception, no empty list)."""
        backend = FakeLLMBackend(chat_raises=RuntimeError("LLM dead"))
        graph = FakePlannerGraphClient(
            entities_by_word={"alpha": [{"id": "a", "name": "Alpha"}]},
            find_raises=RuntimeError("graph dead"),
            available=True,
        )
        planner = QueryPlanner()

        result = planner.decompose("Alpha plus Beta question", neo4j_client=graph, llm_backend=backend)

        assert result == ["Alpha plus Beta question"]
