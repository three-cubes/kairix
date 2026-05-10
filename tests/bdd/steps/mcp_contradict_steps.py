"""Step definitions for mcp_agent_contradict.feature."""

from __future__ import annotations

from pytest_bdd import given, parsers, then, when

from kairix.knowledge.contradict.detector import ContradictionResult
from tests.fakes import FakeLLMBackend

# Module-level state (simple, test-scoped)
_state: dict = {}


@given("the search returns no contradicting documents")
def given_no_contradictions():
    _state["mock_contradictions"] = []
    _state["mock_raises"] = False


@given("the search finds a contradicting document")
def given_contradiction_found():
    _state["mock_contradictions"] = [
        ContradictionResult(
            doc_path="docs/architecture.md",
            score=0.85,
            reason="Existing document states microservices architecture, not monolith.",
            snippet="The system uses a microservices architecture...",
        ),
    ]
    _state["mock_raises"] = False


@given("the search raises an error")
def given_search_raises():
    _state["mock_raises"] = True


@when(parsers.re(r'the agent calls tool_contradict with content "(?P<content>[^"]*)"'))
def call_tool_contradict(content):
    """Drive the MCP adapter ``tool_contradict`` end-to-end via ContradictDeps."""
    from kairix.agents.mcp.server import tool_contradict
    from kairix.use_cases.contradict import ContradictDeps

    _state["exception"] = None
    _state["result"] = None

    fake_llm = FakeLLMBackend()

    def _check_fn(**kwargs):
        if _state.get("mock_raises"):
            raise RuntimeError("search unavailable")
        return _state["mock_contradictions"]

    deps = ContradictDeps(check_fn=_check_fn, llm_backend=fake_llm)

    try:
        _state["result"] = tool_contradict(content=content, deps=deps)
    except Exception as exc:
        _state["exception"] = exc


@then("the contradict response has_contradictions is false")
def has_contradictions_false():
    assert _state["exception"] is None, f"tool_contradict raised: {_state['exception']}"
    assert _state["result"]["has_contradictions"] is False


@then("the contradict response has_contradictions is true")
def has_contradictions_true():
    assert _state["exception"] is None, f"tool_contradict raised: {_state['exception']}"
    assert _state["result"]["has_contradictions"] is True


@then("the contradict response error is empty")
def error_is_empty():
    assert _state["result"]["error"] == "", f"Expected empty error, got {_state['result']['error']!r}"


@then("the contradict response contains at least one contradiction with a reason")
def at_least_one_contradiction_with_reason():
    contradictions = _state["result"]["contradictions"]
    assert len(contradictions) >= 1, f"Expected at least one contradiction, got {len(contradictions)}"
    assert contradictions[0].get("reason"), "First contradiction has no reason"


@then("no contradict exception was raised")
def no_exception():
    assert _state["exception"] is None, f"tool_contradict raised: {_state['exception']}"


@then("the contradict response is a valid dict")
def result_is_valid_dict():
    r = _state["result"]
    assert isinstance(r, dict), f"Expected dict, got {type(r)}"
    assert "has_contradictions" in r
    assert "contradictions" in r
    assert "error" in r
