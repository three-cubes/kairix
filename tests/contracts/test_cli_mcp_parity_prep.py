"""Contract: CLI ↔ MCP parity for the ``prep`` operation (Phase 3c of #168)."""

from __future__ import annotations

import inspect
import typing

import pytest


@pytest.mark.contract
def test_cli_main_calls_run_prep() -> None:
    from kairix.agents.prep import cli

    src = inspect.getsource(cli.main)
    assert "run_prep(" in src


@pytest.mark.contract
def test_mcp_tool_prep_calls_run_prep() -> None:
    from kairix.agents.mcp import server

    src = inspect.getsource(server.tool_prep)
    assert "run_prep(" in src
    assert "from kairix.use_cases.prep import" in src


@pytest.mark.contract
def test_mcp_tool_prep_does_not_drive_pipeline_directly() -> None:
    from kairix.agents.mcp import server

    src = inspect.getsource(server.tool_prep)
    assert "build_search_pipeline" not in src
    assert "chat_completion" not in src


@pytest.mark.contract
def test_use_case_returns_prep_output() -> None:
    from kairix.use_cases.prep import PrepOutput, run_prep

    hints = typing.get_type_hints(run_prep)
    assert hints.get("return") is PrepOutput


@pytest.mark.contract
def test_envelope_keys_match_prep_output() -> None:
    from kairix.use_cases import prep as uc

    src = inspect.getsource(uc.prep_output_to_envelope)
    for key in ("query", "tier", "summary", "tokens", "sources", "error"):
        assert f'"{key}"' in src


@pytest.mark.contract
def test_kairix_prep_command_is_registered() -> None:
    """The dispatch table in kairix/cli.py exposes the prep command."""
    from kairix.cli import COMMANDS

    assert "prep" in COMMANDS
    assert COMMANDS["prep"][0] == "kairix.agents.prep.cli"
