"""Step definitions for reference_library.feature.

Indexes the 30-document fixture into a temp SQLite DB with FTS5, then runs
BM25 search against it. The fixture DB path is threaded through the
``bm25_search(db_path=...)`` kwarg — no env-var monkeypatch.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.db.fts import rebuild_fts
from kairix.core.db.scanner import CollectionConfig, DocumentScanner
from kairix.core.search.bm25 import bm25_search

pytestmark = pytest.mark.bdd

FIXTURE_ROOT = Path(__file__).resolve().parent.parent.parent / "integration" / "reflib_fixture"

_state: dict = {}

# Module-level temp dir to hold the fixture DB across scenarios
_tmpdir = tempfile.mkdtemp(prefix="kairix_reflib_test_")
_DB_PATH = Path(_tmpdir) / "reflib_test.sqlite"


def _build_fixture_db(db_path: Path) -> None:
    """Build a file-backed DB from the fixture documents."""
    if db_path.exists():
        return  # already built
    db = sqlite3.connect(str(db_path))
    from kairix.core.db.schema import create_schema

    create_schema(db)

    scanner = DocumentScanner(db, document_root=FIXTURE_ROOT)
    collections = []
    for subdir in sorted(FIXTURE_ROOT.iterdir()):
        if subdir.is_dir() and subdir.name != "__pycache__":
            collections.append(CollectionConfig(name=subdir.name, path=subdir.name))

    scanner.scan(collections)
    rebuild_fts(db)
    db.close()


@given("the reference library fixture is indexed")
def reflib_indexed():
    _build_fixture_db(_DB_PATH)
    _state["results"] = None


@when(parsers.re(r'I search for "(?P<query>.*)"'))
def search_for(query):
    results = bm25_search(query, db_path=_DB_PATH)
    _state["results"] = results
    _state["query"] = query


@then(parsers.parse("at least {count:d} result is returned"))
def at_least_n_results(count):
    results = _state["results"]
    assert results is not None, "No search was performed"
    assert len(results) >= count, f"Expected at least {count} results, got {len(results)}"


@then("the top result is from the engineering collection")
def top_result_engineering():
    results = _state["results"]
    assert results, "No results returned"
    top = results[0]
    assert top["collection"] == "engineering", f"Top result collection is {top['collection']!r}, expected 'engineering'"


@then('no result snippet starts with "---"')
def no_frontmatter_in_snippets():
    results = _state["results"]
    assert results is not None
    for r in results:
        snippet = r.get("snippet", "")
        assert not snippet.lstrip().startswith("---"), f"Snippet starts with frontmatter delimiter: {snippet[:80]}"


@then("results include BM25 matches")
def results_include_bm25():
    results = _state["results"]
    assert results is not None, "No search was performed"
    assert len(results) > 0, "BM25 returned no matches"
    # BM25 results should have a positive score
    for r in results:
        assert r.get("score", 0) != 0, "BM25 result has zero score"
