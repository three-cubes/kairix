"""Unit tests for kairix.quality.eval.logger.QueryLogger."""

import json

import pytest

from kairix.quality.eval.logger import QueryLogger
from kairix.quality.eval.schema import QueryLogEntry


@pytest.mark.unit
def test_logger_writes_jsonl(tmp_path):
    log_path = tmp_path / "test.jsonl"
    ql = QueryLogger(log_path=log_path)
    entry = QueryLogEntry(
        ts="2026-04-16T10:00:00Z",
        agent="shape",
        query="test query",
        intent="semantic",
        result_count=5,
        bm25_count=3,
        vec_count=2,
        latency_ms=42.0,
    )
    ql.log(entry)
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["query"] == "test query"
    assert row["agent"] == "shape"


@pytest.mark.unit
def test_logger_appends(tmp_path):
    log_path = tmp_path / "test.jsonl"
    ql = QueryLogger(log_path=log_path)
    for i in range(3):
        ql.log(
            QueryLogEntry(
                ts="2026-04-16T10:00:00Z",
                agent="shape",
                query=f"query {i}",
                intent="semantic",
                result_count=1,
                bm25_count=1,
                vec_count=0,
                latency_ms=10.0,
            )
        )
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 3


@pytest.mark.unit
def test_logger_never_raises_on_bad_path():
    """Logger must not raise even if the path is unwritable."""
    ql = QueryLogger(log_path="/nonexistent/deep/path/test.jsonl")
    entry = QueryLogEntry(
        ts="2026-04-16T10:00:00Z",
        agent="shape",
        query="q",
        intent="semantic",
        result_count=0,
        bm25_count=0,
        vec_count=0,
        latency_ms=0.0,
    )
    ql.log(entry)  # should not raise
    assert True, "smoke: unwritable path did not raise"


@pytest.mark.unit
def test_logger_creates_parent_directories(tmp_path):
    """Logger creates parent directories if they do not exist."""
    log_path = tmp_path / "deep" / "nested" / "test.jsonl"
    ql = QueryLogger(log_path=log_path)
    entry = QueryLogEntry(
        ts="2026-04-16T10:00:00Z",
        agent="shape",
        query="test",
        intent="semantic",
        result_count=1,
        bm25_count=1,
        vec_count=0,
        latency_ms=5.0,
    )
    ql.log(entry)
    assert log_path.exists()


@pytest.mark.unit
def test_logger_entry_contains_all_fields(tmp_path):
    """Logged entry contains all QueryLogEntry fields."""
    log_path = tmp_path / "test.jsonl"
    ql = QueryLogger(log_path=log_path)
    entry = QueryLogEntry(
        ts="2026-04-16T10:00:00Z",
        agent="builder",
        query="architecture",
        intent="conceptual",
        result_count=3,
        bm25_count=2,
        vec_count=1,
        latency_ms=55.5,
        top_path="docs/arch.md",
        vec_failed=True,
        error="dim mismatch",
    )
    ql.log(entry)
    row = json.loads(log_path.read_text().strip())
    assert row["agent"] == "builder"
    assert row["intent"] == "conceptual"
    assert row["result_count"] == 3
    assert row["top_path"] == "docs/arch.md"
    assert row["vec_failed"] is True
    assert row["error"] == "dim mismatch"
    assert row["latency_ms"] == pytest.approx(55.5)


@pytest.mark.unit
def test_logger_log_entries_are_valid_json_lines(tmp_path):
    """Each line in the log file is independently valid JSON."""
    log_path = tmp_path / "test.jsonl"
    ql = QueryLogger(log_path=log_path)
    for i in range(5):
        ql.log(
            QueryLogEntry(
                ts=f"2026-04-16T10:0{i}:00Z",
                agent="shape",
                query=f"query {i}",
                intent="semantic",
                result_count=i,
                bm25_count=i,
                vec_count=0,
                latency_ms=float(i),
            )
        )
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 5
    for line in lines:
        parsed = json.loads(line)
        assert "query" in parsed
        assert "ts" in parsed


@pytest.mark.unit
def test_from_search_result_with_simple_result(tmp_path):
    """from_search_result factory logs a search result object."""
    log_path = tmp_path / "test.jsonl"

    # Create a mock search result object
    class MockSearchResult:
        query = "test query"
        intent = "semantic"
        results = []  # noqa: RUF012
        bm25_count = 2
        vec_count = 1
        latency_ms = 30.0
        vec_failed = False
        error = None

    logger_instance = QueryLogger.from_search_result(MockSearchResult(), agent="shape", log_path=log_path)
    assert isinstance(logger_instance, QueryLogger)
    assert log_path.exists()
    row = json.loads(log_path.read_text().strip())
    assert row["query"] == "test query"
    assert row["agent"] == "shape"
    assert row["bm25_count"] == 2


@pytest.mark.unit
def test_from_search_result_with_intent_enum(tmp_path):
    """from_search_result handles intent objects with .value attribute."""
    log_path = tmp_path / "test.jsonl"

    class MockIntent:
        value = "temporal"

    class MockSearchResult:
        query = "what happened last week"
        intent = MockIntent()
        results = []  # noqa: RUF012
        bm25_count = 0
        vec_count = 0
        latency_ms = 10.0
        vec_failed = False
        error = None

    QueryLogger.from_search_result(MockSearchResult(), agent="builder", log_path=log_path)
    row = json.loads(log_path.read_text().strip())
    assert row["intent"] == "temporal"


@pytest.mark.unit
def test_from_search_result_extracts_top_path(tmp_path):
    """from_search_result extracts top_path from first result."""
    log_path = tmp_path / "test.jsonl"

    class MockInnerResult:
        path = "docs/architecture.md"

    class MockResult:
        result = MockInnerResult()

    class MockSearchResult:
        query = "architecture"
        intent = "conceptual"
        results = [MockResult()]  # noqa: RUF012
        bm25_count = 1
        vec_count = 0
        latency_ms = 5.0
        vec_failed = False
        error = None

    QueryLogger.from_search_result(MockSearchResult(), agent="shape", log_path=log_path)
    row = json.loads(log_path.read_text().strip())
    assert row["top_path"] == "docs/architecture.md"


@pytest.mark.unit
def test_default_log_path_uses_env(monkeypatch, tmp_path):
    """QueryLogger default path respects KAIRIX_SEARCH_LOG env var."""
    custom_path = tmp_path / "custom.jsonl"
    monkeypatch.setenv("KAIRIX_SEARCH_LOG", str(custom_path))
    # Re-import to pick up env change
    import importlib

    import kairix.quality.eval.logger as logger_mod

    importlib.reload(logger_mod)
    ql = logger_mod.QueryLogger()
    assert ql._path == custom_path
