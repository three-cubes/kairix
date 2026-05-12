"""Adapter-shell tests for tool_entity_suggest + tool_entity_validate.

The use case bodies are covered in tests/use_cases/test_entity.py;
these tests drive the adapter shells through their typed-deps
forwarders so the projection helpers run end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kairix.agents.mcp.server import tool_entity_suggest, tool_entity_validate
from kairix.use_cases.entity import EntitySuggestDeps, EntityValidateDeps

pytestmark = pytest.mark.unit


@dataclass
class _FakeSuggestion:
    text: str = "Acme"
    label: str = "ORG"
    is_new: bool = True
    existing_id: str | None = None
    existing_name: str | None = None
    context: str = ""


class _FakeNeo4j:
    available = True


def test_tool_entity_suggest_returns_envelope_dict() -> None:
    deps = EntitySuggestDeps(
        suggest_fn=lambda text, neo4j: [_FakeSuggestion()],
        neo4j_client_fn=lambda: _FakeNeo4j(),
    )
    result = tool_entity_suggest(text="Acme is a client.", deps=deps)
    assert result["text"] == "Acme is a client."
    assert result["new_count"] == 1
    assert result["error"] == ""
    assert result["suggestions"][0]["text"] == "Acme"


def test_tool_entity_suggest_failure_returns_error_envelope() -> None:
    def _boom(text: str, neo4j: Any) -> list:
        raise RuntimeError("ner crashed")

    deps = EntitySuggestDeps(suggest_fn=_boom, neo4j_client_fn=lambda: _FakeNeo4j())
    result = tool_entity_suggest(text="x", deps=deps)
    assert result["error"].startswith("RuntimeError")
    assert result["suggestions"] == []


def test_tool_entity_validate_returns_envelope_dict() -> None:
    raw = {
        "name": "Acme",
        "neo4j_id": "acme",
        "matches": [
            {"qid": "Q1", "label": "Acme Inc", "description": "supplier", "url": "u", "confidence": "high"},
        ],
        "updated": True,
    }
    deps = EntityValidateDeps(
        validate_fn=lambda name, neo4j, update: raw,
        neo4j_client_fn=lambda: _FakeNeo4j(),
    )
    result = tool_entity_validate(name="Acme", update=True, deps=deps)
    assert result["name"] == "Acme"
    assert result["updated"] is True
    assert result["matches"][0]["qid"] == "Q1"


def test_tool_entity_validate_failure_returns_error_envelope() -> None:
    def _boom(name: str, neo4j: Any, update: bool) -> dict:
        raise ConnectionError("KAIRIX_NEO4J_URI not reachable")

    deps = EntityValidateDeps(validate_fn=_boom, neo4j_client_fn=lambda: _FakeNeo4j())
    result = tool_entity_validate(name="x", deps=deps)
    assert result["error"].startswith("ConnectionError")
    assert result["matches"] == []
