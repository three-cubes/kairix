"""Production wiring for ``run_search``.

Lazy imports keep ``kairix/use_cases/search.py`` import-light and
fully unit-coverable through ``SearchDeps`` injection.
"""

from __future__ import annotations

from typing import Any

from kairix.core.search.scope import Scope


def default_search(
    query: str,
    budget: int,
    scope: Scope,
    agent: str | None,
) -> Any:
    from kairix.core.factory import build_search_pipeline
    from kairix.core.search.config_loader import load_config

    pipeline = build_search_pipeline(config=load_config())
    return pipeline.search(query=query, budget=budget, scope=scope, agent=agent)


def default_entity_card(name: str) -> dict[str, Any] | None:
    from kairix.agents.mcp.server import _fetch_entity_card

    return _fetch_entity_card(name)


def default_classify(query: str) -> Any:
    from kairix.core.search.intent import classify

    return classify(query)
