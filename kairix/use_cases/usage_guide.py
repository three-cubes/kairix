"""Usage-guide use case — markdown-section retrieval shared by CLI and MCP.

Phase 3f of the CLI/MCP feature parity initiative (#168). Pre-Phase-3f
``mcp__usage_guide`` was MCP-only — operators couldn't read the agent
usage guide from a shell. This module wraps the existing topic-section
extractor in a use case so both surfaces share the same call shape and
result structure.

The CLI surface also addresses dogfood CONN-2 (deployment-step gap):
operators can now run ``kairix usage-guide`` to onboard themselves
without booting the MCP server.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _default_resolve_guide(guide_path: Path | None) -> Path:
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


@dataclass(frozen=True)
class UsageGuideOutput:
    """Outcome of one ``run_usage_guide`` invocation.

    Attributes:
        topic: The caller's topic filter (empty string returns the full guide).
        content: Markdown content. Full guide when ``topic == ""``;
            otherwise concatenated sections whose headings mention the
            topic. Falls back to a keyword-line search when no heading
            matches; first 2000 chars of the guide when no lines match.
        error: Empty on success; an operator-actionable message when
            the guide file is missing; ``"<Class>: <msg>"`` on
            unexpected failure.
    """

    topic: str = ""
    content: str = ""
    error: str = ""


@dataclass(frozen=True)
class UsageGuideDeps:
    """Injectable dependencies for ``run_usage_guide``.

    Non-Optional field wired to the production resolver via
    ``default_factory`` — eliminates the ``Optional[Callable]`` mypy
    regression class flagged in #204. Tests construct
    ``UsageGuideDeps(resolve_guide_fn=fake)`` with explicit overrides;
    ``UsageGuideDeps()`` resolves to ``_default_resolve_guide``.
    """

    resolve_guide_fn: Callable[[Path | None], Path] = field(default_factory=lambda: _default_resolve_guide)


def extract_topic_sections(full_text: str, topic_lower: str) -> str:
    """Return the concatenated markdown sections whose heading mentions the topic.

    Sections are demarcated by ``##`` / ``###`` headings. Falls back to a
    keyword search across all lines when no heading matches; falls back
    again to the first 2000 chars of the guide when no lines match.

    Public so CLI tests can pin the section-extraction contract directly.
    """
    lines = full_text.splitlines()
    sections: list[str] = []
    current: list[str] = []
    in_section = False

    for line in lines:
        is_heading = line.startswith("## ") or line.startswith("### ")
        if is_heading:
            if in_section and current:
                sections.append("\n".join(current))
                current = []
            in_section = topic_lower in line.lower()
            if in_section:
                current = [line]
        elif in_section:
            current.append(line)

    if in_section and current:
        sections.append("\n".join(current))

    if sections:
        return "\n\n".join(sections)
    matching_lines = [ln for ln in lines if topic_lower in ln.lower()]
    return "\n".join(matching_lines[:30]) if matching_lines else full_text[:2000]


def run_usage_guide(
    topic: str = "",
    *,
    guide_path: Path | None = None,
    deps: UsageGuideDeps | None = None,
) -> UsageGuideOutput:
    """Read the usage guide and optionally filter by topic.

    Never raises — failures populate ``UsageGuideOutput.error``.

    Args:
        topic: Optional topic filter (case-insensitive). Empty string
            returns the full guide.
        guide_path: Explicit path to the guide markdown file. When
            omitted, the use case resolves the production location.
        deps: Injectable dependencies; production callers leave None.
    """
    d = deps or UsageGuideDeps()
    resolve = d.resolve_guide_fn

    try:
        resolved = resolve(guide_path)
        if not resolved.exists():
            return UsageGuideOutput(
                topic=topic,
                error="UsageGuideNotFound: run 'kairix onboard guide --document-root <path>' to install it",
            )

        full_text = resolved.read_text(encoding="utf-8")
        if not topic:
            return UsageGuideOutput(content=full_text)

        return UsageGuideOutput(topic=topic, content=extract_topic_sections(full_text, topic.lower()))
    except Exception as exc:
        logger.warning("run_usage_guide failed: %s", exc, exc_info=True)
        return UsageGuideOutput(topic=topic, error=f"{type(exc).__name__}: {exc}")


def usage_guide_output_to_envelope(out: UsageGuideOutput) -> dict[str, Any]:
    """Project a ``UsageGuideOutput`` to the JSON envelope MCP callers receive."""
    return {
        "topic": out.topic,
        "content": out.content,
        "error": out.error,
    }
