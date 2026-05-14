"""
Tests for kairix.knowledge.summaries.staleness
"""

import sqlite3
from pathlib import Path

import pytest

from kairix.knowledge.summaries.generate import SummaryResult
from kairix.knowledge.summaries.staleness import (
    get_stale_paths,
    get_summary,
    init_summaries_db,
    is_stale,
    write_summary,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    init_summaries_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    f = tmp_path / "doc.md"
    f.write_text("Hello world.")
    return f


def _make_result(path: str, l0: str = "Abstract.", l1: str | None = None) -> SummaryResult:
    return SummaryResult(
        path=path,
        l0=l0,
        l1=l1,
        model="gpt-4o-mini",
        generated_at="2025-01-01T00:00:00+00:00",
        tokens_used=10,
    )


# ---------------------------------------------------------------------------
# init_summaries_db
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_creates_table(db: sqlite3.Connection):
    """init_summaries_db() should create the summaries table."""
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='summaries'").fetchall()
    assert len(tables) == 1, "summaries table not created"


@pytest.mark.unit
def test_init_is_idempotent(db: sqlite3.Connection):
    """Calling init_summaries_db() twice should not raise."""
    init_summaries_db(db)  # second call
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='summaries'").fetchall()
    assert len(tables) == 1


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_stale_true_for_missing_path(db: sqlite3.Connection, tmp_path: Path):
    """is_stale() returns True when no summary exists for the path."""
    path = str(tmp_path / "nonexistent.md")
    assert is_stale(path, db) is True


@pytest.mark.unit
def test_is_stale_false_for_fresh_summary(db: sqlite3.Connection, sample_file: Path):
    """is_stale() returns False when summary mtime matches source mtime."""
    result = _make_result(str(sample_file))
    write_summary(result, db)
    # File has not been modified since write_summary captured its mtime
    assert is_stale(str(sample_file), db) is False


@pytest.mark.unit
def test_is_stale_true_when_source_newer(db: sqlite3.Connection, sample_file: Path):
    """is_stale() returns True when source file is newer than stored mtime."""
    result = _make_result(str(sample_file))
    write_summary(result, db)

    # Artificially set stored mtime to past
    old_mtime = sample_file.stat().st_mtime - 10
    db.execute(
        "UPDATE summaries SET source_mtime = ? WHERE path = ?",
        (old_mtime, str(sample_file)),
    )
    db.commit()

    assert is_stale(str(sample_file), db) is True


# ---------------------------------------------------------------------------
# write_summary + get_summary round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_and_get_summary_roundtrip(db: sqlite3.Connection, sample_file: Path):
    """write_summary() + get_summary() should persist and retrieve correctly."""
    result = _make_result(str(sample_file), l0="My abstract.", l1="My overview.")
    write_summary(result, db)

    retrieved = get_summary(str(sample_file), db)
    assert retrieved is not None
    assert retrieved.path == str(sample_file)
    assert retrieved.l0 == "My abstract."
    assert retrieved.l1 == "My overview."
    assert retrieved.model == "gpt-4o-mini"


@pytest.mark.unit
def test_write_summary_upserts(db: sqlite3.Connection, sample_file: Path):
    """Writing a summary twice should update, not duplicate."""
    r1 = _make_result(str(sample_file), l0="First abstract.")
    write_summary(r1, db)

    r2 = _make_result(str(sample_file), l0="Updated abstract.")
    write_summary(r2, db)

    count = db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    assert count == 1

    retrieved = get_summary(str(sample_file), db)
    assert retrieved is not None
    assert retrieved.l0 == "Updated abstract."


@pytest.mark.unit
def test_get_summary_returns_none_for_missing(db: sqlite3.Connection):
    """get_summary() returns None when path not in DB."""
    result = get_summary("/nonexistent/path.md", db)
    assert result is None


# ---------------------------------------------------------------------------
# get_stale_paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_stale_paths_returns_missing(db: sqlite3.Connection, tmp_path: Path):
    """get_stale_paths() includes paths with no summary."""
    p1 = str(tmp_path / "a.md")
    p2 = str(tmp_path / "b.md")
    Path(p1).write_text("a")
    Path(p2).write_text("b")

    # Write summary for p1 only
    write_summary(_make_result(p1), db)

    stale = get_stale_paths([p1, p2], db)
    assert p2 in stale
    assert p1 not in stale


# ---------------------------------------------------------------------------
# is_stale — source file gone (FileNotFoundError path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_stale_true_when_source_file_deleted(db: sqlite3.Connection, sample_file: Path):
    """is_stale() returns True when the source file is gone (deleted between summary write and stale check)."""
    path = str(sample_file)
    write_summary(_make_result(path), db)
    # Confirm baseline: summary is fresh while the file exists.
    assert is_stale(path, db) is False
    # Now delete the source — is_stale should report True (stale, treat as needs-regen).
    sample_file.unlink()
    assert is_stale(path, db) is True


# ---------------------------------------------------------------------------
# write_summary — source file gone (FileNotFoundError path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_summary_stores_zero_mtime_when_source_missing(db: sqlite3.Connection, tmp_path: Path):
    """write_summary() falls back to source_mtime=0.0 when the source file does not exist.

    This is the defensive branch in staleness.write_summary that lets the
    summariser persist a result for a path whose source has been removed
    between fetch and write — instead of raising FileNotFoundError out to
    the caller, the row is stored with source_mtime=0.0 so any future
    is_stale() check returns True.
    """
    missing_path = str(tmp_path / "never-existed.md")
    write_summary(_make_result(missing_path), db)

    row = db.execute("SELECT source_mtime FROM summaries WHERE path = ?", (missing_path,)).fetchone()
    assert row is not None, "summary row should be persisted even when source is missing"
    # source_mtime=0.0 is the documented sentinel; use approx so a later
    # implementation tweak that produces 1e-12 doesn't silently regress on
    # platforms with subtly different float representations (S1244).
    assert row[0] == pytest.approx(0.0), f"expected source_mtime=0.0 fallback for missing source, got {row[0]!r}"
