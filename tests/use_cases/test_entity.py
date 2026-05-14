"""Unit tests for ``kairix.use_cases.entity`` — suggest + validate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kairix.use_cases.entity import (
    EntitySuggestDeps,
    EntitySuggestOutput,
    EntityValidateDeps,
    EntityValidateOutput,
    SuggestedEntityHit,
    entity_suggest_output_to_envelope,
    entity_validate_output_to_envelope,
    run_entity_suggest,
    run_entity_validate,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeSuggestion:
    text: str = ""
    label: str = ""
    is_new: bool = False
    existing_id: str | None = None
    existing_name: str | None = None
    context: str = ""


class _FakeNeo4jClient:
    available = True


# ---------------------------------------------------------------------------
# run_entity_suggest
# ---------------------------------------------------------------------------


def test_suggest_projects_each_suggestion_into_hit() -> None:
    raw = [
        _FakeSuggestion(text="Acme", label="ORG", is_new=False, existing_id="acme", existing_name="Acme"),
        _FakeSuggestion(text="Bob", label="PERSON", is_new=True, context="Bob said yes."),
    ]
    deps = EntitySuggestDeps(
        suggest_fn=lambda text, neo4j: raw,
        neo4j_client_fn=lambda: _FakeNeo4jClient(),
    )
    out = run_entity_suggest("Acme and Bob met today.", deps=deps)

    assert out.error == ""
    assert out.text == "Acme and Bob met today."
    assert len(out.suggestions) == 2
    assert out.suggestions[0].text == "Acme"
    assert out.suggestions[0].existing_id == "acme"
    assert out.suggestions[1].is_new is True
    assert out.suggestions[1].context == "Bob said yes."
    assert out.new_count == 1
    assert out.existing_count == 1


def test_suggest_failure_yields_error_envelope() -> None:
    def _boom(text: str, neo4j: Any) -> list:
        raise RuntimeError("ner failed")

    deps = EntitySuggestDeps(suggest_fn=_boom, neo4j_client_fn=lambda: _FakeNeo4jClient())
    out = run_entity_suggest("anything", deps=deps)
    assert out.error.startswith("RuntimeError:")
    assert out.suggestions == []


def test_suggest_import_error_renders_operator_actionable_message() -> None:
    def _missing_spacy(text: str, neo4j: Any) -> list:
        raise ImportError("spacy not installed")

    deps = EntitySuggestDeps(suggest_fn=_missing_spacy, neo4j_client_fn=lambda: _FakeNeo4jClient())
    out = run_entity_suggest("anything", deps=deps)
    assert out.error.startswith("ImportError:")
    assert "kairix[nlp]" in out.error


def test_suggest_none_existing_fields_become_empty_strings() -> None:
    """A SuggestedEntity whose existing_id/name are None must render as ''."""
    raw = [_FakeSuggestion(text="X", label="ORG", is_new=True, existing_id=None, existing_name=None)]
    deps = EntitySuggestDeps(suggest_fn=lambda t, n: raw, neo4j_client_fn=lambda: _FakeNeo4jClient())
    out = run_entity_suggest("X is here", deps=deps)
    assert out.suggestions[0].existing_id == ""
    assert out.suggestions[0].existing_name == ""


def test_suggest_envelope_has_expected_keys() -> None:
    out = EntitySuggestOutput(
        text="t",
        suggestions=[SuggestedEntityHit(text="A", label="ORG", is_new=True)],
        new_count=1,
        existing_count=0,
    )
    env = entity_suggest_output_to_envelope(out)
    assert env["text"] == "t"
    assert env["new_count"] == 1
    assert env["error"] == ""
    assert env["suggestions"][0] == {
        "text": "A",
        "label": "ORG",
        "is_new": True,
        "existing_id": "",
        "existing_name": "",
        "context": "",
    }


# ---------------------------------------------------------------------------
# run_entity_validate
# ---------------------------------------------------------------------------


def test_validate_projects_dict_matches_into_dataclasses() -> None:
    raw = {
        "name": "Acme",
        "neo4j_id": "acme",
        "matches": [
            {
                "qid": "Q1",
                "label": "Acme Inc",
                "description": "Wile E. supplier",
                "url": "http://wiki/Q1",
                "confidence": "high",
            },
        ],
        "updated": False,
        "error": "",
    }
    deps = EntityValidateDeps(
        validate_fn=lambda name, neo4j, update: raw,
        neo4j_client_fn=lambda: _FakeNeo4jClient(),
    )
    out = run_entity_validate("Acme", deps=deps)

    assert out.name == "Acme"
    assert out.neo4j_id == "acme"
    assert len(out.matches) == 1
    assert out.matches[0].qid == "Q1"
    assert out.matches[0].confidence == "high"
    assert out.updated is False
    assert out.error == ""


def test_validate_passes_update_flag_through() -> None:
    captured: dict = {}

    def _fake(name: str, neo4j: Any, update: bool) -> dict:
        captured["update"] = update
        return {"name": name, "matches": [], "updated": update}

    deps = EntityValidateDeps(validate_fn=_fake, neo4j_client_fn=lambda: _FakeNeo4jClient())
    run_entity_validate("X", update=True, deps=deps)
    assert captured["update"] is True


def test_validate_none_neo4j_id_renders_empty_string() -> None:
    deps = EntityValidateDeps(
        validate_fn=lambda n, c, update: {"name": n, "neo4j_id": None, "matches": [], "updated": False},
        neo4j_client_fn=lambda: _FakeNeo4jClient(),
    )
    out = run_entity_validate("X", deps=deps)
    assert out.neo4j_id == ""


def test_validate_failure_yields_error_envelope() -> None:
    def _boom(name: str, neo4j: Any, update: bool) -> dict:
        raise RuntimeError("wikidata down")

    deps = EntityValidateDeps(validate_fn=_boom, neo4j_client_fn=lambda: _FakeNeo4jClient())
    out = run_entity_validate("X", deps=deps)
    assert out.error.startswith("RuntimeError:")


def test_validate_envelope_includes_all_fields() -> None:
    out = EntityValidateOutput(
        name="Acme",
        neo4j_id="acme",
        matches=[],
        updated=True,
    )
    env = entity_validate_output_to_envelope(out)
    assert env["name"] == "Acme"
    assert env["updated"] is True
    assert env["matches"] == []


# ---------------------------------------------------------------------------
# production_suggest — #166: vault-driven override loading is wired in.
# ---------------------------------------------------------------------------


def test_production_suggest_returns_empty_when_neo4j_unavailable(tmp_path) -> None:
    """``production_suggest`` short-circuits to ``[]`` when Neo4j is down.

    Confirms the override-file resolution + chain construction codepath
    is reachable from ``EntitySuggestDeps.suggest_fn``'s default factory.
    The Neo4j-down branch returns before the spaCy import so this works
    in environments that don't have spaCy installed (CI default).

    F2-clean: the override file path is passed via the production
    ``overrides_path`` kwarg, not by monkeypatching the env var.
    """
    from kairix.use_cases.entity import production_suggest

    overrides_file = tmp_path / "_entity-overrides.md"
    overrides_file.write_text('- "YYY": ORG\n', encoding="utf-8")

    class _UnavailableNeo4j:
        available = False

    result = production_suggest("YYY did a thing.", _UnavailableNeo4j(), overrides_path=overrides_file)
    assert result == []


def test_production_suggest_loads_overrides_before_calling_suggest_entities(tmp_path, caplog) -> None:
    """The override file is read on every call: a malformed entry produces
    the expected loader-warning, and the call still returns (no raise).

    Sabotage-prove: this asserts the override-loader is actually invoked
    by ``production_suggest`` (the warning would not appear if the
    loader were short-circuited). If a future refactor stops reading
    the file, the warning disappears and this test flips.
    """
    import logging

    from kairix.use_cases.entity import production_suggest

    overrides_file = tmp_path / "_entity-overrides.md"
    overrides_file.write_text('- this is malformed\n- "RealOrg": ORG\n', encoding="utf-8")

    class _UnavailableNeo4j:
        available = False

    with caplog.at_level(logging.WARNING, logger="kairix.knowledge.entities.overrides"):
        result = production_suggest("text", _UnavailableNeo4j(), overrides_path=overrides_file)

    assert result == []
    assert any("unparseable entry" in rec.message for rec in caplog.records), (
        "expected the override loader to be invoked and to warn on the malformed entry"
    )
