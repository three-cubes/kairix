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

from kairix.core.health import (
    HealthDeps,
    KairixHealth,
    entity_next_action,
    health_to_envelope,
    probe_health,
)

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
    health: KairixHealth = field(default_factory=KairixHealth)
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
    health_deps: HealthDeps = field(default_factory=HealthDeps)


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
    base_health = probe_health(d.health_deps)
    neo4j_available = _safe_bool(d.health_deps.neo4j_available_fn)
    health = _entity_health(base_health, neo4j_available=neo4j_available)

    try:
        card = d.fetch_fn(name)
    except Exception as exc:
        logger.warning("run_entity_get failed: %s", exc, exc_info=True)
        return EntityGetOutput(name=name, health=health, error=f"{type(exc).__name__}: {exc}")

    if card is None:
        # No row from Neo4j. When the graph is offline that's the *cause*;
        # next_action redirects the agent at tool_search. Either way the
        # caller gets a useful envelope, not a silent empty.
        return EntityGetOutput(name=name, health=health, error=f"EntityNotFound: {name}")

    return EntityGetOutput(
        id=str(card.get("id", "") or ""),
        name=str(card.get("name", "") or ""),
        type=str(card.get("type", "") or ""),
        summary=str(card.get("summary", "") or ""),
        vault_path=str(card.get("vault_path", "") or ""),
        health=health,
    )


def _safe_bool(fn: Callable[[], bool]) -> bool:
    """Call a probe, swallowing failures into ``False``."""
    try:
        return bool(fn())
    except Exception as exc:
        logger.warning("entity_get neo4j probe failed: %s", exc, exc_info=True)
        return False


def _entity_health(base: KairixHealth, *, neo4j_available: bool) -> KairixHealth:
    """Overlay the entity-specific ``next_action`` + degraded_reason.

    When Neo4j is offline the shared probe is silent about the graph
    (it doesn't appear on ``KairixHealth``); this overlay surfaces the
    cause and the prescriptive fallback to ``tool_search``.
    """
    directive = entity_next_action(base, neo4j_available=neo4j_available)
    if not neo4j_available:
        reason = base.degraded_reason
        graph_reason = "Knowledge graph offline"
        if reason and graph_reason not in reason:
            reason = f"{reason}; {graph_reason}"
        else:
            reason = graph_reason
        return KairixHealth(
            vector_search=base.vector_search,
            bm25=base.bm25,
            chat=base.chat,
            secrets_loaded=base.secrets_loaded,
            degraded_reason=reason,
            next_action=directive,
        )
    if not directive:
        return base
    return KairixHealth(
        vector_search=base.vector_search,
        bm25=base.bm25,
        chat=base.chat,
        secrets_loaded=base.secrets_loaded,
        degraded_reason=base.degraded_reason,
        next_action=directive,
    )


def entity_get_output_to_envelope(out: EntityGetOutput) -> dict[str, Any]:
    """Project an ``EntityGetOutput`` to the JSON envelope MCP callers receive."""
    return {
        "id": out.id,
        "name": out.name,
        "type": out.type,
        "summary": out.summary,
        "vault_path": out.vault_path,
        "health": dict(health_to_envelope(out.health)),
        "error": out.error,
    }
