"""pytest-bdd binding for search_chunk_date_recency.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario(
    "features/search_chunk_date_recency.feature",
    "A TEMPORAL query with an explicit date lifts the doc whose chunk_date matches",
)
def test_temporal_query_lifts_recency_matched_doc() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(
    "features/search_chunk_date_recency.feature",
    "Production factory.build_search_pipeline wires the temporal boost classes",
)
def test_factory_wires_temporal_boosts() -> None:
    """Body populated by @scenario from the .feature file."""
