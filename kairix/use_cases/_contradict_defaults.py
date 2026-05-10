"""Production wiring for ``run_contradict``."""

from __future__ import annotations

from typing import Any


def default_check_contradiction(**kwargs: Any) -> list[Any]:
    from kairix.knowledge.contradict.detector import check_contradiction

    return check_contradiction(**kwargs)


def default_llm_backend() -> Any:
    from kairix.platform.llm import get_default_backend

    return get_default_backend()
