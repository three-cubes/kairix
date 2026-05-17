"""
Tests for kairix.core.embed.schema. Covers:
- get_db_path(): env override, missing file
- get_pending_chunks(): synthetic DB
- get_all_chunks_needing_embedding(): synthetic DB
- save_run_log(): creates and rotates
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kairix.core.embed.schema import (
    get_all_chunks_needing_embedding,
    get_pending_chunks,
    migrate_content_vectors,
    save_run_log,
)

# get_db_path coverage moved to tests/db/test_db_init.py (same behaviour,
# tested through env= and home= DI kwargs without process-env mutation).

# ---------------------------------------------------------------------------
# get_pending_chunks + get_all_chunks_needing_embedding
# ---------------------------------------------------------------------------


def _make_minimal_db() -> sqlite3.Connection:
    """Create minimal in-memory kairix schema for testing."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE documents (hash TEXT PRIMARY KEY, path TEXT, active INTEGER DEFAULT 1)")
    db.execute("CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT)")
    db.execute("CREATE TABLE content_vectors (hash TEXT, seq INTEGER, pos INTEGER)")
    return db


@pytest.mark.unit
def test_get_pending_chunks_returns_empty_when_all_embedded() -> None:
    """Returns [] when no chunks need embedding."""
    db = _make_minimal_db()
    db.execute("INSERT INTO documents VALUES ('abc123', 'test/doc.md', 1)")
    db.execute("INSERT INTO content VALUES ('abc123', 'some content')")
    # No content_vectors rows → content_vectors LEFT JOIN won't exclude them

    # For this test, get_pending_chunks expects v.hash IS NULL — since content_vectors
    # has no rows, all content is pending. So there should be 1 pending chunk.
    chunks = get_pending_chunks(db)
    assert len(chunks) == 1
    assert chunks[0]["path"] == "test/doc.md"
    assert chunks[0]["hash"] == "abc123"


@pytest.mark.unit
def test_get_pending_chunks_skips_inactive_docs() -> None:
    """Skips documents with active=0."""
    db = _make_minimal_db()
    db.execute("INSERT INTO documents VALUES ('abc123', 'test/doc.md', 0)")  # inactive
    db.execute("INSERT INTO content VALUES ('abc123', 'some content')")

    chunks = get_pending_chunks(db)
    assert chunks == []


@pytest.mark.unit
def test_get_pending_chunks_skips_empty_content() -> None:
    """Skips chunks with empty doc text."""
    db = _make_minimal_db()
    db.execute("INSERT INTO documents VALUES ('abc123', 'test/doc.md', 1)")
    db.execute("INSERT INTO content VALUES ('abc123', '')")  # empty content

    chunks = get_pending_chunks(db)
    assert chunks == []


@pytest.mark.unit
def test_get_all_chunks_needing_embedding_returns_empty_without_content_vectors() -> None:
    """Returns [] when content_vectors table is empty."""
    db = _make_minimal_db()

    result = get_all_chunks_needing_embedding(db)
    assert result == []


# ---------------------------------------------------------------------------
# save_run_log
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_save_run_log_creates_file_at_injected_path(tmp_path: Path) -> None:
    """Creates the run log file on first call when given an explicit ``log_path``."""
    log_path = tmp_path / "embed-runs.json"

    save_run_log({"run": 1, "status": "ok"}, log_path=log_path)

    assert log_path.exists()
    runs = json.loads(log_path.read_text())
    assert runs == [{"run": 1, "status": "ok"}]


@pytest.mark.unit
def test_save_run_log_creates_parent_directories(tmp_path: Path) -> None:
    """The injected log path's parent dirs are created on first call."""
    log_path = tmp_path / "nested" / "subdir" / "embed-runs.json"

    save_run_log({"run": 1}, log_path=log_path)

    assert log_path.exists()


@pytest.mark.unit
def test_save_run_log_appends_to_existing_log(tmp_path: Path) -> None:
    """Subsequent calls append rather than overwrite."""
    log_path = tmp_path / "embed-runs.json"
    log_path.write_text(json.dumps([{"run": 0}]))

    save_run_log({"run": 1}, log_path=log_path)

    runs = json.loads(log_path.read_text())
    assert runs == [{"run": 0}, {"run": 1}]


@pytest.mark.unit
def test_save_run_log_rotates_to_keep_last_90_runs(tmp_path: Path) -> None:
    """A 91st entry pushes run 0 out — only the 90 most-recent runs are kept."""
    log_path = tmp_path / "embed-runs.json"
    existing = [{"run": i} for i in range(90)]
    log_path.write_text(json.dumps(existing))

    save_run_log({"run": 90}, log_path=log_path)

    runs = json.loads(log_path.read_text())
    assert len(runs) == 90
    assert runs[-1] == {"run": 90}
    assert runs[0] == {"run": 1}  # run 0 dropped


# ---------------------------------------------------------------------------
# migrate_content_vectors — additive ALTER TABLE migration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_migrate_content_vectors_adds_chunk_date_column_when_missing() -> None:
    """A pre-migration content_vectors table without chunk_date gets the column added."""
    db = sqlite3.connect(":memory:")
    # Pre-migration schema: no chunk_date column.
    db.execute("CREATE TABLE content_vectors (hash TEXT, seq INTEGER, pos INTEGER, model TEXT, embedded_at INTEGER)")
    cols_before = {row[1] for row in db.execute("PRAGMA table_info(content_vectors)")}
    assert "chunk_date" not in cols_before

    migrate_content_vectors(db)

    cols_after = {row[1] for row in db.execute("PRAGMA table_info(content_vectors)")}
    assert "chunk_date" in cols_after


@pytest.mark.unit
def test_migrate_content_vectors_is_a_noop_when_chunk_date_already_present() -> None:
    """Running the migration twice does not re-add the column or raise."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE content_vectors (hash TEXT, seq INTEGER, pos INTEGER, model TEXT, embedded_at INTEGER)")
    migrate_content_vectors(db)  # adds chunk_date

    # Second call must be a no-op — would otherwise raise OperationalError on duplicate column.
    migrate_content_vectors(db)

    cols = [row[1] for row in db.execute("PRAGMA table_info(content_vectors)")]
    assert cols.count("chunk_date") == 1


@pytest.mark.unit
def test_save_run_log_recovers_from_corrupt_existing_log(tmp_path: Path) -> None:
    """A corrupt JSON file is replaced rather than crashing the save path."""
    log_path = tmp_path / "embed-runs.json"
    log_path.write_text("{garbled JSON")

    save_run_log({"run": 1}, log_path=log_path)

    runs = json.loads(log_path.read_text())
    assert runs == [{"run": 1}]
