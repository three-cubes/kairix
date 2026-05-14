"""Entity get use case — direct entity-card lookup shared by CLI and MCP.

Phase 3e of the CLI/MCP feature parity initiative (#168). Pre-Phase-3e
``mcp__entity`` was MCP-only; the CLI had no way to look an entity up
by name from the knowledge graph. This module wraps the existing
``_fetch_entity_card`` helper in a use case so both surfaces share
the same call shape and result structure.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _default_fetch_card(name: str, *, neo4j_client: Any | None = None) -> dict[str, Any] | None:
    from kairix.agents.mcp.server import _fetch_entity_card

    return _fetch_entity_card(name, neo4j_client=neo4j_client)


@dataclass(frozen=True)
class EntityGetOutput:
    """Outcome of one ``run_entity_get`` invocation.

    Attributes:
        id: Neo4j node id (slugified). Empty when entity not found.
        name: Canonical entity name. Equals the caller's name when the
            entity was not found in Neo4j.
        type: Neo4j label (``Person``, ``Organisation``, ``Project``, …).
        summary: Human-readable summary built from type-specific fields
            (role, org, tier, industry, …). Empty when no fields were
            populated.
        vault_path: On-disk path to the entity's markdown file in the
            vault. Empty when the entity has no associated file.
        error: Empty on success; ``"EntityNotFound: <name>"`` when
            the lookup returned no rows; structured ``"<Class>: <msg>"``
            on top-level failure (#165: every error string is
            class-prefixed so agents can switch on the prefix).
    """

    id: str = ""
    name: str = ""
    type: str = ""
    summary: str = ""
    vault_path: str = ""
    error: str = ""


@dataclass(frozen=True)
class EntityGetDeps:
    """Injectable dependencies for ``run_entity_get``.

    Mirrors ``WorkerDeps`` (kairix/worker.py): ``fetch_fn`` is
    non-Optional with a ``default_factory`` returning the production
    helper. Tests pass ``EntityGetDeps(fetch_fn=fake)``; production
    callers leave ``deps=None``.
    """

    fetch_fn: Callable[..., dict[str, Any] | None] = field(default_factory=lambda: _default_fetch_card)


def run_entity_get(
    name: str,
    *,
    deps: EntityGetDeps | None = None,
) -> EntityGetOutput:
    """Look up an entity by name and return a structured result.

    Never raises — failures populate ``EntityGetOutput.error``.

    Args:
        name: Entity name to look up (case-insensitive against canonical name + slug).
        deps: Injectable dependencies; production callers leave None.
    """
    d = deps or EntityGetDeps()

    try:
        card = d.fetch_fn(name)
    except Exception as exc:
        logger.warning("run_entity_get failed: %s", exc, exc_info=True)
        return EntityGetOutput(name=name, error=f"{type(exc).__name__}: {exc}")

    if card is None:
        return EntityGetOutput(name=name, error=f"EntityNotFound: {name}")

    return EntityGetOutput(
        id=str(card.get("id", "") or ""),
        name=str(card.get("name", "") or ""),
        type=str(card.get("type", "") or ""),
        summary=str(card.get("summary", "") or ""),
        vault_path=str(card.get("vault_path", "") or ""),
    )


def entity_get_output_to_envelope(out: EntityGetOutput) -> dict[str, Any]:
    """Project an ``EntityGetOutput`` to the JSON envelope MCP callers receive."""
    return {
        "id": out.id,
        "name": out.name,
        "type": out.type,
        "summary": out.summary,
        "vault_path": out.vault_path,
        "error": out.error,
    }
