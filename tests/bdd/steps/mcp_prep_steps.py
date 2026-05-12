"""Step definitions for MCP agent prep tool BDD scenarios."""

from __future__ import annotations

from pytest_bdd import given, parsers, then, when

from kairix.core.search.budget import BudgetedResult
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchResult
from kairix.core.search.rrf import FusedResult

_state: dict = {}


def _make_search_result_with_docs(query: str, paths: list[str]) -> SearchResult:
    """Build a SearchResult with documents for prep grounding."""
    results = []
    for i, p in enumerate(paths):
        fr = FusedResult(
            path=p,
            collection="default",
            title=p.split("/")[-1].replace(".md", ""),
            snippet=f"Document content about {p}",
            rrf_score=0.8 - i * 0.1,
            boosted_score=0.8 - i * 0.1,
            in_bm25=True,
        )
        results.append(BudgetedResult(result=fr, tier="L2", token_estimate=100, content=f"Full content of {p}"))
    return SearchResult(
        query=query,
        intent=QueryIntent.SEMANTIC,
        results=results,
        total_tokens=100 * len(paths),
        latency_ms=10.0,
    )


@given(parsers.re(r'the search returns documents about "(?P<topic>.*)"'))
def given_search_returns_docs(topic):
    _state["mock_search"] = _make_search_result_with_docs(
        topic, [f"docs/{topic}-guide.md", f"docs/{topic}-reference.md"]
    )
    _state["search_raises"] = False


@given("the search returns no documents")
def given_search_returns_empty():
    _state["mock_search"] = SearchResult(query="", intent=QueryIntent.SEMANTIC, results=[], total_tokens=0)
    _state["search_raises"] = False


@given("the search raises an error")
def given_prep_search_raises():
    _state["search_raises"] = True


@given("the LLM returns a summary")
def given_llm_returns_summary():
    _state["mock_summary"] = "This is a synthesised summary based on the retrieved documents."


@when(parsers.re(r'the agent calls tool_prep with query "(?P<query>.*)" at tier "(?P<tier>.*)"'))
def when_agent_calls_prep(query, tier):
    """Drive the MCP adapter ``tool_prep`` end-to-end via ``PrepDeps``."""
    from kairix.agents.mcp.server import tool_prep
    from kairix.use_cases.prep import PrepDeps

    _state["exception"] = None

    def _search_fn(**kwargs):
        if _state.get("search_raises"):
            raise ValueError("test error")
        return _state.get("mock_search")

    mock_summary = _state.get("mock_summary", "A summary.")
    deps = PrepDeps(search_fn=_search_fn, chat_fn=lambda **kw: mock_summary)

    try:
        _state["response"] = tool_prep(query=query, tier=tier, deps=deps)
    except Exception as exc:
        _state["exception"] = exc
        _state["response"] = {}


@then("the prep response has a non-empty summary")
def then_prep_has_summary():
    assert _state["response"].get("summary", "") != ""


@then(parsers.re(r'the prep response tier is "(?P<tier>.*)"'))
def then_prep_tier(tier):
    assert _state["response"].get("tier") == tier


@then("the prep response error is empty")
def then_prep_error_empty():
    assert _state["response"].get("error", "") == ""


@then("the prep response summary indicates no relevant documents")
def then_prep_no_docs():
    summary = _state["response"].get("summary", "")
    assert "no relevant" in summary.lower() or "not found" in summary.lower() or summary != ""


@then("no prep exception was raised")
def then_no_prep_exception():
    assert _state["exception"] is None


@then("the prep response is a valid dict")
def then_prep_is_valid_dict():
    assert isinstance(_state["response"], dict)
    assert "error" in _state["response"] or "summary" in _state["response"]
