"""Step definitions for mcp_agent_timeline.feature."""

from pytest_bdd import parsers, then, when

from kairix.agents.mcp.server import tool_timeline

# Module-level state (simple, test-scoped)
_state: dict = {}


@when(parsers.re(r'the agent calls tool_timeline with query "(?P<query>[^"]*)"(?: and anchor "(?P<anchor>[^"]*)")?'))
def call_tool_timeline(query, anchor):
    _state["exception"] = None
    _state["result"] = None
    try:
        _state["result"] = tool_timeline(query=query, anchor_date=anchor if anchor else None)
    except Exception as exc:
        _state["exception"] = exc


@then("the timeline response is_temporal is true")
def is_temporal_true():
    assert _state["exception"] is None, f"tool_timeline raised: {_state['exception']}"
    assert _state["result"]["is_temporal"] is True


@then("the timeline response is_temporal is false")
def is_temporal_false():
    assert _state["exception"] is None, f"tool_timeline raised: {_state['exception']}"
    assert _state["result"]["is_temporal"] is False


@then("the timeline response time_window has start and end dates")
def time_window_has_dates():
    tw = _state["result"]["time_window"]
    assert tw.get("start"), f"time_window missing start: {tw}"
    assert tw.get("end"), f"time_window missing end: {tw}"


@then(parsers.parse('the timeline response time_window start is "{expected}"'))
def time_window_start(expected):
    tw = _state["result"]["time_window"]
    assert tw["start"] == expected, f"Expected start {expected!r}, got {tw['start']!r}"


@then(parsers.parse('the timeline response time_window end is "{expected}"'))
def time_window_end(expected):
    tw = _state["result"]["time_window"]
    assert tw["end"] == expected, f"Expected end {expected!r}, got {tw['end']!r}"


@then("the timeline response error is empty")
def error_is_empty():
    assert _state["result"]["error"] == "", f"Expected empty error, got {_state['result']['error']!r}"


@then("the rewritten query equals the original query")
def rewritten_equals_original():
    r = _state["result"]
    assert r["rewritten_query"] == r["original_query"], (
        f"Expected rewritten == original, got {r['rewritten_query']!r} != {r['original_query']!r}"
    )


@then("no exception was raised")
def no_exception():
    assert _state["exception"] is None, f"tool_timeline raised: {_state['exception']}"


@then("the timeline response is a valid dict")
def result_is_valid_dict():
    r = _state["result"]
    assert isinstance(r, dict), f"Expected dict, got {type(r)}"
    assert "is_temporal" in r
    assert "original_query" in r
    assert "rewritten_query" in r
    assert "error" in r
