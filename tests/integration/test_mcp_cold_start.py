"""Integration test for the MCP cold-start affordance (#278).

When an agent calls an MCP tool on a not-yet-warmed kairix, the gated
tools return the structured ColdStart envelope immediately — never a
silent multi-second block, never an opaque "fetch failed". The envelope
carries the retry ETA and guidance so the agent commits 'kairix is
warming up, retry in N seconds' to memory rather than 'kairix is flaky'.

Diagnostic tools (usage_guide, onboard_check, worker_status, warm,
capabilities, probes, operator escalations) are deliberately NOT gated
— they exist to diagnose the cold state itself or to perform the
warm-up.

Gating shape: ``@warm_gate`` decorator in ``kairix/agents/mcp/server.py``.
The decorator is the single source of truth — these tests pin its
behaviour across every gated tool via parametrisation, so adding /
removing a gated tool only requires updating the table below, not
writing another test function.

The per-directory conftest pre-marks state warm via an autouse fixture,
so EVERY OTHER integration test runs the production tool path. This
module overrides that fixture to reset cold so the affordance path fires.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kairix.agents.mcp.server import build_server
from kairix.platform.warm.state import reset_warm_state

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Tool inventories — single source of truth for which tools gate and which
# don't. Adding a new tool means updating ONE entry; the parametrised tests
# below cover it automatically.
# ---------------------------------------------------------------------------

# (tool_name, minimal_sample_args) for every ``@warm_gate``-decorated tool
# in ``build_server``. Order matches the ``build_server`` registration order
# so the file reads top-to-bottom alongside ``server.py``.
GATED_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("search", {"query": "anything", "budget": 1000}),
    ("entity", {"name": "anything"}),
    ("prep", {"query": "anything"}),
    ("timeline", {"query": "anything"}),
    ("research", {"query": "anything"}),
    ("contradict", {"content": "anything"}),
    ("brief", {"agent": "anyone"}),
    ("bootstrap", {"agent": "anyone"}),
    ("entity_suggest", {"text": "anything"}),
    ("entity_validate", {"name": "anything"}),
]

# Tools that must STILL serve real responses while cold — they exist to
# diagnose the cold state itself, or return static content.
UNGATED_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("usage_guide", {}),
    ("onboard_check", {}),
]


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Cold-start envelope on every gated tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("tool_name", "sample_args"), GATED_TOOLS)
def test_gated_tool_returns_cold_start_envelope_when_cold(
    tool_name: str,
    sample_args: dict[str, Any],
) -> None:
    """Every ``@warm_gate``-decorated MCP tool returns the ColdStart envelope
    on a not-yet-warm container — instead of "fetch failed", instead of an
    8-second block, instead of an opaque error.

    Sabotage-proof: remove ``@warm_gate`` from any tool in ``build_server``
    and this parametrised case fires for that tool — the body runs against
    the not-yet-warm pipeline and the envelope assertion fails.

    Envelope shape is pinned: ``error == "ColdStart"``, ``tool`` matches
    the calling tool name, ``guidance`` carries a retry ETA, and the
    ``estimated_seconds_remaining`` field gives the agent a number to wait.
    """
    server = build_server(host="127.0.0.1", port=18099)

    payload = _call_tool(server, tool_name, sample_args)

    assert payload.get("error") == "ColdStart", (
        f"cold {tool_name} must return ColdStart envelope; "
        f"got error={payload.get('error')!r}, keys={sorted(payload.keys())}"
    )
    assert payload["tool"] == tool_name
    assert "Retry" in payload["guidance"]
    assert "estimated_seconds_remaining" in payload


# ---------------------------------------------------------------------------
# Diagnostic / static tools must NOT gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("tool_name", "sample_args"), UNGATED_TOOLS)
def test_ungated_tool_serves_while_cold(
    tool_name: str,
    sample_args: dict[str, Any],
) -> None:
    """Diagnostic and static tools must serve real responses while kairix
    is cold — operators need them to diagnose what's wrong, and ``usage_guide``
    returns a static document agents can read while waiting for warm-up.

    Sabotage-proof: add ``@warm_gate`` to one of these tools in
    ``build_server`` and this parametrised case fires — the tool starts
    returning ColdStart instead of its real response.
    """
    server = build_server(host="127.0.0.1", port=18099)

    payload = _call_tool(server, tool_name, sample_args)

    assert payload.get("error") != "ColdStart", (
        f"{tool_name} must serve while cold; got ColdStart envelope: {payload!r}"
    )
