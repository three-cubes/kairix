"""Integration tests for SearchPipeline + SearchLogger.

Validates that the pipeline emits a search-event dict carrying agent,
scope, collections_searched, and vec_failed fields — the new schema the
multi-agent observability work depends on.

Tests use a small in-test logger fake (implements SearchLogger via
isinstance) plus existing fakes from tests/fakes.py for the rest of the
pipeline. No @patch, no monkeypatch.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.core.protocols import SearchLogger
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.pipeline import SearchPipeline
from kairix.core.search.scope import Scope
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeGraphRepository,
    FakeVectorRepository,
)


class _RecordingSearchLogger:
    """Captures every log_search call for assertion."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []

    def log_search(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def log_query(self, event: dict[str, Any]) -> None:
        self.queries.append(event)


def _build_pipeline(logger: SearchLogger) -> SearchPipeline:
    """Compose a minimal SearchPipeline with fakes + the given logger."""
    from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend

    doc_repo = FakeDocumentRepository(
        documents=[
            {"path": "doc1.md", "collection": "shared", "title": "Test", "content": "hello world"},
        ]
    )

    class _FakeEmbedding:
        def embed(self, text: str) -> list[float]:
            return [0.0] * 4

        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 4 for _ in texts]

    return SearchPipeline(
        classifier=FakeClassifier(),
        bm25=BM25SearchBackend(doc_repo),
        vector=VectorSearchBackend(_FakeEmbedding(), FakeVectorRepository()),
        graph=FakeGraphRepository(available=False),
        fusion=RRFFusion(k=60),
        boosts=[],
        logger=logger,
    )


@pytest.mark.unit
def test_pipeline_emits_search_event_with_new_fields() -> None:
    rec = _RecordingSearchLogger()
    pipeline = _build_pipeline(rec)

    pipeline.search("hello", agent="shape", scope=Scope.SHARED_AGENT)

    assert len(rec.events) == 1
    event = rec.events[0]
    # New fields the multi-agent observability story depends on:
    assert event["agent"] == "shape"
    assert event["scope"] == "shared+agent"  # serialised as the enum's string value
    assert "collections_searched" in event
    assert isinstance(event["collections_searched"], list)
    assert "vec_failed" in event
    assert "fallback_used" in event
    # Existing fields still present:
    assert "query_hash" in event
    assert "intent" in event
    assert "bm25_count" in event
    assert "vec_count" in event
    assert "ts" in event


@pytest.mark.unit
def test_pipeline_serialises_scope_as_string_value() -> None:
    """A Scope enum in the call site must serialise as a stable string in the event."""
    rec = _RecordingSearchLogger()
    pipeline = _build_pipeline(rec)

    pipeline.search("hi", agent="shape", scope=Scope.ALL_AGENTS)

    assert rec.events[0]["scope"] == "all-agents"


@pytest.mark.unit
def test_pipeline_records_anonymous_agent() -> None:
    """When no agent is set, the event records agent=None (legitimate value)."""
    rec = _RecordingSearchLogger()
    pipeline = _build_pipeline(rec)

    pipeline.search("hi")

    assert rec.events[0]["agent"] is None


@pytest.mark.contract
def test_recording_logger_satisfies_search_logger_protocol() -> None:
    """The in-test fake structurally satisfies the SearchLogger Protocol."""
    rec = _RecordingSearchLogger()
    assert isinstance(rec, SearchLogger)
