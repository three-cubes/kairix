"""Contract: CLI ↔ MCP parity for the ``entity get`` operation (Phase 3e of #168)."""

from __future__ import annotations

import inspect
import typing

import pytest


@pytest.mark.contract
def test_cli_cmd_get_uses_run_entity_get() -> None:
    from kairix.knowledge.entities import cli

    src = inspect.getsource(cli.cmd_get)
    assert "run_entity_get(" in src


@pytest.mark.contract
def test_mcp_tool_entity_uses_run_entity_get() -> None:
    from kairix.agents.mcp import server

    src = inspect.getsource(server.tool_entity)
    assert "run_entity_get(" in src


@pytest.mark.contract
def test_use_case_returns_entity_get_output() -> None:
    from kairix.use_cases.entity_get import EntityGetOutput, run_entity_get

    hints = typing.get_type_hints(run_entity_get)
    assert hints.get("return") is EntityGetOutput


@pytest.mark.contract
def test_envelope_keys_match_entity_get_output() -> None:
    from kairix.use_cases import entity_get as uc

    src = inspect.getsource(uc.entity_get_output_to_envelope)
    for key in ("id", "name", "type", "summary", "vault_path", "error"):
        assert f'"{key}"' in src


@pytest.mark.contract
def test_entity_get_subcommand_is_registered() -> None:
    """The entity CLI's parser exposes a ``get`` subcommand."""
    from kairix.knowledge.entities.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["get", "Acme"])
    assert args.command == "get"
    assert args.name == "Acme"
