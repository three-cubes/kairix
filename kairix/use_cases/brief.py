"""Brief use case — session briefing generation shared by CLI and MCP.

Phase 3a of the CLI/MCP feature parity initiative (#168). Pre-Phase-3a
``kairix brief`` was CLI-only — agents had to shell out via subprocess
to read their own briefing. This use case wraps the existing
``generate_briefing`` pipeline and surfaces both the content and the
on-disk path through a uniform dataclass; the new MCP tool
``tool_brief`` calls it directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_VALID_AGENTS = {"builder", "shape", "growth", "consultant"}


def _default_generate(agent: str, **kwargs: Any) -> str:
    from kairix.agents.briefing.pipeline import generate_briefing

    return generate_briefing(agent, **kwargs)


def _default_briefing_dir() -> Path:
    from kairix.agents.briefing.writer import BRIEFING_DIR

    return BRIEFING_DIR


@dataclass(frozen=True)
class BriefOutput:
    """Outcome of one ``run_brief`` invocation.

    Attributes:
        agent: The agent name used to generate the briefing.
        content: Full briefing markdown (header + body). Empty when
            ``error`` is set.
        path: On-disk path of the briefing file (may be empty if the
            writer step was skipped or failed). The CLI prints this
            for operators; agents prefer ``content``.
        preview: First 30 lines of ``content``, useful for stdout
            previews without re-splitting.
        error: Empty string on success; structured ``"<Class>: <msg>"``
            on top-level failure, or ``invalid agent: <name>`` when
            the agent name is unknown.
    """

    agent: str
    content: str = ""
    path: str = ""
    preview: str = ""
    error: str = ""


@dataclass(frozen=True)
class BriefDeps:
    """Injectable dependencies for ``run_brief``.

    Mirrors ``WorkerDeps`` (kairix/worker.py): each callable is
    non-Optional with a ``default_factory`` returning the production
    helper. Tests construct ``BriefDeps(generate_fn=fake, ...)``;
    production callers leave ``deps=None`` and the run_brief default
    factory wires the real helpers.
    """

    generate_fn: Callable[..., str] = field(default_factory=lambda: _default_generate)
    briefing_dir_fn: Callable[[], Path] = field(default_factory=lambda: _default_briefing_dir)


def run_brief(
    agent: str,
    *,
    deps: BriefDeps | None = None,
) -> BriefOutput:
    """Generate a session briefing and return a structured result.

    Never raises — failures populate ``BriefOutput.error``.

    Args:
        agent: Agent name (builder / shape / growth / consultant).
        deps: Injectable dependencies; production callers leave None.
    """
    normalised = (agent or "").lower().strip()
    if normalised not in _VALID_AGENTS:
        return BriefOutput(
            agent=agent,
            error=f"InvalidAgent: {agent!r}. Must be one of: {sorted(_VALID_AGENTS)}",
        )

    d = deps or BriefDeps()

    try:
        content = d.generate_fn(normalised)
        out_dir = d.briefing_dir_fn()
        path = str(out_dir / f"{normalised}-latest.md") if out_dir else ""
        preview = "\n".join(content.splitlines()[:30])
        return BriefOutput(
            agent=normalised,
            content=content,
            path=path,
            preview=preview,
        )
    except Exception as exc:
        logger.warning("run_brief failed: %s", exc, exc_info=True)
        return BriefOutput(
            agent=normalised,
            error=f"{type(exc).__name__}: {exc}",
        )


def brief_output_to_envelope(out: BriefOutput) -> dict[str, Any]:
    """Project a ``BriefOutput`` to the JSON envelope MCP callers receive."""
    return {
        "agent": out.agent,
        "content": out.content,
        "path": out.path,
        "preview": out.preview,
        "error": out.error,
    }
