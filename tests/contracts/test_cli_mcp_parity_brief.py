"""Contract: CLI ↔ MCP parity for the ``brief`` operation (Phase 3a of #168)."""

from __future__ import annotations

import inspect
import typing

import pytest


@pytest.mark.contract
def test_cli_main_calls_run_brief_use_case() -> None:
    from kairix.agents.briefing import cli

    src = inspect.getsource(cli)
    assert "from kairix.use_cases.brief import" in src
    assert "run_brief(" in src


@pytest.mark.contract
def test_mcp_tool_brief_calls_run_brief_use_case() -> None:
    from kairix.agents.mcp import server

    src = inspect.getsource(server.tool_brief)
    assert "from kairix.use_cases.brief import" in src
    assert "run_brief(" in src


@pytest.mark.contract
def test_cli_does_not_call_generate_briefing_directly() -> None:
    """CLI must NOT bypass the use case to invoke generate_briefing itself."""
    from kairix.agents.briefing import cli

    src = inspect.getsource(cli)
    assert "generate_briefing" not in src, "CLI bypasses run_brief — see #168 Phase 3a"


@pytest.mark.contract
def test_use_case_returns_brief_output_dataclass() -> None:
    from kairix.use_cases.brief import BriefOutput, run_brief

    hints = typing.get_type_hints(run_brief)
    assert hints.get("return") is BriefOutput


@pytest.mark.contract
def test_envelope_keys_match_brief_output_fields() -> None:
    from kairix.use_cases import brief as uc

    src = inspect.getsource(uc.brief_output_to_envelope)
    for key in ("agent", "content", "path", "preview", "error"):
        assert f'"{key}"' in src, f"envelope projector missing key {key!r}"


@pytest.mark.contract
def test_mcp_tool_brief_signature_minimal() -> None:
    """tool_brief takes ``agent`` (positional) plus ``deps`` (test seam)."""
    from kairix.agents.mcp.server import tool_brief

    params = list(inspect.signature(tool_brief).parameters)
    assert params[0] == "agent"
    assert "deps" in params
