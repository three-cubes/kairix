"""Tests for kairix.core.db.schema — schema creation, validation, migration."""

import sqlite3

import pytest

from kairix.core.db.schema import (
    SCHEMA_VERSION,
    create_schema,
    migrate,
    validate_schema,
)


@pytest.mark.unit
def test_create_schema_creates_all_tables() -> None:
    """create_schema() creates documents, content, content_vectors, kairix_meta, documents_fts."""
    db = sqlite3.connect(":memory:")
    create_schema(db)

    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "documents" in tables
    assert "content" in tables
    assert "content_vectors" in tables
    assert "kairix_meta" in tables


@pytest.mark.unit
def test_validate_schema_passes_on_valid_db() -> None:
    """validate_schema returns empty list on a correctly structured DB."""
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE documents (id INTEGER PRIMARY KEY, collection TEXT, path TEXT, hash TEXT, active INTEGER);
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        CREATE TABLE content_vectors (hash TEXT, seq INTEGER, pos INTEGER, PRIMARY KEY(hash, seq));
    """)
    errors = validate_schema(db)
    assert errors == []


@pytest.mark.unit
def test_validate_schema_detects_missing_table() -> None:
    """validate_schema reports missing tables."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, collection TEXT, path TEXT, hash TEXT, active INTEGER)")
    errors = validate_schema(db)
    assert any("content" in e for e in errors)


@pytest.mark.unit
def test_validate_schema_detects_missing_column() -> None:
    """validate_schema reports missing columns."""
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE documents (id INTEGER PRIMARY KEY, collection TEXT, path TEXT, active INTEGER);
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        CREATE TABLE content_vectors (hash TEXT, seq INTEGER, pos INTEGER, PRIMARY KEY(hash, seq));
    """)
    errors = validate_schema(db)
    assert any("hash" in e for e in errors)


@pytest.mark.unit
def test_migrate_adds_chunk_date() -> None:
    """migrate() adds chunk_date column to content_vectors if missing."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE content_vectors (hash TEXT, seq INTEGER, pos INTEGER, PRIMARY KEY(hash, seq))")
    migrate(db)
    cols = {row[1] for row in db.execute("PRAGMA table_info(content_vectors)")}
    assert "chunk_date" in cols


@pytest.mark.unit
def test_migrate_idempotent() -> None:
    """migrate() is safe to call repeatedly."""
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE content_vectors (hash TEXT, seq INTEGER, pos INTEGER, chunk_date TEXT, PRIMARY KEY(hash, seq))"
    )
    migrate(db)
    migrate(db)
    cols = {row[1] for row in db.execute("PRAGMA table_info(content_vectors)")}
    assert "chunk_date" in cols


@pytest.mark.unit
def test_validate_schema_empty_db_reports_all_missing() -> None:
    """validate_schema on completely empty DB reports all required tables missing."""
    db = sqlite3.connect(":memory:")
    errors = validate_schema(db)
    assert len(errors) == 3
    assert any("documents" in e for e in errors)
    assert any("content" in e for e in errors)
    assert any("content_vectors" in e for e in errors)


@pytest.mark.unit
def test_validate_schema_missing_content_vectors_only() -> None:
    """validate_schema reports just the missing table."""
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE documents (id INTEGER PRIMARY KEY, collection TEXT, path TEXT, hash TEXT, active INTEGER);
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
    """)
    errors = validate_schema(db)
    assert len(errors) == 1
    assert "content_vectors" in errors[0]


@pytest.mark.unit
def test_create_schema_creates_fts_table() -> None:
    """create_schema creates the documents_fts FTS5 virtual table."""
    db = sqlite3.connect(":memory:")
    create_schema(db)
    fts = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents_fts'").fetchone()
    assert fts is not None


@pytest.mark.unit
def test_create_schema_sets_schema_version() -> None:
    """create_schema stores the schema version in kairix_meta."""
    db = sqlite3.connect(":memory:")
    create_schema(db)
    row = db.execute("SELECT value FROM kairix_meta WHERE key='schema_version'").fetchone()
    assert row is not None
    assert row[0] == SCHEMA_VERSION


@pytest.mark.unit
def test_create_schema_idempotent() -> None:
    """create_schema can be called twice without error."""
    db = sqlite3.connect(":memory:")
    create_schema(db)
    create_schema(db)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "documents" in tables


@pytest.mark.unit
def test_migrate_creates_kairix_meta() -> None:
    """migrate() creates kairix_meta table if it does not exist."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE content_vectors (hash TEXT, seq INTEGER, pos INTEGER, PRIMARY KEY(hash, seq))")
    migrate(db)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "kairix_meta" in tables


