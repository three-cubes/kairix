"""Contract test: MCP transport composer exposes a single public symbol.

Sprint 19 (WS1-2): the only module-level import other modules should reach
for is :func:`build_mcp_app`. Anything else in
``kairix.agents.mcp.transport`` is considered private (``_``-prefixed) and
must not be imported by the rest of the codebase or by tests.
"""

from __future__ import annotations

import pytest

# Skip when the optional [agents] extras aren't installed — the transport
# module imports starlette at module level. CI's contract stage runs base
# deps only; full-extras stages exercise this contract.
pytest.importorskip("starlette")

pytestmark = pytest.mark.contract


@pytest.mark.contract
def test_build_mcp_app_is_public_export() -> None:
    """``from kairix.agents.mcp.transport import build_mcp_app`` works."""
    from kairix.agents.mcp.transport import build_mcp_app

    assert callable(build_mcp_app)
    assert build_mcp_app.__name__ == "build_mcp_app"
