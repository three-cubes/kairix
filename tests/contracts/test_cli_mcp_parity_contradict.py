"""Contract: CLI ↔ MCP parity for the ``contradict`` operation (Phase 2 of #168)."""

from __future__ import annotations

import inspect
import typing

import pytest


@pytest.mark.contract
def test_cli_main_calls_run_contradict_use_case() -> None:
    from kairix.knowledge.contradict import cli

    src = inspect.getsource(cli)
    assert "from kairix.use_cases.contradict import" in src
    assert "run_contradict(" in src


@pytest.mark.contract
def test_mcp_tool_contradict_calls_run_contradict_use_case() -> None:
    from kairix.agents.mcp import server

    src = inspect.getsource(server.tool_contradict)
    assert "from kairix.use_cases.contradict import" in src
    assert "run_contradict(" in src


@pytest.mark.contract
def test_cli_does_not_call_check_contradiction_directly() -> None:
    """CLI must NOT bypass the use case to invoke check_contradiction itself."""
    from kairix.knowledge.contradict import cli

    src = inspect.getsource(cli)
    assert "check_contradiction" not in src, "CLI bypasses run_contradict — see #168 Phase 2"


@pytest.mark.contract
def test_mcp_tool_contradict_does_not_call_check_contradiction_directly() -> None:
    from kairix.agents.mcp import server

    src = inspect.getsource(server.tool_contradict)
    assert "check_contradiction" not in src
    assert "get_default_backend" not in src


@pytest.mark.contract
def test_use_case_returns_contradict_output_dataclass() -> None:
    from kairix.use_cases.contradict import ContradictOutput, run_contradict

    hints = typing.get_type_hints(run_contradict)
    assert hints.get("return") is ContradictOutput


@pytest.mark.contract
def test_mcp_signature_exposes_top_claims() -> None:
    """Phase 2 fix for MCP missing ``top_claims`` (CLI already has it)."""
    from kairix.agents.mcp.server import tool_contradict

    params = set(inspect.signature(tool_contradict).parameters)
    assert "top_claims" in params, "tool_contradict must expose top_claims (Phase 2 of #168)"


@pytest.mark.contract
def test_envelope_keys_match_contradict_output_fields() -> None:
    from kairix.use_cases import contradict as uc

    src = inspect.getsource(uc.contradict_output_to_envelope)
    for key in ("content", "contradictions", "has_contradictions", "error"):
        assert f'"{key}"' in src, f"envelope projector missing key {key!r}"
    for hit_key in ("path", "score", "reason", "snippet", "category", "claim"):
        assert f'"{hit_key}"' in src, f"envelope projector hit missing key {hit_key!r}"
