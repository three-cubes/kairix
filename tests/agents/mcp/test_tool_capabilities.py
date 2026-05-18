"""Unit tests for ``tool_capabilities`` — the programmatic capability catalogue.

Per affordance pattern 4 (docs/architecture/operational-tests-design.md), the
catalogue is what an AI-driven SRE agent introspects to discover the kairix
surface. These tests pin the shape, vocabulary, and the catalogue↔registry
contract so a drift between the hand-maintained list and the FastMCP
registration breaks at unit-test time, not at runtime against an LLM.

Each test carries a ``# Sabotage:`` comment naming a concrete production
change that falsifies it — used to evidence sabotage-proofing in the
review-gate checklist.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import kairix.agents.mcp.server as server_module
from kairix.agents.mcp.server import (
    CAP_CATEGORY_AGENT,
    CAP_CATEGORY_DIAGNOSTIC,
    CAP_CATEGORY_DIAGNOSTIC_OPERATOR_ONLY,
    CAP_CATEGORY_KNOWLEDGE_WRITE,
    CAP_CATEGORY_RETRIEVAL,
    CAP_CATEGORY_SYNTHESIS,
    MCP_PROBE_CONCURRENCY_CAP,
    MCP_PROBE_QUERIES_CAP,
    build_server,
    tool_capabilities,
)

pytestmark = pytest.mark.unit


_WELL_KNOWN_CATEGORIES = {
    CAP_CATEGORY_RETRIEVAL,
    CAP_CATEGORY_SYNTHESIS,
    CAP_CATEGORY_DIAGNOSTIC,
    CAP_CATEGORY_DIAGNOSTIC_OPERATOR_ONLY,
    CAP_CATEGORY_KNOWLEDGE_WRITE,
    CAP_CATEGORY_AGENT,
}


def test_returns_dict_with_capabilities_key() -> None:
    """Top-level envelope must expose `capabilities` (list) + `schema_version`."""
    # Sabotage: rename the "capabilities" key to "items" in tool_capabilities()
    # → this assertion fails.
    out = tool_capabilities()
    assert isinstance(out, dict)
    assert "capabilities" in out
    assert isinstance(out["capabilities"], list)
    assert out["capabilities"], "catalogue must not be empty"
    assert "schema_version" in out


def test_every_entry_has_required_keys() -> None:
    """Every entry must carry `name`, `mcp_tool`, `cli`, `category`.

    Entries whose `mcp_tool` is None (escalation-only / CLI-only) MUST point
    to an escalation target via `escalate_via`.
    """
    # Sabotage: drop the `cli` key from any catalogue entry → this fails.
    required = {"name", "mcp_tool", "cli", "category"}
    for entry in tool_capabilities()["capabilities"]:
        missing = required - entry.keys()
        assert not missing, f"entry {entry.get('name')!r} missing keys: {missing}"
        if entry["mcp_tool"] is None:
            assert entry.get("escalate_via"), f"entry {entry['name']!r} has mcp_tool=None but no escalate_via target"


def test_categories_are_well_known() -> None:
    """Every entry's category string must be in the well-known set."""
    # Sabotage: change one entry's category to "misc" → this assertion fails.
    for entry in tool_capabilities()["capabilities"]:
        assert entry["category"] in _WELL_KNOWN_CATEGORIES, (
            f"entry {entry['name']!r} has unknown category {entry['category']!r}; "
            f"allowed: {sorted(_WELL_KNOWN_CATEGORIES)}"
        )


def test_probe_search_entry_has_mcp_caps() -> None:
    """probe_search exposes the agent-safe caps verbatim (20 queries / 3 concurrency)."""
    # Sabotage: drop `mcp_caps` from the probe_search entry, or change the
    # MCP_PROBE_QUERIES_CAP/MCP_PROBE_CONCURRENCY_CAP module constants → fails.
    catalogue = {e["name"]: e for e in tool_capabilities()["capabilities"]}
    probe = catalogue["probe_search"]
    assert "mcp_caps" in probe, "probe_search entry must publish mcp_caps for agents"
    assert probe["mcp_caps"] == {"queries_max": 20, "concurrency_max": 3}
    # Pin the constants themselves — if either changes, the agent contract changes.
    assert MCP_PROBE_QUERIES_CAP == 20
    assert MCP_PROBE_CONCURRENCY_CAP == 3


def test_escalate_via_targets_match_existing_stubs() -> None:
    """Every `escalate_via` value must resolve to a real public surface.

    The target either appears as another catalogue entry's `mcp_tool` (i.e. a
    registered MCP wrapper), OR exists as a public ``tool_<name>`` symbol on
    ``kairix.agents.mcp.server`` (the canonical escalation stubs do).
    """
    # Sabotage: rename `tool_soak_run` to `tool_soak_runner` in server.py
    # without updating the escalate_via target → fails.
    entries = tool_capabilities()["capabilities"]
    registered_mcp_tools = {e["mcp_tool"] for e in entries if e["mcp_tool"]}
    for entry in entries:
        target = entry.get("escalate_via")
        if target is None:
            continue
        in_catalogue = target in registered_mcp_tools
        as_public_stub = hasattr(server_module, f"tool_{target}")
        assert in_catalogue or as_public_stub, (
            f"entry {entry['name']!r} escalates to {target!r}, "
            f"which is neither a registered MCP tool nor a public tool_<name> symbol"
        )


def test_catalogue_includes_every_registered_mcp_tool() -> None:
    """Every FastMCP-registered tool name must appear in the catalogue.

    Catches the failure mode: someone adds a new ``@server.tool()`` wrapper
    inside ``build_server`` but forgets to add the matching catalogue entry,
    so an introspecting agent can't see it. The catalogue exists exactly to
    prevent that drift.

    A registered tool may surface in the catalogue in either role: as another
    entry's `mcp_tool` (agent-callable), OR as another entry's `escalate_via`
    target (the escalation stubs that return an OperatorOnlyCapability
    envelope). Both forms are catalogued — the test just checks that nothing
    is silently registered without an entry.
    """
    # Sabotage: add a new @server.tool() wrapper inside build_server without
    # updating tool_capabilities() → this assertion fails.
    server = build_server(host="127.0.0.1", port=18190)
    registered = {t.name for t in asyncio.run(server.list_tools())}
    entries = tool_capabilities()["capabilities"]
    catalogued = {e["mcp_tool"] for e in entries if e["mcp_tool"]} | {
        e["escalate_via"] for e in entries if e.get("escalate_via")
    }
    missing = registered - catalogued
    assert not missing, (
        f"registered MCP tools missing from catalogue: {sorted(missing)}. "
        f"fix: add a catalogue entry in tool_capabilities() for each."
    )


def test_catalogue_is_stable_round_trip() -> None:
    """Two calls return equal dicts — no timestamps, no nondeterminism."""
    # Sabotage: add a `"generated_at": time.time()` field to the envelope →
    # this equality fails.
    assert tool_capabilities() == tool_capabilities()


def test_envelope_serialises_via_json_dumps() -> None:
    """The catalogue must JSON-serialise cleanly and round-trip back equal.

    MCP transports the envelope over JSON, so any tuple/set/datetime leakage
    here would silently corrupt the agent's view.
    """
    # Sabotage: change one `category` value from str to an enum instance →
    # json.dumps raises TypeError and this test fails.
    original = tool_capabilities()
    encoded = json.dumps(original)
    decoded = json.loads(encoded)
    assert decoded == original
