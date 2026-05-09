"""pytest-bdd test module for search_backends.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "search_backends.feature")


@pytest.mark.bdd
@scenario(FEATURE, "BM25 backend returns the document matching the query")
def test_bm25_returns_match() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "BM25 backend honours the collection filter")
def test_bm25_collection_filter() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "BM25 backend returns no results when nothing matches")
def test_bm25_no_match() -> None:
    """Body populated by @scenario from the .feature file."""
