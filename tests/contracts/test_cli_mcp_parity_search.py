"""Contract: CLI ↔ MCP parity for the ``search`` operation (Phase 2 of #168)."""

from __future__ import annotations

import inspect
import typing

import pytest


@pytest.mark.contract
def test_cli_main_calls_run_search_use_case() -> None:
    from kairix.core.search import cli

    src = inspect.getsource(cli)
    assert "from kairix.use_cases.search import" in src
    assert "run_search(" in src


@pytest.mark.contract
def test_mcp_tool_search_calls_run_search_use_case() -> None:
    from kairix.agents.mcp import server

    src = inspect.getsource(server.tool_search)
    assert "from kairix.use_cases.search import run_search" in src
    assert "run_search(" in src


@pytest.mark.contract
def test_cli_does_not_drive_search_pipeline_directly() -> None:
    """CLI must NOT bypass the use case to call SearchPipeline.search itself.

    Pre-Phase 2, both surfaces called ``pipeline.search`` directly with
    their own intent classification + budget inference + entity-card
    augmentation. The use case now owns all three.
    """
    from kairix.core.search import cli

    src = inspect.getsource(cli)
    assert "pipeline.search" not in src, "CLI bypasses run_search — see #168 Phase 2"
    assert "build_search_pipeline" not in src, "CLI builds its own pipeline — must go via run_search"


@pytest.mark.contract
def test_mcp_tool_search_does_not_drive_pipeline_directly() -> None:
    from kairix.agents.mcp import server

    src = inspect.getsource(server.tool_search)
    assert "build_search_pipeline" not in src
    assert "_fetch_entity_card" not in src
    assert "_infer_budget" not in src


@pytest.mark.contract
def test_use_case_returns_search_output_dataclass() -> None:
    from kairix.use_cases.search import SearchOutput, run_search

    hints = typing.get_type_hints(run_search)
    assert hints.get("return") is SearchOutput


@pytest.mark.contract
def test_mcp_envelope_keys_match_search_output_fields() -> None:
    """The MCP JSON envelope keys are exactly the use case's SearchOutput fields.

    The keys live in ``search_output_to_envelope`` (the shared projection
    helper); the MCP adapter ``tool_search`` calls it. Both surfaces (CLI
    --json and MCP) reach it from different directions.
    """
    import kairix.use_cases.search as search_uc

    src = inspect.getsource(search_uc.search_output_to_envelope)
    for key in (
        "query",
        "intent",
        "results",
        "bm25_count",
        "vec_count",
        "fused_count",
        "vec_failed",
        "total_tokens",
        "latency_ms",
        "error",
    ):
        assert f'"{key}"' in src, f"envelope projector missing key {key!r}"
    for hit_key in ("path", "title", "snippet", "score", "tier", "tokens", "collection"):
        assert f'"{hit_key}"' in src, f"envelope projector hit missing key {hit_key!r}"


@pytest.mark.contract
def test_mcp_search_signature_exposes_limit() -> None:
    """Phase 2 fixes the drift where MCP lacked ``limit``.

    Both surfaces must accept ``limit`` so an agent can ask for the
    same amount of context as a CLI operator.
    """
    from kairix.agents.mcp.server import tool_search

    params = set(inspect.signature(tool_search).parameters)
    assert "limit" in params, "tool_search must expose limit (Phase 2 of #168)"
