"""Unit tests for ``kairix.agents.mcp.server.tool_brief``.

The MCP adapter is a 4-line glue function. Coverage for the use-case
body lives in ``tests/use_cases/test_brief.py``; this test drives the
adapter shell via the typed-deps forwarder so the projection through
``brief_output_to_envelope`` is exercised end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.agents.mcp.server import tool_brief
from kairix.core.health import HealthDeps
from kairix.use_cases.brief import BriefDeps

pytestmark = pytest.mark.unit


def _healthy_health_deps() -> HealthDeps:
    return HealthDeps(
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
        neo4j_available_fn=lambda: True,
    )


def test_tool_brief_happy_path_returns_envelope_dict() -> None:
    deps = BriefDeps(
        generate_fn=lambda agent, **_: "line 1\nline 2\nline 3",
        briefing_dir_fn=lambda: Path("/var/kairix"),
        health_deps=_healthy_health_deps(),
    )
    result = tool_brief(agent="builder", deps=deps)

    assert result["agent"] == "builder"
    assert result["content"] == "line 1\nline 2\nline 3"
    assert result["path"] == "/var/kairix/builder-latest.md"
    assert result["preview"] == "line 1\nline 2\nline 3"
    assert result["error"] == ""
    # Health snapshot is now part of every tool envelope (#246 W3).
    assert result["health"]["chat"] == "ok"
    assert result["health"]["next_action"] == ""


def test_tool_brief_invalid_agent_returns_error_envelope() -> None:
    deps = BriefDeps(health_deps=_healthy_health_deps())
    result = tool_brief(agent="rogue", deps=deps)
    assert result["error"].startswith("InvalidAgent")
    assert result["content"] == ""


def test_tool_brief_generate_failure_returns_error_envelope() -> None:
    def _boom(agent: str, **_: object) -> str:
        raise RuntimeError("generate failed")

    deps = BriefDeps(generate_fn=_boom, briefing_dir_fn=lambda: Path("/x"), health_deps=_healthy_health_deps())
    result = tool_brief(agent="builder", deps=deps)
    assert result["error"].startswith("RuntimeError")
    assert result["content"] == ""
