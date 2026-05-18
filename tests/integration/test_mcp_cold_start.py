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


# ---------------------------------------------------------------------------
# v2026.5.17: the cold-start gate now covers every retrieval/synthesis tool,
# not just search/entity/prep. An agent's first call against a cold container
# (typically ``bootstrap`` or ``brief`` at session start) returns the
# ColdStart envelope instead of a transport-level "fetch failed".
#
# Sabotage proof for each: remove the ``cold = _check_warm_or_return_envelope(...)``
# line from the tool's registration in ``build_server`` and the corresponding
# test breaks because the real tool body runs against the not-yet-warm pipeline.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cold_bootstrap_returns_affordance_envelope() -> None:
    """An agent's session-start bootstrap call on a cold kairix gets the
    ColdStart envelope, not a transport failure.

    This is the path the user reported: agents calling ``bootstrap`` first
    used to see "fetch failed" because bootstrap didn't gate on warm state.
    """
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "bootstrap", {"agent": "anyone"})
    assert payload.get("error") == "ColdStart"
    assert payload["tool"] == "bootstrap"
    assert "Retry" in payload["guidance"]


@pytest.mark.integration
def test_cold_brief_returns_affordance_envelope() -> None:
    """Brief — agent-facing synthesis tool — gates on warm."""
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "brief", {"agent": "anyone"})
    assert payload.get("error") == "ColdStart"
    assert payload["tool"] == "brief"


@pytest.mark.integration
def test_cold_timeline_returns_affordance_envelope() -> None:
    """Timeline — temporal retrieval — gates on warm."""
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "timeline", {"query": "anything"})
    assert payload.get("error") == "ColdStart"
    assert payload["tool"] == "timeline"


@pytest.mark.integration
def test_cold_research_returns_affordance_envelope() -> None:
    """Research — iterative search loop — gates on warm."""
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "research", {"query": "anything"})
    assert payload.get("error") == "ColdStart"
    assert payload["tool"] == "research"


@pytest.mark.integration
def test_cold_contradict_returns_affordance_envelope() -> None:
    """Contradict — uses retrieval to find conflicting claims — gates on warm."""
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "contradict", {"content": "anything"})
    assert payload.get("error") == "ColdStart"
    assert payload["tool"] == "contradict"


@pytest.mark.integration
def test_cold_entity_suggest_returns_affordance_envelope() -> None:
    """Entity suggest — uses NER + Neo4j — gates on warm."""
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "entity_suggest", {"text": "anything"})
    assert payload.get("error") == "ColdStart"
    assert payload["tool"] == "entity_suggest"


@pytest.mark.integration
def test_cold_entity_validate_returns_affordance_envelope() -> None:
    """Entity validate — uses Neo4j + Wikidata lookup — gates on warm."""
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "entity_validate", {"name": "anything"})
    assert payload.get("error") == "ColdStart"
    assert payload["tool"] == "entity_validate"


# Diagnostic tools (onboard_check, worker_status, warm, capabilities, probes,
# operator escalations) deliberately do NOT gate on warm — they exist to
# diagnose the cold state itself or to perform the warm-up. usage_guide
# returns a static document, also no gate.
@pytest.mark.integration
def test_cold_usage_guide_does_not_gate_on_warm() -> None:
    """``usage_guide`` returns a static document; it must work on a cold
    container so an agent can read the guide while waiting for warm.

    Sabotage-proof: add ``_check_warm_or_return_envelope("usage_guide")``
    to the registration and this test breaks because usage_guide starts
    returning ColdStart instead of the guide.
    """
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "usage_guide", {})
    assert payload.get("error") != "ColdStart", "usage_guide must serve while cold"


@pytest.mark.integration
def test_cold_onboard_check_does_not_gate_on_warm() -> None:
    """``onboard_check`` is the deployment health probe — it must run on a
    cold container so operators can diagnose what's wrong.
    """
    server = build_server(host="127.0.0.1", port=18099)
    payload = _call_tool(server, "onboard_check", {})
    assert payload.get("error") != "ColdStart", "onboard_check must run while cold"
