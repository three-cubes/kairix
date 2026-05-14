"""Unit tests for ``kairix.use_cases.entity_get.run_entity_get``."""

from __future__ import annotations

from typing import Any

import pytest

from kairix.core.health import HealthDeps, KairixHealth
from kairix.use_cases.entity_get import (
    EntityGetDeps,
    EntityGetOutput,
    entity_get_output_to_envelope,
    run_entity_get,
)

pytestmark = pytest.mark.unit


def _healthy_health_deps() -> HealthDeps:
    return HealthDeps(
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
        neo4j_available_fn=lambda: True,
    )


def _neo4j_offline_health_deps() -> HealthDeps:
    return HealthDeps(
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
        neo4j_available_fn=lambda: False,
    )


def _build_deps(
    card: dict[str, Any] | None = None,
    raises: bool = False,
    health_deps: HealthDeps | None = None,
) -> EntityGetDeps:
    def _fetch(name: str) -> dict[str, Any] | None:
        if raises:
            raise RuntimeError("Neo4j down")
        return card

    return EntityGetDeps(fetch_fn=_fetch, health_deps=health_deps or _healthy_health_deps())


def test_card_present_projects_into_output() -> None:
    card = {
        "id": "acme",
        "name": "Acme",
        "type": "Organisation",
        "summary": "supplier — Tier A",
        "vault_path": "02-Areas/00-Clients/Acme/Acme.md",
    }
    out = run_entity_get("Acme", deps=_build_deps(card=card))
    assert out.error == ""
    assert out.id == "acme"
    assert out.name == "Acme"
    assert out.type == "Organisation"
    assert out.summary == "supplier — Tier A"
    assert out.vault_path == "02-Areas/00-Clients/Acme/Acme.md"


def test_card_none_returns_not_found_error() -> None:
    out = run_entity_get("Bogus", deps=_build_deps(card=None))
    assert out.error == "EntityNotFound: Bogus"
    assert out.id == ""
    # Name preserved from the caller for the operator's error message.
    assert out.name == "Bogus"


def test_none_summary_renders_empty_string() -> None:
    card = {"id": "x", "name": "X", "type": "Project", "summary": None, "vault_path": None}
    out = run_entity_get("X", deps=_build_deps(card=card))
    assert out.summary == ""
    assert out.vault_path == ""


def test_fetch_failure_yields_error_envelope() -> None:
    out = run_entity_get("X", deps=_build_deps(raises=True))
    assert out.error.startswith("RuntimeError:")
    assert out.id == ""


def test_envelope_includes_all_fields() -> None:
    out = EntityGetOutput(id="a", name="A", type="Person", summary="role", vault_path="/p")
    env = entity_get_output_to_envelope(out)
    assert env["id"] == "a"
    assert env["name"] == "A"
    assert env["type"] == "Person"
    assert env["summary"] == "role"
    assert env["vault_path"] == "/p"
    assert env["error"] == ""
    # Health envelope carries the snapshot's projected dict.
    assert "vector_search" in env["health"]
    assert "next_action" in env["health"]


# ---------------------------------------------------------------------------
# W3: health envelope contract (#246)
# ---------------------------------------------------------------------------


def test_healthy_state_entity_carries_clean_health_field() -> None:
    card = {"id": "a", "name": "A", "type": "Person", "summary": "role", "vault_path": "/p"}
    out = run_entity_get("A", deps=_build_deps(card=card))
    assert out.health.vector_search == "ok"
    assert out.health.degraded_reason == ""
    assert out.health.next_action == ""


def test_neo4j_offline_returns_prescriptive_next_action_pointing_to_search() -> None:
    """W3 contract: when Neo4j is offline the entity lookup still returns
    an envelope; the directive tells the agent to fall back to tool_search.

    Sabotage anchor: dropping the directive in ``entity_next_action``
    makes this test fail on the ``next_action`` assertion."""
    deps = _build_deps(card=None, health_deps=_neo4j_offline_health_deps())
    out = run_entity_get("Bogus", deps=deps)

    # Entity is empty (fetch returned None) but the affordance is alive.
    assert out.id == ""
    assert out.name == "Bogus"
    # Health surfaces the offline graph.
    assert "Knowledge graph offline" in out.health.degraded_reason
    # Prescriptive directive points at tool_search.
    assert out.health.next_action != ""
    assert "tool_search" in out.health.next_action
    assert "vault references" in out.health.next_action


def test_neo4j_offline_with_a_card_match_still_overlays_directive() -> None:
    """Even when the fetch_fn returns a card (mocked), the directive
    must still appear because the graph itself is reported offline."""
    card = {"id": "a", "name": "A", "type": "Person", "summary": "role", "vault_path": "/p"}
    deps = _build_deps(card=card, health_deps=_neo4j_offline_health_deps())
    out = run_entity_get("A", deps=deps)
    assert out.name == "A"
    assert "Knowledge graph offline" in out.health.degraded_reason
    assert "tool_search" in out.health.next_action


def test_entity_envelope_includes_health_dict() -> None:
    out = EntityGetOutput(name="A", health=KairixHealth())
    env = entity_get_output_to_envelope(out)
    assert "health" in env
    assert env["health"]["vector_search"] == "ok"


def test_every_degraded_entity_response_carries_a_next_action() -> None:
    """Sabotage anchor: removing the directive in ``entity_next_action``
    or ``_entity_health`` breaks this iteration."""
    for secrets, embed, bm25, neo4j in (
        (True, True, True, False),  # only neo4j offline
        (False, True, True, True),  # only secrets offline
        (True, False, True, True),  # only embed offline
        (False, False, False, False),  # everything offline
    ):
        hd = HealthDeps(
            secrets_loaded_fn=lambda s=secrets: s,
            embed_backend_available_fn=lambda e=embed: e,
            bm25_index_available_fn=lambda b=bm25: b,
            neo4j_available_fn=lambda n=neo4j: n,
        )
        deps = EntityGetDeps(fetch_fn=lambda _name: None, health_deps=hd)
        out = run_entity_get("X", deps=deps)
        assert out.health.next_action != "", (
            f"entity envelope dropped next_action for secrets={secrets} embed={embed} bm25={bm25} neo4j={neo4j}"
        )
