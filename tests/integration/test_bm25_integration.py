"""
Integration tests for ``kairix.core.search.bm25`` against the production
SQLite + FTS5 stack.

These tests run ``bm25_search`` against the session-scoped ``real_db`` /
``real_document_root`` fixtures (defined in ``tests/integration/conftest.py``)
which:

  - Build a real SQLite database with the production schema.
  - Run the production ``DocumentScanner`` over the reflib fixture.
  - Populate the production FTS5 index.

They exist to close the integration-level coverage gap on bm25.py:

  - The unit tests in ``tests/search/test_bm25.py`` use hand-rolled in-memory
    DBs that don't go through the production schema/scanner.
  - ``tests/integration/test_search_pipeline.py`` only checks "results > 0"
    and dedup; it doesn't exercise collection filtering, score normalisation,
    frontmatter stripping at SQL scale, date_filter_paths, or the SQL-error
    failure path against the real schema.

No mocking. No monkeypatching of the function under test. The only
monkeypatch usage is the env-var setup performed by ``real_document_root``
(an existing fixture) — same as every other integration test in this
suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.core.search.bm25 import bm25_search

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Happy-path queries against the real FTS5 index
# ---------------------------------------------------------------------------


def test_bm25_search_finds_engineering_docs(real_db, real_document_root) -> None:
    """A query that matches engineering reflib docs returns at least one
    hit and the top hit lives under the engineering collection.
    """
    results = bm25_search(query="twelve factor codebase", limit=10)
    assert len(results) >= 1, "FTS5 returned no rows for an engineering query"
    # Top result must be from the engineering collection.
    assert results[0]["collection"] == "engineering"


def test_bm25_search_filters_by_collection_against_real_db(real_db, real_document_root) -> None:
    """The collection filter must be honoured by the real FTS5 SQL path.
    Without filtering, results may span multiple collections; with a
    single-collection filter, every row must come from that collection.
    """
    results = bm25_search(
        query="security",
        collections=["engineering"],
        limit=20,
    )
    # If the fixture contains any matching engineering doc, we get rows.
    # If not, we get []. Either way every returned row must obey the filter.
    assert all(r["collection"] == "engineering" for r in results)


def test_bm25_search_excludes_other_collections_when_filtered(real_db, real_document_root) -> None:
    """Sabotage-prove the collection filter: an unrelated collection name
    must produce only that collection's results (no leakage)."""
    results = bm25_search(
        query="philosophy",
        collections=["philosophy"],
        limit=20,
    )
    assert all(r["collection"] == "philosophy" for r in results)


# ---------------------------------------------------------------------------
# Score normalisation contract — verified against real BM25 scores
# ---------------------------------------------------------------------------


def test_bm25_scores_are_normalised_into_zero_one_range(real_db, real_document_root) -> None:
    """The docstring contract: ``score = abs(raw)/(1+abs(raw))`` lies in
    [0, 1). Verify this against real FTS5 BM25 scores produced by the
    production schema — not synthetic single-doc DBs.
    """
    results = bm25_search(query="engineering codebase", limit=20)
    assert results, "expected at least one hit on the reflib fixture"
    for r in results:
        assert 0.0 <= r["score"] < 1.0, f"score {r['score']} out of normalised range for {r['file']}"


# ---------------------------------------------------------------------------
# Frontmatter stripping contract — verified against real fixture docs
# (every reflib fixture doc has YAML frontmatter; the SQL path must
#  serve a snippet that does NOT begin with the frontmatter delimiter)
# ---------------------------------------------------------------------------


def test_bm25_snippets_do_not_leak_yaml_frontmatter(real_db, real_document_root) -> None:
    """The reflib fixture docs all start with ``---`` YAML blocks. The
    snippet returned by bm25_search MUST be the body, not the frontmatter.
    """
    results = bm25_search(query="codebase", limit=10)
    assert results, "expected reflib hits for 'codebase'"
    for r in results:
        snippet = r["snippet"]
        # The snippet must not begin with the YAML delimiter.
        assert not snippet.lstrip().startswith("---"), f"snippet for {r['file']} leaked frontmatter: {snippet[:80]!r}"
        # The snippet must not contain the frontmatter ``title:`` key
        # at the very start (well-formed docs put it in the YAML block).
        assert not snippet.lstrip().startswith("title:"), (
            f"snippet for {r['file']} leaked YAML title key: {snippet[:80]!r}"
        )


