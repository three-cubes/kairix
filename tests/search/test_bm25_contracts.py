"""Contract-first tests for kairix.core.search.bm25.

Probes the documented contracts of ``bm25_search``:

  - "Never raises" — all failure paths return [].
  - Score normalisation: ``abs(s) / (1 + abs(s))``.
  - Frontmatter stripping when doc starts with ``---``.
  - ``doc_repo`` injection seam: when provided, delegates to ``doc_repo.search_fts``
    and maps the raw rows into BM25Result.
  - ``date_filter_paths`` post-query filter behaviour.
  - Whitespace-only query → [] without DB access.

These tests are written against docstring claims — not the impl —
and sabotage-proven before commit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kairix.core.search.bm25 import bm25_search

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_db_with_doc(tmp_path: Path, doc_text: str, *, path: str = "doc.md") -> Path:
    """Create a single-doc SQLite DB with the given doc content."""
    db_path = tmp_path / "single.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript(f"""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT,
            hash TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        CREATE VIRTUAL TABLE documents_fts USING fts5(
            title, doc, content='', tokenize='porter unicode61'
        );
        INSERT INTO documents (collection, path, title, hash, active)
        VALUES ('vault', '{path}', 'Test Doc', 'h1', 1);
    """)
    db.execute("INSERT INTO content (hash, doc) VALUES ('h1', ?)", (doc_text,))
    db.execute(
        "INSERT INTO documents_fts(rowid, title, doc) "
        "SELECT d.id, d.title, c.doc FROM documents d JOIN content c ON c.hash = d.hash"
    )
    db.commit()
    db.close()
    return db_path


class _DocRepoStub:
    """Minimal DocumentRepository-shaped stub for the doc_repo branch."""

    def __init__(
        self,
        *,
        rows: list[dict] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._rows = list(rows or [])
        self._raises = raises
        self.calls: list[tuple[str, list[str] | None, int]] = []

    def search_fts(
        self,
        query: str,
        collections: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        self.calls.append((query, collections, limit))
        if self._raises is not None:
            raise self._raises
        return self._rows


# ---------------------------------------------------------------------------
# Whitespace / empty contracts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_whitespace_only_query_returns_empty_without_calling_doc_repo() -> None:
    """A whitespace-only query short-circuits to [] BEFORE the doc_repo
    branch runs. Sabotage-prove: assert the repo was never called.
    """
    repo = _DocRepoStub(rows=[{"file": "x", "title": "", "snippet": "", "score": 0, "collection": ""}])
    results = bm25_search("   \t\n   ", doc_repo=repo)
    assert results == []
    assert repo.calls == [], "whitespace-only query must not reach doc_repo.search_fts"


# ---------------------------------------------------------------------------
# Score normalisation contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_score_is_normalised_to_zero_one_range(tmp_path: Path) -> None:
    """Per impl: ``score = abs(raw) / (1 + abs(raw))``. The mapped score
    must lie in [0, 1).
    """
    db_path = _create_db_with_doc(tmp_path, "kairix knowledge platform")
    results = bm25_search("kairix knowledge", db_path=db_path)
    assert results
    for r in results:
        assert 0.0 <= r["score"] < 1.0


# ---------------------------------------------------------------------------
# Frontmatter stripping contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_snippet_strips_yaml_frontmatter_when_doc_starts_with_triple_dash(tmp_path: Path) -> None:
    """Per impl: when doc starts with ``---``, the snippet is the body
    (after the closing ``---``), not the frontmatter block.
    """
    doc = "---\ntitle: Test\ntags: [foo]\n---\nThis is the body content with kairix."
    db_path = _create_db_with_doc(tmp_path, doc)
    results = bm25_search("kairix", db_path=db_path)
    assert results
    snippet = results[0]["snippet"]
    # Body content present.
    assert "body content" in snippet
    # YAML keys NOT in the snippet (otherwise we leaked frontmatter).
    assert "title:" not in snippet
    assert "tags:" not in snippet


@pytest.mark.unit
def test_snippet_does_not_strip_when_doc_does_not_start_with_triple_dash(tmp_path: Path) -> None:
    """Docs without frontmatter keep their full content (truncated to 300)."""
    doc = "Plain content about kairix without any frontmatter delimiter."
    db_path = _create_db_with_doc(tmp_path, doc)
    results = bm25_search("kairix", db_path=db_path)
    assert results
    assert results[0]["snippet"].startswith("Plain content")


@pytest.mark.unit
def test_snippet_falls_back_when_frontmatter_is_malformed(tmp_path: Path) -> None:
    """A doc starting with ``---`` but missing the closing delimiter
    should fall back to a 300-char prefix rather than raising.
    """
    doc = "---\nthis frontmatter has no closer and contains kairix"
    db_path = _create_db_with_doc(tmp_path, doc)
    results = bm25_search("kairix", db_path=db_path)
    assert results
    # Falls back to first-300 chars (which start with ---).
    assert results[0]["snippet"].startswith("---")


@pytest.mark.unit
def test_snippet_truncated_to_300_chars(tmp_path: Path) -> None:
    """Both fallback paths cap snippet at 300 chars."""
    long = "kairix " + ("filler text " * 200)  # >> 300 chars
    db_path = _create_db_with_doc(tmp_path, long)
    results = bm25_search("kairix", db_path=db_path)
    assert results
    assert len(results[0]["snippet"]) <= 300


# ---------------------------------------------------------------------------
# doc_repo injection seam contracts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_doc_repo_is_used_when_provided_skipping_direct_sql() -> None:
    """When doc_repo is provided, the function delegates to ``search_fts``
    and never touches a real DB. Confirmed by passing a nonexistent db_path
    that would fail open if the SQL branch ran.
    """
    repo = _DocRepoStub(
        rows=[{"file": "a.md", "title": "A", "snippet": "snippet A", "score": 0.5, "collection": "vault"}]
    )
    results = bm25_search(
        "anything",
        doc_repo=repo,
        db_path=Path("/nonexistent/should-not-be-touched.sqlite"),
    )
    assert len(repo.calls) == 1, "doc_repo.search_fts should have been called exactly once"
    assert results[0]["file"] == "a.md"


@pytest.mark.unit
def test_doc_repo_falls_back_to_path_key_when_file_absent() -> None:
    """Per impl: ``r.get("file", r.get("path", ""))`` — repos that
    return ``path`` instead of ``file`` must still produce a usable result.
    """
    repo = _DocRepoStub(
        rows=[{"path": "from-path-key.md", "title": "T", "snippet": "S", "score": 0.1, "collection": "c"}]
    )
    results = bm25_search("q", doc_repo=repo)
    assert len(results) == 1
    assert results[0]["file"] == "from-path-key.md"


@pytest.mark.unit
def test_doc_repo_falls_back_to_content_for_snippet_when_snippet_absent() -> None:
    """Per impl: ``r.get("snippet", r.get("content", "")[:300])``."""
    long_content = "x" * 500
    repo = _DocRepoStub(rows=[{"file": "a.md", "title": "T", "content": long_content, "score": 0.1, "collection": "c"}])
    results = bm25_search("q", doc_repo=repo)
    assert len(results) == 1
    assert len(results[0]["snippet"]) == 300


@pytest.mark.unit
def test_doc_repo_returns_empty_when_search_fts_raises() -> None:
    """The doc_repo branch swallows exceptions and returns [] (per "Never raises")."""
    repo = _DocRepoStub(raises=RuntimeError("repo broken"))
    results = bm25_search("q", doc_repo=repo)
    assert results == []


@pytest.mark.unit
def test_doc_repo_applies_date_filter_paths_post_query() -> None:
    """``date_filter_paths`` filters the doc_repo branch results by file path."""
    repo = _DocRepoStub(
        rows=[
            {"file": "keep.md", "title": "K", "snippet": "S", "score": 0.5, "collection": "c"},
            {"file": "drop.md", "title": "D", "snippet": "S", "score": 0.5, "collection": "c"},
        ]
    )
    results = bm25_search(
        "q",
        doc_repo=repo,
        date_filter_paths=frozenset(["keep.md"]),
    )
    assert [r["file"] for r in results] == ["keep.md"]


@pytest.mark.unit
def test_doc_repo_propagates_collections_and_limit_to_search_fts() -> None:
    """The collections list and limit kwarg should reach the repo."""
    repo = _DocRepoStub(rows=[])
    bm25_search("q", collections=["vault", "shared"], limit=3, doc_repo=repo)
    assert repo.calls[0] == ("q", ["vault", "shared"], 3)


# ---------------------------------------------------------------------------
# date_filter_paths contracts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_date_filter_paths_none_does_not_filter(tmp_path: Path) -> None:
    """date_filter_paths=None means no path filtering."""
    db_path = _create_db_with_doc(tmp_path, "kairix content", path="some/doc.md")
    results = bm25_search("kairix", db_path=db_path, date_filter_paths=None)
    assert len(results) == 1


@pytest.mark.unit
def test_date_filter_paths_excludes_results_not_in_set(tmp_path: Path) -> None:
    """When non-empty, only results whose ``file`` is in the set are kept."""
    db_path = _create_db_with_doc(tmp_path, "kairix content", path="some/doc.md")
    results = bm25_search(
        "kairix",
        db_path=db_path,
        date_filter_paths=frozenset(["totally-different-path.md"]),
    )
    assert results == []


# ---------------------------------------------------------------------------
# Never-raises guarantee on the doc_repo branch with malformed rows
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_doc_repo_branch_does_not_raise_on_partially_shaped_rows() -> None:
    """The doc_repo branch uses ``.get()`` defaults for every field, so
    a partially-shaped row (missing every documented key) must still
    yield a result rather than raise. Validates the never-raises invariant.
    """
    repo = _DocRepoStub(rows=[{}])  # totally empty row
    # The contract is just: must not raise. The shape of the result on
    # an empty row is unspecified by the docstring, but it must be a list.
    results = bm25_search("q", doc_repo=repo)
    assert isinstance(results, list)
