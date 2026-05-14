"""Tests for kairix.knowledge.entities.suggest — NER entity suggestions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kairix.knowledge.entities.suggest import (
    SuggestedEntity,
    format_suggestions,
    suggest_entities,
)
from tests.fixtures.neo4j_mock import FakeNeo4jClient


def _make_mock_spacy(entities: list[tuple[str, str]]):
    """Build a mock spaCy nlp pipeline that returns fixed entities."""
    mock_nlp = MagicMock()
    mock_doc = MagicMock()

    mock_ents = []
    for text, label in entities:
        ent = MagicMock()
        ent.text = text
        ent.label_ = label
        mock_ents.append(ent)

    mock_sent = MagicMock()
    mock_sent.ents = mock_ents
    mock_sent.text = "Test sentence with entities."
    mock_doc.sents = [mock_sent]
    mock_nlp.return_value = mock_doc
    return mock_nlp


@pytest.mark.unit
def test_suggest_entities_new_entity():
    """Entities not in Neo4j should be marked as new.

    F1-clean: pass nlp= directly through the existing constructor seam
    instead of @patch'ing _load_model + spacy + sys.modules. The previous
    triple-patch was a smell that obscured what the test actually proved.
    """
    neo4j = FakeNeo4jClient(entities=[])  # empty graph
    mock_nlp = _make_mock_spacy([("AcmeCorp", "ORG")])

    result = suggest_entities("AcmeCorp is a new company.", neo4j, nlp=mock_nlp)

    # Sabotage-prove: assert the new entity is flagged as new, not just
    # that the call returned. With FakeNeo4jClient.entities=[] any
    # extracted entity must be is_new=True.
    new_acme = [s for s in result if s.text == "AcmeCorp"]
    assert new_acme, f"expected AcmeCorp in suggestions; got {[s.text for s in result]}"
    assert new_acme[0].is_new is True
    assert new_acme[0].existing_id is None


@pytest.mark.unit
def test_suggest_returns_empty_when_neo4j_unavailable():
    """Should return [] gracefully when Neo4j is unavailable."""

    class UnavailableNeo4j:
        available = False

    result = suggest_entities("Some text", UnavailableNeo4j())
    assert result == []


@pytest.mark.unit
def test_suggest_graceful_import_error():
    """Should raise ImportError with install instructions when spaCy not installed."""
    neo4j = FakeNeo4jClient()
    import sys

    # Remove spacy from sys.modules to simulate it not being installed
    sys_modules_backup = sys.modules.copy()
    sys.modules.pop("spacy", None)
    sys.modules["spacy"] = None  # type: ignore  # simulating uninstalled package; None forces ImportError

    try:
        with pytest.raises(ImportError, match="pip install"):
            suggest_entities("test text", neo4j)
    finally:
        # Restore
        if "spacy" in sys_modules_backup:
            sys.modules["spacy"] = sys_modules_backup["spacy"]
        else:
            sys.modules.pop("spacy", None)


@pytest.mark.unit
def test_format_suggestions_empty():
    result = format_suggestions([])
    assert "No entity suggestions" in result


@pytest.mark.unit
def test_format_suggestions_table():
    suggestions = [
        SuggestedEntity(
            text="OpenClaw",
            label="ORG",
            existing_id="openclaw",
            existing_name="OpenClaw",
            is_new=False,
            context="OpenClaw is an AI platform.",
        ),
        SuggestedEntity(
            text="NewCorp",
            label="ORG",
            existing_id=None,
            existing_name=None,
            is_new=True,
            context="NewCorp was founded in 2025.",
        ),
    ]
    result = format_suggestions(suggestions, fmt="table")
    assert "OpenClaw" in result
    assert "NewCorp" in result
    assert "existing" in result
    assert "NEW" in result


@pytest.mark.unit
def test_format_suggestions_jsonl():
    import json

    suggestions = [
        SuggestedEntity(
            text="OpenClaw",
            label="ORG",
            existing_id="openclaw",
            existing_name="OpenClaw",
            is_new=False,
        ),
    ]
    result = format_suggestions(suggestions, fmt="jsonl")
    parsed = json.loads(result.strip())
    assert parsed["text"] == "OpenClaw"
    assert parsed["is_new"] is False


@pytest.mark.contract
def test_suggested_entity_is_new_flag():
    """is_new must be True when entity not in graph, False when found."""
    new_entity = SuggestedEntity(text="NewCorp", label="ORG", existing_id=None, existing_name=None, is_new=True)
    existing_entity = SuggestedEntity(
        text="OpenClaw",
        label="ORG",
        existing_id="openclaw",
        existing_name="OpenClaw",
        is_new=False,
    )
    assert new_entity.is_new is True
    assert existing_entity.is_new is False


@pytest.mark.unit
def test_suggest_entities_existing_entity_marked_not_new():
    """Entities found in Neo4j surface with is_new=False and the existing id/name."""
    # FakeNeo4jClient.find_by_name returns matching entity by name
    neo4j = FakeNeo4jClient(entities=[{"id": "openclaw-id", "name": "OpenClaw"}])
    mock_nlp = _make_mock_spacy([("OpenClaw", "ORG")])

    result = suggest_entities("OpenClaw is an AI platform.", neo4j, nlp=mock_nlp)

    matches = [s for s in result if s.text == "OpenClaw"]
    assert matches, f"expected OpenClaw in suggestions; got {[s.text for s in result]}"
    assert matches[0].is_new is False
    assert matches[0].existing_id == "openclaw-id"
    assert matches[0].existing_name == "OpenClaw"


@pytest.mark.unit
def test_suggest_entities_handles_nlp_processing_failure():
    """When nlp(text) raises, suggest_entities logs a warning and returns []."""

    class _ExplodingNLP:
        def __call__(self, text):
            raise RuntimeError("nlp pipeline crashed")

    neo4j = FakeNeo4jClient(entities=[])
    result = suggest_entities("any text", neo4j, nlp=_ExplodingNLP())
    assert result == []


@pytest.mark.unit
def test_suggest_entities_handles_neo4j_lookup_failure():
    """Neo4j lookup failures are logged and the entity surfaces as new."""

    class _FailingNeo4j:
        available = True

        def find_by_name(self, name):
            raise RuntimeError("graph unreachable")

    mock_nlp = _make_mock_spacy([("Acme", "ORG")])
    result = suggest_entities("Acme is a company.", _FailingNeo4j(), nlp=mock_nlp)
    matches = [s for s in result if s.text == "Acme"]
    assert matches
    assert matches[0].is_new is True
    assert matches[0].existing_id is None


@pytest.mark.unit
def test_suggest_entities_drops_empty_surface_form():
    """Filter chain entries with empty 'text' are skipped (line 116)."""

    class _ChainEmittingEmpty:
        def apply(self, suggestions, context):
            return [{"text": "", "label": "ORG"}, {"text": "RealCorp", "label": "ORG"}]

    neo4j = FakeNeo4jClient(entities=[])
    mock_nlp = _make_mock_spacy([("AcmeCorp", "ORG")])
    result = suggest_entities("test", neo4j, filter_chain=_ChainEmittingEmpty(), nlp=mock_nlp)

    surface_forms = {s.text for s in result}
    assert "" not in surface_forms
    assert "RealCorp" in surface_forms


@pytest.mark.unit
def test_suggest_entities_load_model_failure_returns_empty():
    """When spaCy is importable but _load_model fails, suggest_entities returns []
    (covers the `except Exception` branch around _load_model and the _load_model
    body itself).

    We install a fake `spacy` module whose `load` raises OSError. The kairix
    code path catches that as 'spaCy load failed' and short-circuits with [].
    No @patch on kairix internals — we only place a fake module into sys.modules,
    same pattern used by test_suggest_graceful_import_error above.
    """
    import sys
    import types

    fake_spacy = types.ModuleType("spacy")

    def _raise_oserror(name):
        raise OSError("model not found")

    fake_spacy.load = _raise_oserror  # type: ignore[attr-defined]  # injecting load attr on fake spacy module

    sys_modules_backup = sys.modules.get("spacy")
    sys.modules["spacy"] = fake_spacy

    try:
        neo4j = FakeNeo4jClient(entities=[])
        result = suggest_entities("Acme is a company.", neo4j)
        # _load_model raised RuntimeError (wrapped from OSError); kairix logged
        # and returned [].
        assert result == []
    finally:
        if sys_modules_backup is not None:
            sys.modules["spacy"] = sys_modules_backup
        else:
            sys.modules.pop("spacy", None)