def test_bm25_snippet_length_capped_at_300_chars(real_db, real_document_root) -> None:
    """Per impl, snippets are truncated to 300 characters. Verify against
    the real FTS5 path which serves variable-length doc bodies.
    """
    results = bm25_search(query="codebase", limit=20)
    assert results, "expected reflib hits"
    for r in results:
        assert len(r["snippet"]) <= 300, f"snippet for {r['file']} exceeded 300 chars: {len(r['snippet'])}"


# ---------------------------------------------------------------------------
# Limit + result-shape contract
# ---------------------------------------------------------------------------


def test_bm25_search_respects_limit_against_real_db(real_db, real_document_root) -> None:
    """The ``limit`` kwarg caps the number of rows returned by the
    real FTS5 SQL path."""
    results = bm25_search(query="engineering", limit=2)
    assert len(results) <= 2


def test_bm25_search_returns_bare_paths_no_uri_scheme(real_db, real_document_root) -> None:
    """The ``file`` field is a document-store-relative path —
    no ``vault://`` / ``file://`` / ``http://`` scheme prefix."""
    results = bm25_search(query="engineering", limit=10)
    assert results, "expected reflib hits"
    for r in results:
        assert "://" not in r["file"], f"unexpected scheme in {r['file']!r}"


def test_bm25_search_results_have_all_typeddict_fields(real_db, real_document_root) -> None:
    """Every row returned by the real SQL path must populate the full
    BM25Result TypedDict surface."""
    results = bm25_search(query="codebase", limit=5)
    assert results, "expected reflib hits"
    for r in results:
        assert set(r.keys()) >= {"file", "title", "snippet", "score", "collection"}
        assert isinstance(r["file"], str) and r["file"]
        assert isinstance(r["snippet"], str)
        assert isinstance(r["score"], float)
        assert isinstance(r["collection"], str) and r["collection"]


# ---------------------------------------------------------------------------
# date_filter_paths contract against the real DB
# ---------------------------------------------------------------------------


def test_date_filter_paths_intersects_real_results(real_db, real_document_root) -> None:
    """``date_filter_paths`` keeps only rows whose ``file`` is in the set."""
    # First, learn what paths the unfiltered query returns.
    base = bm25_search(query="engineering", limit=10)
    assert base, "expected reflib hits before filtering"

    # Pick one path from the unfiltered results, and verify the filter
    # keeps exactly that one (and nothing else).
    chosen = base[0]["file"]
    filtered = bm25_search(
        query="engineering",
        limit=10,
        date_filter_paths=frozenset([chosen]),
    )
    assert filtered, "filter dropped a path that the unfiltered query found"
    assert all(r["file"] == chosen for r in filtered)


def test_date_filter_paths_with_no_matches_returns_empty(real_db, real_document_root) -> None:
    """A date_filter_paths set that matches nothing in the result set
    yields []."""
    results = bm25_search(
        query="engineering",
        limit=10,
        date_filter_paths=frozenset(["totally/bogus/path/that/cannot/exist.md"]),
    )
    assert results == []


# ---------------------------------------------------------------------------
# Never-raises contract on the SQL path: bad db_path is swallowed
# ---------------------------------------------------------------------------


def test_bm25_search_never_raises_on_missing_db(tmp_path: Path) -> None:
    """A nonexistent db_path must yield [] — never raise. Exercised
    against a fresh tmp path so we know there is no cached DB handle."""
    results = bm25_search(
        query="anything",
        db_path=tmp_path / "does-not-exist.sqlite",
    )
    assert results == []


def test_bm25_search_never_raises_on_db_without_fts_table(tmp_path: Path) -> None:
    """A SQLite DB without the FTS5 schema must yield [] — never raise.

    This exercises the SQL execution failure branch (the ``except`` around
    ``db.execute(sql, params).fetchall()``) which wasn't covered by the
    other tests in this suite. We build a real SQLite file with no
    ``documents_fts`` table; opening succeeds, the SQL execute throws,
    and the function must swallow it.
    """
    import sqlite3

    db_path = tmp_path / "no_fts.sqlite"
    db = sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE placeholder (id INTEGER PRIMARY KEY)")
    db.commit()
    db.close()

    results = bm25_search(query="anything", db_path=db_path)
    assert results == []
