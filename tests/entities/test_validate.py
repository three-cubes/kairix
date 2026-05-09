"""Tests for kairix.knowledge.entities.validate — Wikidata validator."""

from typing import Any

import pytest

from kairix.knowledge.entities.validate import (
    WikidataMatch,
    search_wikidata,
    validate_entity,
)
from tests.fixtures.neo4j_mock import FakeNeo4jClient


class _FakeResponse:
    """Minimal requests.Response stand-in: ``raise_for_status`` is a no-op,
    ``json()`` returns the wrapped Wikidata payload."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"search": self._items}


def _fake_http_get(items: list[dict]):
    """Return a callable that mimics requests.get returning Wikidata items."""

    def _get(*args: Any, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(items)

    return _get


def _failing_http_get(exc):
    """Return a callable that raises the given exception."""

    def _get(*args, **kwargs):
        raise exc

    return _get


@pytest.mark.unit
def test_search_wikidata_returns_empty_on_network_error():
    result = search_wikidata("OpenClaw", http_get=_failing_http_get(ConnectionError("timeout")))
    assert result == []


@pytest.mark.unit
def test_search_wikidata_parses_results():
    fake_items = [
        {"id": "Q123", "label": "OpenClaw", "description": "AI agent platform"},
        {"id": "Q456", "label": "Open Claw Tool", "description": "A hardware tool"},
    ]
    results = search_wikidata("OpenClaw", http_get=_fake_http_get(fake_items))
    assert len(results) == 2
    assert results[0].qid == "Q123"
    assert results[0].confidence == "high"  # exact label match
    assert results[1].confidence in ("medium", "low")


@pytest.mark.unit
def test_confidence_high_on_exact_match():
    fake_items = [{"id": "Q1", "label": "ACME", "description": "Example company"}]
    results = search_wikidata("ACME", http_get=_fake_http_get(fake_items))
    assert results[0].confidence == "high"


@pytest.mark.unit
def test_validate_entity_no_neo4j_match():
    neo4j = FakeNeo4jClient(entities=[])
    fake_items = [{"id": "Q999", "label": "Unknown", "description": "Unknown entity"}]
    result = validate_entity("Unknown", neo4j, http_get=_fake_http_get(fake_items))
    assert result["neo4j_id"] is None
    assert len(result["matches"]) == 1
    assert result["updated"] is False


@pytest.mark.unit
def test_validate_entity_with_neo4j_match():
    neo4j = FakeNeo4jClient()  # has OpenClaw in default entities
    fake_items = [{"id": "Q100", "label": "OpenClaw", "description": "AI platform"}]
    result = validate_entity("OpenClaw", neo4j, http_get=_fake_http_get(fake_items))
    assert result["neo4j_id"] == "openclaw"
    assert result["matches"][0]["qid"] == "Q100"


@pytest.mark.unit
def test_validate_entity_update_writes_qid():
    neo4j = FakeNeo4jClient()
    fake_items = [{"id": "Q100", "label": "OpenClaw", "description": "AI platform"}]
    result = validate_entity("OpenClaw", neo4j, update=True, http_get=_fake_http_get(fake_items))
    assert result["updated"] is True


@pytest.mark.unit
def test_validate_entity_never_raises_on_api_failure():
    neo4j = FakeNeo4jClient()
    result = validate_entity("OpenClaw", neo4j, http_get=_failing_http_get(Exception("connection refused")))
    assert result["matches"] == []
    assert result["error"] == ""


@pytest.mark.contract
def test_wikidata_match_has_required_fields():
    m = WikidataMatch(
        qid="Q1",
        label="Test",
        description="Desc",
        url="https://wikidata.org/wiki/Q1",
        confidence="high",
    )
    assert hasattr(m, "qid")
    assert hasattr(m, "label")
    assert hasattr(m, "confidence")
    assert m.confidence in ("high", "medium", "low")
