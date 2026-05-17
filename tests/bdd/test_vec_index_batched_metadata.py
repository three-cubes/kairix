"""pytest-bdd binding for vec_index_batched_metadata.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "vec_index_batched_metadata.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Ten search results with diverse paths all return in a single SQL query")
def test_ten_results_single_sql_query() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Results preserve ANN ranking order even though SQL fetches them out of order")
def test_results_preserve_ann_ranking() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Inactive documents are filtered out of results")
def test_inactive_documents_excluded() -> None:
    """Body populated by @scenario from the .feature file."""
