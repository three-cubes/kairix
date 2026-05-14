"""
Prescriptive MCP tool descriptions (#246 W2).

The ``description=`` field on each MCP tool is the only affordance an
LLM agent sees in the tool list. Pre-W2 descriptions described what
each tool does (neutral voice); post-W2 they describe **when** to call
(prescriptive directive voice).

Sabotage proof: if any description regresses to neutral describing
voice — dropping "Call before", "Call at session start", "Call when",
or removing the prescriptive trigger phrase — the matching assertion
below fails.
"""

from __future__ import annotations

import asyncio

import pytest

from kairix.agents.mcp.server import build_server


def _descriptions_by_name() -> dict[str, str]:
    server = build_server(host="127.0.0.1", port=18093)
    tools = asyncio.run(server.list_tools())
    return {t.name: (t.description or "") for t in tools}


@pytest.mark.unit
def test_search_description_is_prescriptive_about_when_to_call() -> None:
    desc = _descriptions_by_name()["search"]
    # Trigger phrase + the proactive directive — sabotaging either flips
    # this test red.
    assert "Call before" in desc
    assert "factual question" in desc
    assert "proactively" in desc
    # Knowledge-store framing — distinguishes search from brief/research.
    assert "knowledge store" in desc


@pytest.mark.unit
def test_bootstrap_description_is_prescriptive_about_session_start() -> None:
    desc = _descriptions_by_name()["bootstrap"]
    assert "Call at session start" in desc
    # The vector-search degradation directive is what makes bootstrap an
    # observability surface, not just an orientation surface.
    assert "vector_search" in desc
    # Reference to the human handler — bootstrap is the agent's first
    # chance to escalate a degraded deployment.
    assert "surface" in desc.lower()


@pytest.mark.unit
def test_brief_description_is_prescriptive_about_synthesised_view() -> None:
    desc = _descriptions_by_name()["brief"]
    assert "Call when you want" in desc
    assert "synthesised" in desc
    # The "tempted to summarise from memory" anchor is the load-bearing
    # affordance that distinguishes brief from search; if it disappears
    # we've regressed back to neutral voice.
    assert "memory" in desc


@pytest.mark.unit
def test_entity_description_is_prescriptive_about_named_lookup() -> None:
    desc = _descriptions_by_name()["entity"]
    assert "Call when you need" in desc
    assert "named entity" in desc
    # Differentiation from search — "faster than search" earns its keep.
    assert "faster than search" in desc


@pytest.mark.unit
def test_no_prescriptive_tool_description_uses_neutral_voice() -> None:
    """The four prescriptive tools must NEVER drop the imperative trigger.

    Sabotage proof: replacing any of these with "This tool ..." or
    "Returns ..." would defeat the W2 affordance. Asserting on the
    leading directive verb keeps the constraint mechanical.
    """
    descs = _descriptions_by_name()
    for tool in ("search", "bootstrap", "brief", "entity"):
        assert descs[tool].lstrip().startswith("Call "), (
            f"tool {tool!r} description regressed away from prescriptive voice: {descs[tool]!r}"
        )
