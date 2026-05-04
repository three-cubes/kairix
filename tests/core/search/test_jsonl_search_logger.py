"""Unit tests for JsonlSearchLogger (kairix.core.search.logger).

All filesystem work uses ``tmp_path``; no real ``/tmp`` or ``~/.cache``
writes. No ``@patch``, no ``monkeypatch.setattr`` against the logger's
internals — tests construct real ``JsonlSearchLogger`` instances with
``tmp_path``-scoped log files and assert behaviour by reading the files
back.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kairix.core.protocols import SearchLogger
from kairix.core.search.logger import JsonlSearchLogger, default_search_log_paths


def _read_lines(path: Path) -> list[dict[str, object]]:
    """Read a JSONL file into a list of dicts. Empty file -> empty list."""
    if not path.exists():
        return []
    out: list[dict[str, object]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            out.append(json.loads(raw))
    return out


# ---------------------------------------------------------------------------
# log_search: basic write + JSON shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_log_search_writes_one_jsonl_line(tmp_path: Path) -> None:
    log_path = tmp_path / "search.jsonl"
    lg = JsonlSearchLogger(search_log_path=log_path)

    lg.log_search({"query_hash": "abc123", "intent": "semantic", "fused_count": 5})

    rows = _read_lines(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["query_hash"] == "abc123"
    assert row["intent"] == "semantic"
    assert row["fused_count"] == 5


@pytest.mark.unit
def test_log_search_augments_missing_ts(tmp_path: Path) -> None:
    log_path = tmp_path / "search.jsonl"
    lg = JsonlSearchLogger(search_log_path=log_path)

    lg.log_search({"query_hash": "x"})

    rows = _read_lines(log_path)
    assert len(rows) == 1
    assert "ts" in rows[0]
    # ISO 8601 UTC: contains "T" separator and a UTC offset / "Z" / "+00:00"
    ts = rows[0]["ts"]
    assert isinstance(ts, str)
    assert "T" in ts


@pytest.mark.unit
def test_log_search_preserves_existing_ts(tmp_path: Path) -> None:
    log_path = tmp_path / "search.jsonl"
    lg = JsonlSearchLogger(search_log_path=log_path)

    lg.log_search({"query_hash": "x", "ts": "2026-05-04T12:00:00+00:00"})

    rows = _read_lines(log_path)
    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-05-04T12:00:00+00:00"


@pytest.mark.unit
def test_multiple_log_search_calls_produce_multiple_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "search.jsonl"
    lg = JsonlSearchLogger(search_log_path=log_path)

    for i in range(5):
        lg.log_search({"query_hash": f"hash-{i}"})

    rows = _read_lines(log_path)
    assert len(rows) == 5
    assert [r["query_hash"] for r in rows] == [f"hash-{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# Parent-directory creation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parent_directory_created_on_first_call(tmp_path: Path) -> None:
    nested = tmp_path / "deeply" / "nested" / "logs"
    log_path = nested / "search.jsonl"
    assert not nested.exists()

    lg = JsonlSearchLogger(search_log_path=log_path)
    lg.log_search({"query_hash": "x"})

    assert nested.is_dir()
    assert log_path.is_file()


# ---------------------------------------------------------------------------
# log_query: gated on query_log_path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_log_query_writes_when_path_set(tmp_path: Path) -> None:
    search_path = tmp_path / "search.jsonl"
    query_path = tmp_path / "query.jsonl"
    lg = JsonlSearchLogger(search_log_path=search_path, query_log_path=query_path)

    lg.log_query({"query": "what is kairix", "query_hash": "qh"})

    rows = _read_lines(query_path)
    assert len(rows) == 1
    assert rows[0]["query"] == "what is kairix"
    # Search log untouched.
    assert not search_path.exists()


@pytest.mark.unit
def test_log_query_no_op_when_path_none(tmp_path: Path) -> None:
    search_path = tmp_path / "search.jsonl"
    lg = JsonlSearchLogger(search_log_path=search_path, query_log_path=None)

    # Should not raise and should not create any file.
    lg.log_query({"query": "secret"})

    # No file path was configured, so nothing should be written anywhere.
    assert not search_path.exists()
    # The query log path was None, so there's nothing to check beyond "no exception raised".


# ---------------------------------------------------------------------------
# Schema preservation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_event_fields_preserved_as_is(tmp_path: Path) -> None:
    log_path = tmp_path / "search.jsonl"
    lg = JsonlSearchLogger(search_log_path=log_path)

    event: dict[str, object] = {
        "ts": "2026-05-04T08:00:00+00:00",
        "query_hash": "deadbeef",
        "intent": "entity",
        "agent": "agent-alpha",
        "scope": "shared+agent",
        "collections_searched": ["shared-knowledge", "agent-alpha-memory"],
        "vec_failed": False,
        "bm25_count": 12,
        "vec_count": 8,
        "fused_count": 15,
        "total_tokens": 4096,
        "latency_ms": 132.4,
        "result_count": 10,
        "success": True,
    }
    lg.log_search(event)

    rows = _read_lines(log_path)
    assert len(rows) == 1
    row = rows[0]
    for key, value in event.items():
        assert row[key] == value, f"field {key!r} not preserved: got {row.get(key)!r} want {value!r}"


# ---------------------------------------------------------------------------
# Failure handling: no raise on write failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_log_search_does_not_raise_on_write_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # tmp_path itself is a directory. Trying to open it as a file in append
    # mode raises IsADirectoryError (an OSError subclass) — which the logger
    # must catch and convert to a WARNING.
    bad_path = tmp_path  # Path is a directory, not a file
    lg = JsonlSearchLogger(search_log_path=bad_path)

    with caplog.at_level(logging.WARNING, logger="kairix.core.search.logger"):
        lg.log_search({"query_hash": "x"})  # must not raise

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log record on write failure"
    assert any("JsonlSearchLogger" in r.getMessage() for r in warnings)


# ---------------------------------------------------------------------------
# default_search_log_paths helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_search_log_paths_under_custom_base(tmp_path: Path) -> None:
    base = tmp_path / "kairix" / "logs"
    search_p, query_p = default_search_log_paths(base=base)

    assert search_p == base / "search.jsonl"
    assert query_p == base / "query.jsonl"
    # Helper is path-computation only — must not create anything on disk.
    assert not base.exists()


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_jsonl_search_logger_satisfies_search_logger_protocol(tmp_path: Path) -> None:
    lg = JsonlSearchLogger(search_log_path=tmp_path / "search.jsonl")
    assert isinstance(lg, SearchLogger)
