"""Step definitions for tests/bdd/features/search_planner.feature.

Operator-visible BDD: a multi-hop query is decomposed into >=2 sub-queries;
a simple query passes through unchanged; a failing LLM falls back.

Drives the public ``QueryPlanner.decompose`` surface with the canonical
``FakeLLMBackend`` from ``tests.fakes``. No monkeypatching, no inline
stubs.
"""

from __future__ import annotations

from pytest_bdd import given, parsers, then, when

from kairix.core.search.planner import QueryPlanner
from tests.fakes import FakeLLMBackend

_state: dict = {}


@given("a planner backed by a fake LLM that returns two sub-queries")
def _planner_with_two_subs() -> None:
    _state.clear()
    _state["backend"] = FakeLLMBackend(chat_response='["aspect of X", "aspect of Y"]')
    _state["planner"] = QueryPlanner()


@given("a planner backed by a fake LLM that returns a single sub-query")
def _planner_with_one_sub() -> None:
    _state.clear()
    _state["backend"] = FakeLLMBackend(chat_response='["what is kairix"]')
    _state["planner"] = QueryPlanner()


@given("a planner backed by a fake LLM that always raises")
def _planner_with_failing_llm() -> None:
    _state.clear()
    _state["backend"] = FakeLLMBackend(chat_raises=RuntimeError("LLM unavailable"))
    _state["planner"] = QueryPlanner()


@when(parsers.parse('the operator decomposes the multi-hop query "{query}"'))
def _decompose_multi_hop(query: str) -> None:
    _state["query"] = query
    _state["result"] = _state["planner"].decompose(query, llm_backend=_state["backend"])


@when(parsers.parse('the operator decomposes the simple query "{query}"'))
def _decompose_simple(query: str) -> None:
    _state["query"] = query
    _state["result"] = _state["planner"].decompose(query, llm_backend=_state["backend"])


@then("the planner returns at least 2 sub-queries")
def _at_least_two() -> None:
    result = _state["result"]
    assert isinstance(result, list)
    assert len(result) >= 2, f"expected >=2 sub-queries, got {result}"


@then("the planner returns exactly 1 sub-query")
def _exactly_one() -> None:
    result = _state["result"]
    assert isinstance(result, list)
    assert len(result) == 1, f"expected exactly 1 sub-query, got {result}"


@then("the planner returns the original query unchanged")
def _original_query() -> None:
    result = _state["result"]
    assert result == [_state["query"]], f"expected fallback to {[_state['query']]}, got {result}"
