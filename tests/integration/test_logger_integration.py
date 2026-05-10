"""Integration tests for JsonlSearchLogger wired into SearchPipeline.

End-to-end observability check: a search() call through a real
SearchPipeline (composed from canonical fakes for backends + real
JsonlSearchLogger writing to a tmp_path JSONL file) lands a structured
event on disk that an SRE can grep. No @patch, no monkeypatch.setattr.

The point of these tests is not to re-test logger primitives — those are
covered by tests/search/test_logger_contracts.py and
tests/core/search/test_jsonl_search_logger.py — but to verify that the
production wiring (SearchPipeline -> SearchLogger) actually emits what
the operations runbooks claim it does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.core.search.logger import JsonlSearchLogger
from kairix.core.search.pipeline import SearchPipeline
from kairix.core.search.scope import Scope
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakeVectorRepository,
)


def _read_lines(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _build_pipeline_with_jsonl_logger(
    *,
    search_log_path: Path,
    query_log_path: Path | None = None,
) -> SearchPipeline:
    """Compose a SearchPipeline with canonical fakes + a real JsonlSearchLogger."""
    docs = [
        {
            "path": "doc1.md",
            "collection": "shared-knowledge",
            "title": "Hello",
            "content": "hello world",
        },
    ]
    return SearchPipeline(
        classifier=FakeClassifier(intent=QueryIntent.SEMANTIC),
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
        vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
        graph=FakeGraphRepository(available=False),
        fusion=RRFFusion(k=60),
        boosts=[],
        logger=JsonlSearchLogger(
            search_log_path=search_log_path,
            query_log_path=query_log_path,
        ),
        config=RetrievalConfig.defaults(),
    )


# ---------------------------------------------------------------------------
# A search call writes a structured JSONL line to the configured path.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_pipeline_search_emits_jsonl_event_with_documented_fields(
    tmp_path: Path,
) -> None:
    """A pipeline.search() call lands one JSONL line with the new schema.

    The new-schema fields (agent, scope, collections_searched, vec_failed)
    are what multi-agent observability runbooks grep for. If any are
    missing the SRE story breaks.
    """
    log_path = tmp_path / "logs" / "search.jsonl"
    pipeline = _build_pipeline_with_jsonl_logger(search_log_path=log_path)

    pipeline.search("hello", agent="agent-alpha", scope=Scope.SHARED_AGENT)

    rows = _read_lines(log_path)
    assert len(rows) == 1, "exactly one JSONL line should land on disk"
    event = rows[0]

    # New observability fields:
    assert event["agent"] == "agent-alpha"
    assert event["scope"] == "shared+agent"
    assert "collections_searched" in event
    assert isinstance(event["collections_searched"], list)
    assert "vec_failed" in event
    assert isinstance(event["vec_failed"], bool)
    assert "fallback_used" in event

    # Existing fields — preserved per the module docstring:
    assert "query_hash" in event
    assert "intent" in event
    assert "bm25_count" in event
    assert "vec_count" in event
    assert "fused_count" in event
    assert "latency_ms" in event
    assert "ts" in event


@pytest.mark.integration
def test_pipeline_emits_one_line_per_search_call(tmp_path: Path) -> None:
    """N search calls -> exactly N JSONL lines (append-only, no clobbering)."""
    log_path = tmp_path / "search.jsonl"
    pipeline = _build_pipeline_with_jsonl_logger(search_log_path=log_path)

    for q in ["alpha", "bravo", "charlie", "delta"]:
        pipeline.search(q, agent="agent-beta", scope=Scope.SHARED)

    rows = _read_lines(log_path)
    assert len(rows) == 4
    # Each has a distinct query_hash (sha256[:12] of the query).
    hashes = [r["query_hash"] for r in rows]
    assert len(set(hashes)) == 4, "expected four distinct query_hash values"


@pytest.mark.integration
def test_pipeline_serialises_scope_enum_as_stable_string(tmp_path: Path) -> None:
    """The Scope enum is serialised as its string value, not its repr."""
    log_path = tmp_path / "search.jsonl"
    pipeline = _build_pipeline_with_jsonl_logger(search_log_path=log_path)

    pipeline.search("q", agent="agent-alpha", scope=Scope.ALL_AGENTS)

    event = _read_lines(log_path)[0]
    # Stable, grep-friendly string — not "Scope.ALL_AGENTS" or "<...>".
    assert event["scope"] == "all-agents"


@pytest.mark.integration
def test_pipeline_records_anonymous_agent_as_none(tmp_path: Path) -> None:
    """When no agent is set, the event records agent=null in JSON."""
    log_path = tmp_path / "search.jsonl"
    pipeline = _build_pipeline_with_jsonl_logger(search_log_path=log_path)

    pipeline.search("q")  # no agent kwarg

    raw = log_path.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 1
    # JSON null serialises as the literal string "null".
    assert '"agent": null' in raw[0]


# ---------------------------------------------------------------------------
# Logging failures must not crash search.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_search_completes_when_log_path_is_a_directory(tmp_path: Path) -> None:
    """If the log path is unwritable, search must still return a result.

    The class docstring guarantees: 'Search must not break because logging
    broke.' We point search_log_path at a directory (write fails with
    IsADirectoryError) and verify the pipeline still produces a SearchResult.
    """
    # tmp_path itself is a directory; the logger will fail to append to it.
    pipeline = _build_pipeline_with_jsonl_logger(search_log_path=tmp_path)

    result = pipeline.search("hello", agent="agent-alpha", scope=Scope.SHARED_AGENT)

    # Search returns a real SearchResult — no exception bubbled up.
    assert result is not None
    assert result.query == "hello"
    assert result.error == "", f"search should not surface logging error: {result.error!r}"


# ---------------------------------------------------------------------------
# query_log_path gating works end-to-end through the pipeline-owned logger.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_pipeline_does_not_write_query_log_when_path_unset(tmp_path: Path) -> None:
    """SearchPipeline never calls log_query through this code path; even if
    invoked directly, log_query is a no-op when query_log_path is None.

    This is the privacy gate — the search log is opt-out (always written),
    the query log is opt-in (only when path is configured).
    """
    log_path = tmp_path / "search.jsonl"
    query_path = tmp_path / "query.jsonl"
    # query_log_path explicitly None -> log_query is a no-op.
    pipeline = _build_pipeline_with_jsonl_logger(
        search_log_path=log_path,
        query_log_path=None,
    )

    pipeline.search("hello", agent="agent-alpha", scope=Scope.SHARED_AGENT)

    # Search log is written (opt-out)...
    assert log_path.exists()
    # ...but the query log is not (opt-in, off by default).
    assert not query_path.exists()


@pytest.mark.integration
def test_pipeline_logger_writes_query_when_path_configured(tmp_path: Path) -> None:
    """Wired-in JsonlSearchLogger writes to query_log_path when called.

    SearchPipeline.search() does not currently call log_query, so we
    invoke it directly on the logger we know is wired in. This proves
    the wiring is path-aware end-to-end.
    """
    log_path = tmp_path / "search.jsonl"
    query_path = tmp_path / "query.jsonl"
    pipeline = _build_pipeline_with_jsonl_logger(
        search_log_path=log_path,
        query_log_path=query_path,
    )
    # Sanity: pipeline.logger is a JsonlSearchLogger (proves wiring).
    assert isinstance(pipeline.logger, JsonlSearchLogger)

    pipeline.logger.log_query({"query": "what is kairix", "query_hash": "h"})

    rows = _read_lines(query_path)
    assert len(rows) == 1
    assert rows[0]["query"] == "what is kairix"
    # Search log untouched by log_query.
    assert not log_path.exists()
