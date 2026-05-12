"""Contract: CLI ↔ MCP parity for entity_suggest + entity_validate (Phase 3b of #168)."""

from __future__ import annotations

import inspect

import pytest


@pytest.mark.contract
def test_cli_suggest_uses_run_entity_suggest() -> None:
    from kairix.knowledge.entities import cli

    src = inspect.getsource(cli.cmd_suggest)
    assert "run_entity_suggest(" in src
    assert "from kairix.use_cases.entity import" in src


@pytest.mark.contract
def test_cli_validate_uses_run_entity_validate() -> None:
    from kairix.knowledge.entities import cli

    src = inspect.getsource(cli.cmd_validate)
    assert "run_entity_validate(" in src


@pytest.mark.contract
def test_cli_does_not_call_legacy_helpers_directly() -> None:
    """CLI must not bypass the use case to call suggest_entities/validate_entity."""
    from kairix.knowledge.entities import cli

    src = inspect.getsource(cli.cmd_suggest) + inspect.getsource(cli.cmd_validate)
    assert "suggest_entities(" not in src
    assert "validate_entity(" not in src


@pytest.mark.contract
def test_mcp_tools_call_use_cases() -> None:
    from kairix.agents.mcp import server

    suggest_src = inspect.getsource(server.tool_entity_suggest)
    validate_src = inspect.getsource(server.tool_entity_validate)
    assert "run_entity_suggest(" in suggest_src
    assert "run_entity_validate(" in validate_src
    assert "from kairix.use_cases.entity import" in suggest_src
    assert "from kairix.use_cases.entity import" in validate_src


@pytest.mark.contract
def test_mcp_signatures_minimal_and_explicit() -> None:
    from kairix.agents.mcp.server import tool_entity_suggest, tool_entity_validate

    suggest_params = list(inspect.signature(tool_entity_suggest).parameters)
    validate_params = list(inspect.signature(tool_entity_validate).parameters)
    assert suggest_params[0] == "text"
    assert "deps" in suggest_params
    assert validate_params[0] == "name"
    assert "update" in validate_params
    assert "deps" in validate_params


@pytest.mark.contract
def test_envelopes_include_documented_keys() -> None:
    from kairix.use_cases import entity as uc

    suggest_src = inspect.getsource(uc.entity_suggest_output_to_envelope)
    validate_src = inspect.getsource(uc.entity_validate_output_to_envelope)
    for key in ("text", "suggestions", "new_count", "existing_count", "error"):
        assert f'"{key}"' in suggest_src
    for key in ("name", "neo4j_id", "matches", "updated", "error"):
        assert f'"{key}"' in validate_src
