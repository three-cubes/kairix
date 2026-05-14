"""Research use case — iterative LangGraph research shared by CLI and MCP.

Phase 3d of the CLI/MCP feature parity initiative (#168). Pre-Phase-3d
research was MCP-only. Operators couldn't reproduce or debug an
agent's research synthesis from a shell. This module wraps the
existing ``run_research`` orchestrator so both surfaces share the
same call shape and result structure.

The use case is named ``run_research_use_case`` to avoid colliding with
``kairix.agents.research.graph.run_research``; both adapters import
the use-case version, which delegates to the orchestrator.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


_MAX_TURNS_FLOOR = 1
_MAX_TURNS_CEILING = 10


def _default_research(**kwargs: Any) -> dict[str, Any]:
    """Lazy-load the LangGraph research orchestrator."""
    from kairix.agents.research.graph import run_research

    return run_research(**kwargs)


@dataclass(frozen=True)
class ResearchOutput:
    """Outcome of one ``run_research_use_case`` invocation.

    Attributes:
        query: The caller's query, unchanged.
        synthesis: LLM-synthesised answer drawn from retrieved evidence.
        retrieved_chunks: Up to 10 chunks the orchestrator considered.
        gaps: List of unresolved sub-questions or missing facts.
        confidence: 0.0-1.0 self-assessed confidence in the synthesis.
        turns: Number of search/refine cycles consumed.
        error: Empty on success; structured ``"<Class>: <msg>"`` on top-
            level failure.
    """

    query: str
    synthesis: str = ""
    retrieved_chunks: list[Any] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    confidence: float = 0.0
    turns: int = 0
    error: str = ""


@dataclass(frozen=True)
class ResearchDeps:
    """Injectable dependencies for ``run_research_use_case``.

    Non-Optional field wired to the production orchestrator via
    ``default_factory`` — eliminates the ``Optional[Callable]`` mypy
    regression class flagged in #204. Tests construct
    ``ResearchDeps(research_fn=fake)`` with explicit overrides;
    ``ResearchDeps()`` with no kwargs resolves to ``_default_research``.
    """

    research_fn: Callable[..., dict[str, Any]] = field(default_factory=lambda: _default_research)


def run_research_use_case(
    query: str,
    *,
    max_turns: int = 4,
    deps: ResearchDeps | None = None,
) -> ResearchOutput:
    """Run iterative research and return a structured result.

    Never raises — failures populate ``ResearchOutput.error``.

    Args:
        query: The question to research.
        max_turns: Cap on search/refine cycles (clamped to [1, 10] to
            prevent unbounded LLM amplification — same contract as the
            pre-refactor ``tool_research``).
        deps: Injectable dependencies; production callers leave None.
    """
    clamped = min(max(_MAX_TURNS_FLOOR, max_turns), _MAX_TURNS_CEILING)
    d = deps or ResearchDeps()
    research = d.research_fn

    try:
        result = research(query=query, max_turns=clamped)
        return ResearchOutput(
            query=str(result.get("query", query)),
            synthesis=str(result.get("synthesis", "") or ""),
            retrieved_chunks=list(result.get("retrieved_chunks", []) or [])[:10],
            gaps=list(result.get("gaps", []) or []),
            confidence=float(result.get("confidence", 0.0) or 0.0),
            turns=int(result.get("turns", 0) or 0),
            error=str(result.get("error", "") or ""),
        )
    except Exception as exc:
        logger.warning("run_research_use_case failed: %s", exc, exc_info=True)
        return ResearchOutput(query=query, error=f"{type(exc).__name__}: {exc}")


def research_output_to_envelope(out: ResearchOutput) -> dict[str, Any]:
    """Project a ``ResearchOutput`` to the JSON envelope MCP callers receive."""
    return {
        "query": out.query,
        "synthesis": out.synthesis,
        "retrieved_chunks": out.retrieved_chunks,
        "gaps": out.gaps,
        "confidence": out.confidence,
        "turns": out.turns,
        "error": out.error,
    }
