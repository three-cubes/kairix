"""
FTS5 full-text search index management.

Builds and maintains the ``documents_fts`` FTS5 virtual table that powers
BM25 search. The index covers document titles and content, using the
``porter unicode61`` tokenizer for stemming and Unicode normalisation.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FtsHealth:
    """Preflight result for the BM25 / FTS leg of hybrid retrieval.

    ``available=False`` with ``reason="missing_table"`` means the BM25 leg
    will silently degrade to vector-only. Callers should escalate
    visibly (log at ERROR; surface in onboard check) and offer
    ``kairix embed --rebuild-fts`` as the fix.
    """

    available: bool
    reason: str  # ok | missing_table | empty | error:<detail>
    row_count: int = 0


def check_fts_available(db: sqlite3.Connection) -> FtsHealth:
    """Lightweight preflight: is the FTS leg of hybrid retrieval working?

    Returns ``FtsHealth(available=True, reason="ok", row_count=N)`` when
    ``documents_fts`` exists and is queryable. Returns
    ``available=False`` with a specific ``reason`` otherwise. Never
    raises — callers use the structured result to decide what to do.
    """
    try:
        row = db.execute("SELECT COUNT(*) FROM documents_fts").fetchone()
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "no such table" in msg.lower():
            return FtsHealth(available=False, reason="missing_table")
        return FtsHealth(available=False, reason=f"error:{msg}")
    except Exception as e:
        return FtsHealth(available=False, reason=f"error:{type(e).__name__}:{e}")

    count = int(row[0]) if row else 0
    if count == 0:
        return FtsHealth(available=False, reason="empty", row_count=0)
    return FtsHealth(available=True, reason="ok", row_count=count)


def rebuild_fts(db: sqlite3.Connection) -> int:
    """
    Drop and rebuild the FTS5 index from scratch.

    Reads all active documents from ``documents`` joined with ``content``
    and populates ``documents_fts``.

    Returns the number of documents indexed.

    The rebuild runs inside a single ``BEGIN IMMEDIATE`` transaction so
    concurrent readers see either the old FTS table or the new one, never
    a window where ``documents_fts`` is missing. Without this, a reader
    that runs `SELECT ... FROM documents_fts` between the DROP and the
    INSERT/commit gets "no such table: documents_fts" and the BM25 leg
    of hybrid retrieval silently degrades to vector-only.
    """
    # Use regular content FTS5 (not contentless content='') for accurate BM25 scoring.
    # Contentless mode saves disk but degrades ranking because term frequency
    # statistics are computed differently.
    started_transaction = not db.in_transaction
    if started_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("DROP TABLE IF EXISTS documents_fts")
        db.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(filepath, title, doc, tokenize='porter unicode61')")
        db.execute("""
            INSERT INTO documents_fts(rowid, filepath, title, doc)
            SELECT d.id, COALESCE(d.path, ''), COALESCE(d.title, ''), COALESCE(c.doc, '')
            FROM documents d
            JOIN content c ON c.hash = d.hash
            WHERE d.active = 1
        """)
        row = db.execute("SELECT COUNT(*) FROM documents_fts").fetchone()
        count: int = int(row[0]) if row else 0
        if started_transaction:
            db.commit()
    except Exception:
        if started_transaction:
            db.rollback()
        raise

    logger.info("db.fts: rebuilt FTS5 index — %d documents indexed", count)
    return count


def sync_fts(db: sqlite3.Connection, document_ids: list[int]) -> int:
    """
    Incrementally update the FTS5 index for specific documents.

    Used after a vault scan to add/update only the changed documents
    rather than rebuilding the entire index.

    Args:
        db:           Open database connection.
        document_ids: List of document IDs (from ``documents.id``) to sync.

    Returns:
        Number of documents synced.
    """
    if not document_ids:
        return 0

    # For contentless FTS5 tables, individual deletes require the original
    # content. A targeted rebuild for specific IDs is simpler and correct.
    # We delete matching rowids and re-insert from source tables.
    synced = 0
    for doc_id in document_ids:
        # Fetch current state from source tables
        row = db.execute(
            """
            SELECT d.id, COALESCE(d.title, ''), COALESCE(c.doc, '')
            FROM documents d
            JOIN content c ON c.hash = d.hash
            WHERE d.id = ? AND d.active = 1
            """,
            (doc_id,),
        ).fetchone()

        if row:
            synced += 1

    # If we need to sync, rebuild the entire FTS (contentless FTS5 doesn't
    # support efficient single-row updates). For small sync batches this is
    # acceptable; for large batches the caller should use rebuild_fts().
    if synced > 0:
        rebuild_fts(db)

    return synced
