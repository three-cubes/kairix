"""Integration test for the MCP cold-start affordance (#278).

When an agent calls an MCP tool on a not-yet-warmed kairix, the tool
returns the structured ColdStart envelope immediately — never a silent
multi-second block, never an opaque error. The envelope carries the
retry ETA and guidance so the agent commits 'kairix is warming up, retry
in N seconds' to memory rather than 'kairix is flaky'.

The per-directory conftest pre-marks state warm via an autouse fixture,
so EVERY OTHER integration test runs the production tool path. This
test deliberately resets state cold so the affordance path fires.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kairix.agents.mcp.server import build_server
from kairix.platform.warm.state import reset_warm_state

pytestmark = pytest.mark.integration


def _payload_from_call(raw: Any) -> dict[str, Any]:
    """Decode whatever shape FastMCP's call_tool returned into a dict."""
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        return raw[1]
    if isinstance(raw, list) and raw and hasattr(raw[0], "text"):
        return json.loads(raw[0].text)
    return raw  # type: ignore[no-any-return]  # raw shape varies across FastMCP versions; runtime narrowing


def _call_tool(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _payload_from_call(asyncio.run(server.call_tool(name, arguments)))


@pytest.fixture(autouse=True)
def _force_cold_state() -> None:
    """Override the directory-level autouse fixture — these tests want cold."""
    reset_warm_state()
    yield
    reset_warm_state()


@pytest.mark.integration
def test_cold_search_returns_immediate_affordance_envelope() -> None:
    """An agent calling `search` on a cold kairix gets ColdStart, not a slow result.

    Sabotage-proof: comment out the _check_warm_or_return_envelope call in the
    search registration and this test breaks because the real search runs.
    """
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "search", {"query": "anything", "budget": 1000})

    assert payload.get("error") == "ColdStart", (
        f"cold search must return ColdStart envelope; got error={payload.get('error')!r}, keys={sorted(payload.keys())}"
    )
    assert payload["tool"] == "search"
    assert "Retry" in payload["guidance"]
    assert "estimated_seconds_remaining" in payload


@pytest.mark.integration
def test_cold_entity_returns_affordance_envelope() -> None:
    """Same affordance for entity lookups."""
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "entity", {"name": "anything"})
    assert payload.get("error") == "ColdStart"
    assert payload["tool"] == "entity"


@pytest.mark.integration
def test_cold_prep_returns_affordance_envelope() -> None:
    """Same affordance for prep."""
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "prep", {"query": "anything"})
    assert payload.get("error") == "ColdStart"
    assert payload["tool"] == "prep"
