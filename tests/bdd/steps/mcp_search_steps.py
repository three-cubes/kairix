"""Step definitions for MCP agent search tool BDD scenarios."""

from __future__ import annotations

from pytest_bdd import given, parsers, then, when

from kairix.core.search.budget import BudgetedResult
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchResult
from kairix.core.search.rrf import FusedResult

_state: dict = {}


def _make_search_result(query: str, intent: QueryIntent, paths: list[str]) -> SearchResult:
    """Build a SearchResult with BudgetedResult items for testing."""
    results = []
    for i, p in enumerate(paths):
        fr = FusedResult(
            path=p,
            collection="default",
            title=p.split("/")[-1].replace(".md", ""),
            snippet=f"Content from {p}",
            rrf_score=0.9 - i * 0.1,
            boosted_score=0.9 - i * 0.1,
            in_bm25=True,
        )
        results.append(BudgetedResult(result=fr, tier="L2", token_estimate=50, content=f"Content from {p}"))
    return SearchResult(
        query=query,
        intent=intent,
        results=results,
        bm25_count=len(paths),
        vec_count=0,
        fused_count=len(paths),
        total_tokens=50 * len(paths),
        latency_ms=10.0,
    )


@given(parsers.re(r'the hybrid search returns results for "(?P<term>.*)"'))
def given_search_returns_results(term):
    _state["mock_result"] = _make_search_result(
        query=term,
        intent=QueryIntent.KEYWORD,
        paths=[f"docs/{term.lower()}-overview.md", f"docs/{term.lower()}-detail.md"],
    )
    _state["search_raises"] = False


@given("the hybrid search returns no results")
def given_search_returns_empty():
    _state["mock_result"] = SearchResult(query="", intent=QueryIntent.SEMANTIC, results=[], total_tokens=0)
    _state["search_raises"] = False


@given("the hybrid search raises an error")
def given_search_raises():
    _state["search_raises"] = True


@given(parsers.re(r'Neo4j has entity card for "(?P<name>.*)" with summary "(?P<summary>.*)"'))
def given_neo4j_entity_card(name, summary):
    _state["entity_card"] = {
        "id": name.lower(),
        "name": name,
        "type": "Organisation",
        "summary": summary,
        "vault_path": f"entities/{name.lower()}.md",
    }
    # Override intent to ENTITY for this scenario
    if "mock_result" in _state:
        _state["mock_result"] = SearchResult(
            query=_state["mock_result"].query,
            intent=QueryIntent.ENTITY,
            results=_state["mock_result"].results,
            bm25_count=_state["mock_result"].bm25_count,
            total_tokens=_state["mock_result"].total_tokens,
            latency_ms=10.0,
        )


@when(parsers.re(r'the agent calls tool_search with query "(?P<query>.*)"'))
def when_agent_calls_search(query):
    """Drive the MCP adapter ``tool_search`` end-to-end via SearchDeps.

    Phase 2 of #168: ``tool_search`` is a thin adapter that forwards to
    ``run_search`` (passing ``deps`` through) and projects via
    ``search_output_to_envelope``. This step exercises the full path —
    adapter shell + use case body + envelope projection — without
    touching live Azure/Neo4j.
    """
    from kairix.agents.mcp.server import tool_search
    from kairix.use_cases.search import SearchDeps

    _state["exception"] = None

    def _search_fn(**kwargs):
        if _state.get("search_raises"):
            raise ValueError("test error")
        return _state["mock_result"]

    entity_card = _state.get("entity_card")
    deps = SearchDeps(
        search_fn=_search_fn,
        entity_card_fn=lambda name: entity_card,
        classify_fn=lambda q: _state["mock_result"].intent,
    )

    try:
        _state["response"] = tool_search(query=query, deps=deps)
    except Exception as exc:
        _state["exception"] = exc
        _state["response"] = {}


@then("the search response contains a results list")
def then_response_has_results():
    assert "results" in _state["response"]
    assert isinstance(_state["response"]["results"], list)


@then(parsers.re(r'the search response intent is "(?P<intent>.*)"'))
def then_response_intent(intent):
    assert _state["response"]["intent"] == intent


@then("the search response error is empty")
def then_response_error_empty():
    assert _state["response"].get("error", "") == ""


@then("each search result has path, score, snippet, and tokens")
def then_results_have_fields():
    for r in _state["response"]["results"]:
        assert "path" in r
        assert "score" in r
        assert "snippet" in r
        assert "tokens" in r


@then(parsers.re(r'the first search result source is "(?P<source>.*)"'))
def then_first_result_source(source):
    results = _state["response"]["results"]
    assert len(results) > 0
    assert results[0].get("source") == source


@then(parsers.re(r'the first search result snippet contains "(?P<text>.*)"'))
def then_first_result_snippet_contains(text):
    results = _state["response"]["results"]
    assert len(results) > 0
    assert text in results[0].get("snippet", "")


@then("no search exception was raised")
def then_no_search_exception():
    assert _state["exception"] is None


@then('the search response is a valid dict with key "error"')
def then_response_is_valid_dict():
    assert isinstance(_state["response"], dict)
    assert "error" in _state["response"]
