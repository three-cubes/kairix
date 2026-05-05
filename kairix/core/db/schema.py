"""
Kairix database schema creation, validation, and migration.

The schema includes
a ``kairix_meta`` table for schema versioning. Column names and types are
identical to ensure all existing queries work without modification.

Tables:
  - documents       — document registry (path, collection, hash, active flag)
  - content         — document text keyed by content hash
  - content_vectors — chunk metadata (hash, seq, pos, model, embedded_at, chunk_date)
  - documents_fts   — FTS5 full-text search index
  - kairix_meta     — schema version tracking

Vector storage is handled by usearch (HNSW ANN index), not SQLite.
"""

import logging
import sqlite3

from . import EMBED_VECTOR_DIMS

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"


def create_schema(db: sqlite3.Connection, *, dims: int = EMBED_VECTOR_DIMS) -> None:
    """
    Create all kairix tables if they do not exist.

    Idempotent — safe to call on every startup. Uses IF NOT EXISTS for all
    DDL statements.

    Args:
        db:   Open sqlite3.Connection.
        dims: Vector embedding dimensions (for metadata only — vectors stored in usearch).
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT,
            hash TEXT NOT NULL,
            created_at TEXT,
            modified_at TEXT,
            active INTEGER DEFAULT 1,
            agent_owner TEXT,
            UNIQUE(collection, path)
        );

        CREATE TABLE IF NOT EXISTS content (
            hash TEXT PRIMARY KEY,
            doc TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS content_vectors (
            hash TEXT NOT NULL,
            seq INTEGER NOT NULL,
            pos INTEGER NOT NULL,
            model TEXT,
            embedded_at TEXT,
            chunk_date TEXT,
            PRIMARY KEY (hash, seq)
        );

        CREATE TABLE IF NOT EXISTS kairix_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(hash);
        CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection);
        CREATE INDEX IF NOT EXISTS idx_documents_active ON documents(active);
        CREATE INDEX IF NOT EXISTS idx_documents_agent_owner ON documents(agent_owner);
        CREATE INDEX IF NOT EXISTS idx_content_vectors_chunk_date ON content_vectors(chunk_date);
    """)

    # FTS5 — external content mode is not needed; we populate directly.
    # Check if it already exists before creating (FTS5 doesn't support IF NOT EXISTS).
    fts_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents_fts'").fetchone()
    if not fts_exists:
        db.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(filepath, title, doc, tokenize='porter unicode61')")

    # Schema version
    db.execute(
        "INSERT OR IGNORE INTO kairix_meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    db.execute(
        "INSERT OR IGNORE INTO kairix_meta (key, value) VALUES ('created_by', 'kairix')",
    )

    db.commit()
    logger.info(
        "db.schema: kairix schema initialised (version=%s, dims=%d)",
        SCHEMA_VERSION,
        dims,
    )


def validate_schema(db: sqlite3.Connection) -> list[str]:
    """
    Validate the database schema against expectations.

    Returns a list of error strings. Empty list means schema is valid.
    """
    errors: list[str] = []

    # Check required tables
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    for required in ("documents", "content", "content_vectors"):
        if required not in tables:
            errors.append(f"missing table: {required}")

    if errors:
        return errors  # Can't check columns if tables are missing

    # Check critical columns
    expected_cols = {
        "documents": {"id", "collection", "path", "hash", "active"},
        "content": {"hash", "doc"},
        "content_vectors": {"hash", "seq", "pos"},
    }
    for table, expected in expected_cols.items():
        # safe: table name from expected_cols keys (hardcoded)
        actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        missing = expected - actual
        if missing:
            errors.append(f"{table} missing columns: {missing}")

    return errors


def migrate(db: sqlite3.Connection) -> None:
    """
    Run all pending migrations. Idempotent — safe to call on every startup.

    Currently handles:
      - Adding chunk_date column to content_vectors (if missing)
      - Creating kairix_meta table (if missing)
    """
    # Ensure kairix_meta exists
    db.execute("""
        CREATE TABLE IF NOT EXISTS kairix_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # chunk_date migration (originally from embed/schema.py migrate_content_vectors)
    existing = {row[1] for row in db.execute("PRAGMA table_info(content_vectors)")}
    if "chunk_date" not in existing:
        db.execute("ALTER TABLE content_vectors ADD COLUMN chunk_date TEXT")
        db.commit()
        logger.info("db.schema: migration — added chunk_date column to content_vectors")

    # agent_owner migration — per-document agent provenance for #114.
    # Existing rows get NULL (treated as shared / not agent-owned) until a
    # `kairix embed --backfill-agent-owner` pass re-applies the path → agent
    # mapping from the configured AgentRegistry.
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "documents" in tables:
        existing_doc = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
        if "agent_owner" not in existing_doc:
            db.execute("ALTER TABLE documents ADD COLUMN agent_owner TEXT")
            db.commit()
            logger.info("db.schema: migration — added agent_owner column to documents")

    # Ensure indexes exist (idempotent) — only if the tables exist
    if "documents" in tables:
        db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(hash);
            CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection);
            CREATE INDEX IF NOT EXISTS idx_documents_active ON documents(active);
            CREATE INDEX IF NOT EXISTS idx_documents_agent_owner ON documents(agent_owner);
        """)
    if "content_vectors" in tables:
        db.execute("CREATE INDEX IF NOT EXISTS idx_content_vectors_chunk_date ON content_vectors(chunk_date)")
