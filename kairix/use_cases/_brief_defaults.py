"""Production wiring for ``run_brief``."""

from __future__ import annotations

from typing import Any


def default_generate(agent: str, **kwargs: Any) -> str:
    from kairix.agents.briefing.pipeline import generate_briefing

    return generate_briefing(agent, **kwargs)


def default_briefing_dir() -> Any:
    from kairix.agents.briefing.writer import BRIEFING_DIR

    return BRIEFING_DIR
