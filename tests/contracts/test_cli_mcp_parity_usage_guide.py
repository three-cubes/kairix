"""Contract: CLI ↔ MCP parity for the ``usage_guide`` operation (Phase 3f of #168)."""

from __future__ import annotations

import inspect
import typing

import pytest


@pytest.mark.contract
def test_cli_main_calls_run_usage_guide() -> None:
    from kairix.agents.usage_guide import cli

    src = inspect.getsource(cli.main)
    assert "run_usage_guide(" in src


@pytest.mark.contract
def test_mcp_tool_usage_guide_calls_run_usage_guide() -> None:
    from kairix.agents.mcp import server

    src = inspect.getsource(server.tool_usage_guide)
    assert "run_usage_guide(" in src
    assert "from kairix.use_cases.usage_guide import" in src


@pytest.mark.contract
def test_mcp_tool_usage_guide_does_not_extract_topics_directly() -> None:
    """The topic-section extractor moved into the use case in Phase 3f."""
    from kairix.agents.mcp import server

    src = inspect.getsource(server)
    assert "_extract_topic_sections" not in src
    assert "_resolve_guide_path" not in src


@pytest.mark.contract
def test_use_case_returns_usage_guide_output() -> None:
    from kairix.use_cases.usage_guide import UsageGuideOutput, run_usage_guide

    hints = typing.get_type_hints(run_usage_guide)
    assert hints.get("return") is UsageGuideOutput


@pytest.mark.contract
def test_envelope_keys_match_usage_guide_output() -> None:
    from kairix.use_cases import usage_guide as uc

    src = inspect.getsource(uc.usage_guide_output_to_envelope)
    for key in ("topic", "content", "error"):
        assert f'"{key}"' in src


@pytest.mark.contract
def test_kairix_usage_guide_command_is_registered() -> None:
    from kairix.cli import COMMANDS

    assert "usage-guide" in COMMANDS
    assert COMMANDS["usage-guide"][0] == "kairix.agents.usage_guide.cli"
