"""Production wiring for ``run_entity_get``."""

from __future__ import annotations

from typing import Any


def default_fetch_card(name: str, *, neo4j_client: Any | None = None) -> dict[str, Any] | None:
    from kairix.agents.mcp.server import _fetch_entity_card

    return _fetch_entity_card(name, neo4j_client=neo4j_client)
