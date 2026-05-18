"""Finite-score validation for BM25 scoring (#143 Phase 0b).

If the FTS5 backend ever returns a non-finite raw score (nan / +inf / -inf —
for example, on an empty document or a pathological index state), the
normalised score must clamp to 0 rather than propagate NaN into the
downstream RRF fusion. A NaN slipping into RRF silently rank-poisons every
query touching that document.

The check is driven through the public ``bm25_search`` surface:

* ``test_bm25_search_empty_doc_returns_finite_score`` — exercises the
  SQLite FTS5 path with a real empty document and asserts the emitted
  score is finite in [0, 1].
* ``test_bm25_search_clamps_nan_score_from_doc_repo`` — exercises the
  ``doc_repo=`` injection seam by feeding a fake repository that returns
  a NaN raw score; asserts the public surface clamps to 0 rather than
  propagate.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kairix.core.search.bm25 import bm25_search


def _create_db_with_empty_doc(tmp_path: Path) -> Path:
    """Build a SQLite FTS5 DB where the matched document has an empty body.

    Empty doc bodies are the canonical way to stress BM25 scoring — the
    schema mirrors production.
    """
    db_path = tmp_path / "test.sqlite"
    db = sqlite3.connect(str(db_path))
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
        CREATE VIRTUAL TABLE documents_fts USING fts5(
            title, doc, content='', tokenize='porter unicode61'
        );

        INSERT INTO documents (collection, path, title, hash, active)
        VALUES ('coll', 'empty-doc.md', 'kairix', 'h1', 1);
        INSERT INTO content (hash, doc) VALUES ('h1', '');

        INSERT INTO documents_fts(rowid, title, doc) SELECT d.id, d.title, c.doc
        FROM documents d JOIN content c ON c.hash = d.hash WHERE d.active = 1;
        """
    )
    db.close()
    return db_path


class _NonFiniteScoreDocRepo:
    """DocumentRepository fake returning a single row with the configured score.

    Used to drive the ``doc_repo=`` injection seam of ``bm25_search`` —
    simulates a pathological backend that emits NaN or inf.
    """

    def __init__(self, score: float) -> None:
        self._score = score

    def search_fts(
        self,
        query: str,
        *,
        collections: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        # Protocol-shape parameters consumed for kwarg-compat with the
        # real DocumentRepository — the fake doesn't filter or limit
        # because the contract under test is score clamping, not paging.
        del query, collections, limit
        return [
            {
                "file": "empty-doc.md",
                "title": "kairix",
                "snippet": "",
                "score": self._score,
                "collection": "coll",
            }
        ]


@pytest.mark.unit
def test_bm25_search_empty_doc_returns_finite_score(tmp_path: Path) -> None:
    """An empty document via the real SQLite FTS5 backend must emit a finite score."""
    db_path = _create_db_with_empty_doc(tmp_path)
    results = bm25_search("kairix", db_path=db_path)
    for r in results:
        assert math.isfinite(r["score"]), f"non-finite score for {r['file']}: {r['score']!r}"
        assert 0.0 <= r["score"] <= 1.0


@pytest.mark.unit
def test_bm25_search_clamps_nan_from_doc_repo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A NaN score from the injected DocumentRepository must clamp to 0.

    Exercises ``_coerce_finite_score`` on the public surface — a NaN
    slipping into RRF silently rank-poisons every query touching that
    row, so the public surface must defend.
    """
    with caplog.at_level("WARNING", logger="kairix.core.search.bm25"):
        results = bm25_search("anything", doc_repo=_NonFiniteScoreDocRepo(float("nan")))
    assert results, "doc_repo path returned no rows"
    for r in results:
        assert math.isfinite(r["score"])
        assert r["score"] == 0.0
    assert any("non-finite" in rec.message for rec in caplog.records)


@pytest.mark.unit
def test_bm25_search_clamps_positive_inf_from_doc_repo() -> None:
    """+inf score from doc_repo must clamp to 0 and stay finite."""
    results = bm25_search("anything", doc_repo=_NonFiniteScoreDocRepo(float("inf")))
    assert results
    for r in results:
        assert math.isfinite(r["score"])
        assert r["score"] == 0.0


@pytest.mark.unit
def test_bm25_search_clamps_negative_inf_from_doc_repo() -> None:
    """-inf score from doc_repo must clamp to 0 and stay finite."""
    results = bm25_search("anything", doc_repo=_NonFiniteScoreDocRepo(float("-inf")))
    assert results
    for r in results:
        assert math.isfinite(r["score"])
        assert r["score"] == 0.0


@pytest.mark.unit
def test_bm25_search_passes_finite_score_through_doc_repo() -> None:
    """A finite score from doc_repo must pass through unchanged — the clamp
    only triggers on non-finite values."""
    results = bm25_search("anything", doc_repo=_NonFiniteScoreDocRepo(0.42))
    assert results
    assert results[0]["score"] == 0.42
