"""Production wiring for the entity use cases."""

from __future__ import annotations

from typing import Any


def default_neo4j_client() -> Any:
    from kairix.knowledge.graph.client import get_client

    return get_client()


def default_suggest(text: str, neo4j_client: Any) -> list[Any]:
    from kairix.knowledge.entities.suggest import suggest_entities

    return suggest_entities(text, neo4j_client)


def default_validate(name: str, neo4j_client: Any, update: bool) -> dict[str, Any]:
    from kairix.knowledge.entities.validate import validate_entity

    return validate_entity(name, neo4j_client, update=update)
