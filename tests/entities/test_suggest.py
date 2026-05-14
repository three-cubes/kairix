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
