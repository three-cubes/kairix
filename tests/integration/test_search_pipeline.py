"""
Integration tests: BM25 search pipeline against real indexed documents.
"""

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.integration
def test_search_returns_results(real_db, real_db_path):
    """Search against real indexed documents returns non-empty results."""
    from kairix.core.search.bm25 import bm25_search

    results = bm25_search(query="architecture decision", limit=5, db_path=real_db_path)
    assert len(results) > 0


@pytest.mark.integration
def test_search_results_have_no_duplicate_paths(real_db, real_db_path):
    """No two results share the same path."""
    from kairix.core.search.bm25 import bm25_search

    results = bm25_search(query="engineering", limit=20, db_path=real_db_path)
    paths = [r["file"] for r in results]
    assert len(paths) == len(set(paths))