@pytest.mark.unit
def test_migrate_creates_indexes_on_documents() -> None:
    """migrate() creates indexes on existing documents table."""
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE documents (id INTEGER PRIMARY KEY, collection TEXT, path TEXT, hash TEXT, active INTEGER);
        CREATE TABLE content_vectors (hash TEXT, seq INTEGER, pos INTEGER, chunk_date TEXT, PRIMARY KEY(hash, seq));
    """)
    migrate(db)
    indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_documents_hash" in indexes
    assert "idx_documents_collection" in indexes
    assert "idx_documents_active" in indexes


@pytest.mark.unit
def test_migrate_creates_chunk_date_index() -> None:
    """migrate() creates idx_content_vectors_chunk_date index."""
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE content_vectors (hash TEXT, seq INTEGER, pos INTEGER, chunk_date TEXT, PRIMARY KEY(hash, seq))"
    )
    migrate(db)
    indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_content_vectors_chunk_date" in indexes


@pytest.mark.unit
def test_create_schema_creates_indexes() -> None:
    """create_schema creates expected indexes."""
    db = sqlite3.connect(":memory:")
    create_schema(db)

    indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_documents_hash" in indexes
    assert "idx_documents_collection" in indexes
    assert "idx_documents_active" in indexes
    assert "idx_content_vectors_chunk_date" in indexes


@pytest.mark.unit
def test_create_schema_documents_table_columns() -> None:
    """create_schema creates documents table with all expected columns."""
    db = sqlite3.connect(":memory:")
    create_schema(db)

    cols = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
    expected = {
        "id",
        "collection",
        "path",
        "title",
        "hash",
        "created_at",
        "modified_at",
        "active",
    }
    assert expected.issubset(cols)


@pytest.mark.unit
def test_create_schema_content_vectors_has_chunk_date() -> None:
    """create_schema creates content_vectors with chunk_date column."""
    db = sqlite3.connect(":memory:")
    create_schema(db)

    cols = {row[1] for row in db.execute("PRAGMA table_info(content_vectors)")}
    assert "chunk_date" in cols
    assert "hash" in cols
    assert "seq" in cols
    assert "model" in cols


# ---------------------------------------------------------------------------
# agent_owner column (#114)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_schema_includes_agent_owner_column() -> None:
    """Fresh DB has agent_owner column on documents (default NULL)."""
    db = sqlite3.connect(":memory:")
    create_schema(db)

    cols = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
    assert "agent_owner" in cols


@pytest.mark.unit
def test_create_schema_creates_agent_owner_index() -> None:
    """Index idx_documents_agent_owner is created for filter performance."""
    db = sqlite3.connect(":memory:")
    create_schema(db)

    indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_documents_agent_owner" in indexes


@pytest.mark.unit
def test_migrate_adds_agent_owner_to_legacy_documents_table() -> None:
    """A pre-#114 documents table without agent_owner gets migrated additively.

    Existing rows survive with agent_owner=NULL.
    """
    db = sqlite3.connect(":memory:")
    # Build the old schema (pre-agent_owner) and seed a row
    db.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT,
            hash TEXT NOT NULL,
            created_at TEXT,
            modified_at TEXT,
            active INTEGER DEFAULT 1,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT, created_at TEXT);
        CREATE TABLE content_vectors (
            hash TEXT NOT NULL, seq INTEGER NOT NULL, pos INTEGER NOT NULL,
            model TEXT, embedded_at TEXT,
            PRIMARY KEY (hash, seq)
        );
        INSERT INTO documents (collection, path, title, hash, active)
        VALUES ('areas', '02-Areas/legacy.md', 'Legacy Doc', 'h1', 1);
        """
    )

    migrate(db)

    cols = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
    assert "agent_owner" in cols

    # Existing row preserved with agent_owner=NULL
    row = db.execute("SELECT path, title, agent_owner FROM documents").fetchone()
    assert row[0] == "02-Areas/legacy.md"
    assert row[1] == "Legacy Doc"
    assert row[2] is None


@pytest.mark.unit
def test_migrate_idempotent_on_agent_owner() -> None:
    """Running migrate twice doesn't error (idempotent)."""
    db = sqlite3.connect(":memory:")
    create_schema(db)
    migrate(db)
    migrate(db)  # second call must not raise
    cols = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
    assert "agent_owner" in cols


@pytest.mark.unit
def test_create_schema_on_legacy_db_runs_migration() -> None:
    """create_schema() must work on a pre-#114 documents table without agent_owner.

    Regression for VM hotfix 2026-05-06: deploying the WS3-5-114 schema to a
    running VM with an existing index.sqlite raised
    `sqlite3.OperationalError: no such column: agent_owner` because the
    CREATE INDEX for idx_documents_agent_owner ran inside the same
    executescript as the CREATE TABLE IF NOT EXISTS — the IF NOT EXISTS
    skipped the table create on legacy DBs (table already there without the
    column), but the CREATE INDEX still fired.

    Fix: split the executescript so migrate() runs *between* table creation
    and index creation. This test asserts the upgrade path works.
    """
    db = sqlite3.connect(":memory:")
    # Build a pre-#114 documents schema (no agent_owner column) and seed a row
    db.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT,
            hash TEXT NOT NULL,
            created_at TEXT,
            modified_at TEXT,
            active INTEGER DEFAULT 1,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT, created_at TEXT);
        CREATE TABLE content_vectors (
            hash TEXT NOT NULL, seq INTEGER NOT NULL, pos INTEGER NOT NULL,
            model TEXT, embedded_at TEXT,
            PRIMARY KEY (hash, seq)
        );
        INSERT INTO documents (collection, path, title, hash, active)
        VALUES ('areas', '02-Areas/legacy.md', 'Legacy Doc', 'h1', 1);
        """
    )

    # Must not raise — this is the bug the hotfix repairs
    create_schema(db)

    # Both columns are now present
    doc_cols = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
    cv_cols = {row[1] for row in db.execute("PRAGMA table_info(content_vectors)")}
    assert "agent_owner" in doc_cols
    assert "chunk_date" in cv_cols

    # Indexes for migrated columns are present
    indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_documents_agent_owner" in indexes
    assert "idx_content_vectors_chunk_date" in indexes

    # The pre-existing row survived migration with NULL agent_owner
    row = db.execute("SELECT path, agent_owner FROM documents").fetchone()
    assert row[0] == "02-Areas/legacy.md"
    assert row[1] is None
