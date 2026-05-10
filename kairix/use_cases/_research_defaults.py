"""Production wiring for ``run_research_use_case``."""

from __future__ import annotations

from typing import Any


def default_research(**kwargs: Any) -> dict[str, Any]:
    """Lazy-load the LangGraph research orchestrator."""
    from kairix.agents.research.graph import run_research

    return run_research(**kwargs)
