"""
Tests for kairix.core.search.bm25 — direct SQLite FTS5 search.

Tests use in-memory SQLite databases with the kairix schema.
No subprocess calls — BM25 search is now fully internal.
"""

import sqlite3
from pathlib import Path

import pytest

from kairix.core.search.bm25 import BM25Result, bm25_search

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_db(tmp_path: Path) -> Path:
    """Create a test SQLite DB with FTS5 and sample documents."""
    db_path = tmp_path / "test.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript("""
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
        CREATE VIRTUAL TABLE documents_fts USING fts5(
            title, doc, content='', tokenize='porter unicode61'
        );

        INSERT INTO documents (collection, path, title, hash, active)
        VALUES ('knowledge-shared', 'shared/facts.md', 'Shared Facts', 'h1', 1);
        INSERT INTO content (hash, doc) VALUES ('h1', 'The VM has 4 vCPUs and 16 GB RAM.');

        INSERT INTO documents (collection, path, title, hash, active)
        VALUES ('knowledge-builder', 'builder/patterns.md', 'Builder Patterns', 'h2', 1);
        INSERT INTO content (hash, doc) VALUES ('h2', 'Use trash instead of rm for safety.');

        INSERT INTO documents (collection, path, title, hash, active)
        VALUES ('vault-areas', 'areas/kairix.md', 'Kairix Platform', 'h3', 1);
        INSERT INTO content (hash, doc)
        VALUES ('h3', 'Kairix is a knowledge management platform for enterprise agents.');

        INSERT INTO documents (collection, path, title, hash, active)
        VALUES ('vault-areas', 'areas/inactive.md', 'Inactive Doc', 'h4', 0);
        INSERT INTO content (hash, doc) VALUES ('h4', 'This document is inactive and should not appear in results.');

        -- Populate FTS index
        INSERT INTO documents_fts(rowid, title, doc) SELECT d.id, d.title, c.doc
        FROM documents d JOIN content c ON c.hash = d.hash WHERE d.active = 1;
    """)
    db.close()
    return db_path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bm25_search_returns_results(tmp_path: Path) -> None:
    """Successful FTS query returns BM25Result list."""
    db_path = _create_test_db(tmp_path)
    results = bm25_search("knowledge management platform", db_path=db_path)

    assert len(results) >= 1
    assert any("kairix" in r["file"] for r in results)


@pytest.mark.unit
def test_bm25_search_filters_by_collection(tmp_path: Path) -> None:
    """Collection filter restricts results to matching collections."""
    db_path = _create_test_db(tmp_path)
    results = bm25_search("VM vCPUs", collections=["knowledge-shared"], db_path=db_path)

    assert len(results) >= 1
    assert all(r["collection"] == "knowledge-shared" for r in results)


@pytest.mark.unit
def test_bm25_search_multiple_collections(tmp_path: Path) -> None:
    """Multiple collections are searched simultaneously."""
    db_path = _create_test_db(tmp_path)
    results = bm25_search("safety", collections=["knowledge-shared", "knowledge-builder"], db_path=db_path)

    assert len(results) >= 1


@pytest.mark.unit
def test_bm25_search_returns_bare_paths(tmp_path: Path) -> None:
    """Result file field is a bare document-store-relative path, not a scheme URI."""
    db_path = _create_test_db(tmp_path)
    results = bm25_search("knowledge management", db_path=db_path)

    for r in results:
        assert "://" not in r["file"]  # no scheme prefix


@pytest.mark.unit
def test_bm25_search_excludes_inactive_documents(tmp_path: Path) -> None:
    """Inactive (active=0) documents are excluded from results."""
    db_path = _create_test_db(tmp_path)
    results = bm25_search("inactive document", db_path=db_path)

    assert all("inactive" not in r["file"] for r in results)


@pytest.mark.unit
def test_bm25_search_respects_limit(tmp_path: Path) -> None:
    """Limit parameter caps results."""
    db_path = _create_test_db(tmp_path)
    results = bm25_search("management platform", limit=1, db_path=db_path)

    assert len(results) <= 1


@pytest.mark.unit
def test_bm25_search_applies_date_filter(tmp_path: Path) -> None:
    """date_filter_paths filters results to matching paths only."""
    db_path = _create_test_db(tmp_path)
    results = bm25_search(
        "knowledge management",
        date_filter_paths=frozenset(["areas/kairix.md"]),
        db_path=db_path,
    )

    assert all(r["file"] == "areas/kairix.md" for r in results)


# ---------------------------------------------------------------------------
# Failure modes — all must return []
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bm25_search_returns_empty_for_empty_query() -> None:
    """Empty query string → [] without touching DB."""
    results = bm25_search("")
    assert results == []


@pytest.mark.unit
def test_bm25_search_returns_empty_for_stop_words_only() -> None:
    """Query of only stop words → []."""
    results = bm25_search("what is the a an")
    assert results == []


@pytest.mark.unit
def test_bm25_search_returns_empty_on_db_error() -> None:
    """Database error → []."""
    results = bm25_search("query", db_path=Path("/nonexistent/db.sqlite"))
    assert results == []


# ---------------------------------------------------------------------------
# BM25Result TypedDict
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bm25_result_typeddict_fields() -> None:
    """BM25Result TypedDict has the expected fields."""
    r: BM25Result = BM25Result(
        file="/f.md",
        title="T",
        snippet="s",
        score=1.0,
        collection="c",
    )
    assert r["file"] == "/f.md"
    assert r["score"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Tokenisation behaviour observed through bm25_search public surface
# (per "no internal function tests" rule — tokenizer has its own dedicated
# tests in test_tokenizer.py; bm25 is responsible only for using it correctly)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bm25_search_handles_hyphenated_query_tokens(tmp_path: Path) -> None:
    """A hyphenated query (`project-x`) must be tokenised into separate
    terms so docs containing `project` match. Observed via results, not
    via inspecting the FTS string.
    """
    db_path = tmp_path / "hyphen.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL, path TEXT NOT NULL, title TEXT,
            hash TEXT NOT NULL, active INTEGER DEFAULT 1,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        CREATE VIRTUAL TABLE documents_fts USING fts5(title, doc, content='', tokenize='porter unicode61');

        INSERT INTO documents (collection, path, title, hash)
        VALUES ('docs', 'docs/project-overview.md', 'Project Overview', 'h1');
        INSERT INTO content (hash, doc)
        VALUES ('h1', 'A description of the Project Overview document.');
        INSERT INTO documents_fts(rowid, title, doc)
        SELECT d.id, d.title, c.doc FROM documents d JOIN content c ON c.hash = d.hash;
    """)
    db.close()
    # Hyphenated query must still match the doc with "project" in it.
    results = bm25_search("project-overview", db_path=db_path)
    assert len(results) >= 1


@pytest.mark.unit
def test_bm25_search_strips_stop_words_so_only_meaningful_tokens_drive_match(tmp_path: Path) -> None:
    """A query that is mostly stop-words should still match docs by the
    meaningful token. Observable through results: a doc containing only
    the stop-words returns nothing, but a doc with the meaningful token does.
    """
    db_path = _create_test_db(tmp_path)
    # "what do we know about kairix" — only "kairix" survives.
    results = bm25_search("what do we know about kairix", db_path=db_path)
    assert any("kairix" in r["file"] for r in results)
