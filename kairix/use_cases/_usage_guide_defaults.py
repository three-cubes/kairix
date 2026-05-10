"""Production wiring for ``run_usage_guide``."""

from __future__ import annotations

from pathlib import Path


def default_resolve_guide(guide_path: Path | None) -> Path:
    """Resolve the usage-guide markdown file path. Production fallback chain:
    relative to the MCP server module → relative to the installed kairix
    package.
    """
    if guide_path is not None:
        return guide_path
    import kairix.agents.mcp.server as _server_mod

    candidate = Path(_server_mod.__file__).parent.parent.parent / "docs" / "agent-usage-guide.md"
    if candidate.exists():
        return candidate
    import kairix as _kairix

    return Path(_kairix.__file__).parent.parent / "docs" / "agent-usage-guide.md"
