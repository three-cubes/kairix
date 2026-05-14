"""End-to-end integration tests for ``kairix.agents.mcp.server.build_server``.

Constructs the real FastMCP server (the production wiring) and verifies:
- All seven kairix tools are registered with the expected names + descriptions.
- Each tool wrapper dispatches to its underlying ``tool_*`` function and
  returns a JSON-serialisable dict on the happy path.

The wrapper bodies (lines 735-815 of server.py) are otherwise uncovered by
unit tests because they only exist as inner closures inside ``build_server``.
This file exercises them through ``call_tool`` so the dispatch glue is
genuinely tested rather than just the underlying free functions.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kairix.agents.mcp.server import build_server

pytestmark = pytest.mark.integration


_EXPECTED_TOOLS = {
    "search",
    "entity",
    "prep",
    "timeline",
    "research",
    "contradict",
    "usage_guide",
    "brief",
    "entity_suggest",
    "entity_validate",
    "bootstrap",
}


def _list_tool_names(server: Any) -> set[str]:
    """Resolve FastMCP's async ``list_tools`` into a set of registered tool names."""
    return {t.name for t in asyncio.run(server.list_tools())}


def _call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    """Invoke a registered FastMCP tool through the real dispatch path."""
    return asyncio.run(server.call_tool(name, arguments))


@pytest.mark.integration
def test_build_server_returns_fastmcp_with_all_kairix_tools() -> None:
    """The constructed server must register every kairix tool by name."""
    from mcp.server.fastmcp import FastMCP

    server = build_server(host="127.0.0.1", port=18081)

    assert isinstance(server, FastMCP)
    assert _list_tool_names(server) == _EXPECTED_TOOLS, f"unexpected tool registration; got: {_list_tool_names(server)}"


@pytest.mark.integration
def test_build_server_usage_guide_tool_returns_guide_dict() -> None:
    """The ``usage_guide`` tool dispatch returns a dict shaped like ``tool_usage_guide`` does.

    This exercises the wrapper at server.py:811-813 (``return tool_usage_guide(topic=topic)``)
    by routing through the real FastMCP dispatch — not just by calling the underlying
    function directly.
    """
    server = build_server(host="127.0.0.1", port=18082)

    raw = _call_tool(server, "usage_guide", {"topic": ""})
    # FastMCP returns a (content_list, structured_dict) tuple. Pull the dict back out.
    payload = _payload_from_call(raw)
    assert isinstance(payload, dict)
    assert "topic" in payload and "content" in payload and "error" in payload


@pytest.mark.integration
def test_build_server_entity_tool_returns_error_when_no_neo4j() -> None:
    """``entity`` tool dispatch returns the production error path when Neo4j is unavailable."""
    server = build_server(host="127.0.0.1", port=18083)

    raw = _call_tool(server, "entity", {"name": "no-such-entity"})
    payload = _payload_from_call(raw)
    # In the test environment Neo4j is not running → the production tool returns an error dict.
    assert isinstance(payload, dict)
    assert payload.get("error", "") != ""
    assert payload.get("id", "_unset") == ""


@pytest.mark.integration
def test_build_server_search_tool_returns_dict_with_results_or_error() -> None:
    """``search`` tool dispatch returns a dict with either ``results`` or ``error`` populated.

    In the test env there is no FTS index and no embed credentials; the search
    pipeline returns an empty result rather than raising. This test asserts the
    dispatch glue + wrapper signature, not the search algorithm itself.
    """
    server = build_server(host="127.0.0.1", port=18084)
    raw = _call_tool(server, "search", {"query": "anything", "budget": 1000})
    payload = _payload_from_call(raw)
    assert isinstance(payload, dict)
    # Either results=[] (clean empty) or error="..." (clean failure) — never raises.
    assert "results" in payload or "error" in payload


@pytest.mark.integration
def test_build_server_prep_tool_returns_dict_via_dispatch() -> None:
    """``prep`` tool dispatch returns a dict; in test env without Azure / index it errors cleanly."""
    server = build_server(host="127.0.0.1", port=18085)
    raw = _call_tool(server, "prep", {"query": "anything"})
    payload = _payload_from_call(raw)
    assert isinstance(payload, dict)


@pytest.mark.integration
def test_build_server_timeline_tool_returns_dict_via_dispatch() -> None:
    """``timeline`` tool dispatch returns a dict; pipeline construction inside the wrapper is exercised."""
    server = build_server(host="127.0.0.1", port=18086)
    raw = _call_tool(server, "timeline", {"query": "what happened recently"})
    payload = _payload_from_call(raw)
    assert isinstance(payload, dict)


@pytest.mark.integration
def test_build_server_research_tool_returns_dict_via_dispatch() -> None:
    """``research`` tool dispatch returns a dict (the wrapper passes through to ``tool_research``)."""
    server = build_server(host="127.0.0.1", port=18087)
    raw = _call_tool(server, "research", {"query": "any topic"})
    payload = _payload_from_call(raw)
    assert isinstance(payload, dict)


@pytest.mark.integration
def test_build_server_contradict_tool_returns_dict_via_dispatch() -> None:
    """``contradict`` tool dispatch returns a dict (the wrapper passes through to ``tool_contradict``)."""
    server = build_server(host="127.0.0.1", port=18088)
    raw = _call_tool(server, "contradict", {"content": "any claim"})
    payload = _payload_from_call(raw)
    assert isinstance(payload, dict)
    # The contradict tool always returns this key regardless of the underlying outcome.
    assert "has_contradictions" in payload or payload.get("error", "") != ""


def _payload_from_call(raw: Any) -> Any:
    """Extract the structured dict payload from a FastMCP ``call_tool`` result.

    FastMCP returns a tuple of (content_list, structured) where ``structured`` is
    the JSON-serialisable dict our tools return. When ``structured`` isn't set
    (older FastMCP versions or non-dict returns) we fall back to parsing the
    text content.
    """
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        return raw[1]
    if isinstance(raw, list) and raw and hasattr(raw[0], "text"):
        return json.loads(raw[0].text)
    return raw
