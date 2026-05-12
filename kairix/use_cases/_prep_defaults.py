"""Production wiring for ``run_prep``."""

from __future__ import annotations

from typing import Any


def default_search(**kwargs: Any) -> Any:
    from kairix.core.factory import build_search_pipeline

    pipeline = build_search_pipeline()
    return pipeline.search(**kwargs)


def default_chat(**kwargs: Any) -> str:
    from kairix._azure import chat_completion

    return chat_completion(**kwargs)
