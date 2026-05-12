"""Unit tests for ``kairix.use_cases.entity_get.run_entity_get``."""

from __future__ import annotations

from typing import Any

import pytest

from kairix.use_cases.entity_get import (
    EntityGetDeps,
    EntityGetOutput,
    entity_get_output_to_envelope,
    run_entity_get,
)

pytestmark = pytest.mark.unit


def _build_deps(card: dict[str, Any] | None = None, raises: bool = False) -> EntityGetDeps:
    def _fetch(name: str) -> dict[str, Any] | None:
        if raises:
            raise RuntimeError("Neo4j down")
        return card

    return EntityGetDeps(fetch_fn=_fetch)


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
    assert env == {
        "id": "a",
        "name": "A",
        "type": "Person",
        "summary": "role",
        "vault_path": "/p",
        "error": "",
    }
