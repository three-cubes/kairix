"""Tests for kairix.core.db.fts — FTS5 index management."""

import sqlite3

import pytest

from kairix.core.db.fts import rebuild_fts, sync_fts


def _create_test_db() -> sqlite3.Connection:
    """Create an in-memory DB with documents + content tables populated."""
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT,
            hash TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT, created_at TEXT);

        INSERT INTO documents (collection, path, title, hash, active)
        VALUES ('vault-areas', 'doc1.md', 'First Document', 'hash1', 1);
        INSERT INTO content (hash, doc) VALUES ('hash1', 'The quick brown fox jumps over the lazy dog.');

        INSERT INTO documents (collection, path, title, hash, active)
        VALUES ('vault-areas', 'doc2.md', 'Second Document', 'hash2', 1);
        INSERT INTO content (hash, doc) VALUES ('hash2', 'Knowledge management for enterprise agents.');

        INSERT INTO documents (collection, path, title, hash, active)
        VALUES ('vault-areas', 'doc3.md', 'Inactive Doc', 'hash3', 0);
        INSERT INTO content (hash, doc) VALUES ('hash3', 'This document has been removed.');
    """)
    return db


@pytest.mark.unit
def test_rebuild_fts_indexes_active_documents() -> None:
    """rebuild_fts creates FTS index with only active documents."""
    db = _create_test_db()
    count = rebuild_fts(db)
    assert count == 2  # Only active docs (doc1, doc2), not inactive doc3


@pytest.mark.unit
def test_rebuild_fts_searchable() -> None:
    """FTS index supports MATCH queries after rebuild."""
    db = _create_test_db()
    rebuild_fts(db)

    rows = db.execute("SELECT rowid FROM documents_fts WHERE documents_fts MATCH 'knowledge'").fetchall()
    assert len(rows) == 1


@pytest.mark.unit
def test_rebuild_fts_idempotent() -> None:
    """Calling rebuild_fts twice produces correct results."""
    db = _create_test_db()
    rebuild_fts(db)
    count = rebuild_fts(db)
    assert count == 2


@pytest.mark.unit
def test_rebuild_fts_excludes_inactive() -> None:
    """Inactive (removed) documents are not in the FTS index."""
    db = _create_test_db()
    rebuild_fts(db)

    rows = db.execute("SELECT rowid FROM documents_fts WHERE documents_fts MATCH 'removed'").fetchall()
    assert len(rows) == 0


@pytest.mark.unit
def test_sync_fts_updates_specific_documents() -> None:
    """sync_fts updates only the specified documents."""
    db = _create_test_db()
    rebuild_fts(db)

    # Update doc1 content
    db.execute("UPDATE content SET doc = 'Updated content about retrieval' WHERE hash = 'hash1'")
    db.execute("UPDATE documents SET title = 'Updated Title' WHERE id = 1")

    synced = sync_fts(db, [1])
    assert synced == 1

    # Old content should not match
    rows = db.execute("SELECT rowid FROM documents_fts WHERE documents_fts MATCH 'fox'").fetchall()
    assert len(rows) == 0

    # New content should match
    rows = db.execute("SELECT rowid FROM documents_fts WHERE documents_fts MATCH 'retrieval'").fetchall()
    assert len(rows) == 1


@pytest.mark.unit
def test_sync_fts_empty_list() -> None:
    """sync_fts with empty list is a no-op."""
    db = _create_test_db()
    rebuild_fts(db)
    assert sync_fts(db, []) == 0


@pytest.mark.unit
def test_rebuild_fts_is_atomic_under_failure() -> None:
    """If an INSERT fails mid-rebuild, the OLD documents_fts must still exist.

    Regression for #223: previously rebuild_fts ran DROP, CREATE, INSERT as
    three separate auto-commit steps. A reader between DROP and CREATE saw
    "no such table: documents_fts" and BM25 silently degraded to vector-only.

    Sabotage-prove: drop the ``content`` table mid-rebuild so the INSERT's
    JOIN fails; assert documents_fts is still queryable with the old data.
    Without the BEGIN IMMEDIATE / rollback wrapping, the DROP would have
    committed and the assertion would fail.
    """
    db = _create_test_db()
    rebuild_fts(db)

    # Snapshot pre-rebuild state — used to confirm rollback restores it.
    pre_count = db.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
    assert pre_count == 2

    # Force the next rebuild to fail mid-flight by removing a JOINed table.
    db.execute("DROP TABLE content")

    with pytest.raises(sqlite3.OperationalError):
        rebuild_fts(db)

    # The old documents_fts MUST still be queryable. Without atomic
    # rebuild, the DROP TABLE IF EXISTS would have committed and this
    # query would raise "no such table: documents_fts".
    rows = db.execute("SELECT rowid FROM documents_fts").fetchall()
    assert len(rows) == pre_count


@pytest.mark.unit
def test_rebuild_fts_no_visibility_gap_for_concurrent_reader() -> None:
    """A second connection on the same DB must always see documents_fts
    while a rebuild is in progress.

    Regression for #223. Uses a temp-file database (in-memory connections
    can't be shared across cursors with WAL semantics). The reader runs on
    a separate connection and queries documents_fts AFTER rebuild_fts has
    committed; the queryability must be maintained across the rebuild.
    """
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        path = tmp.name

    writer = sqlite3.connect(path)
    writer.executescript("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT,
            hash TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT, created_at TEXT);
        INSERT INTO documents (collection, path, title, hash, active) VALUES ('vault', 'a.md', 'A', 'h1', 1);
        INSERT INTO content (hash, doc) VALUES ('h1', 'alpha bravo charlie');
    """)
    rebuild_fts(writer)

    reader = sqlite3.connect(path)
    pre_rows = reader.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
    assert pre_rows == 1

    # Rebuild and re-query through the reader. With atomic rebuild, the
    # reader never sees a "no such table" gap.
    writer.execute(
        "INSERT INTO documents (collection, path, title, hash, active) VALUES ('vault', 'b.md', 'B', 'h2', 1)"
    )
    writer.execute("INSERT INTO content (hash, doc) VALUES ('h2', 'delta echo foxtrot')")
    writer.commit()
    rebuild_fts(writer)

    post_rows = reader.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
    assert post_rows == 2

    reader.close()
    writer.close()
