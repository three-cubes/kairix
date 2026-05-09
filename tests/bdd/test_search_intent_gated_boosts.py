"""pytest-bdd binding for search_intent_gated_boosts.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario(
    "features/search_intent_gated_boosts.feature",
    "A SEMANTIC question is not re-ordered by the procedural booster",
)
def test_semantic_not_reordered_by_procedural() -> None:
    pass


@pytest.mark.bdd
@scenario(
    "features/search_intent_gated_boosts.feature",
    "A PROCEDURAL question lifts the runbook into top-1",
)
def test_procedural_lifts_runbook() -> None:
    pass


@pytest.mark.bdd
@scenario(
    "features/search_intent_gated_boosts.feature",
    "A TEMPORAL query with an explicit date in the path gets the date-matched doc to top-1",
)
def test_temporal_lifts_dated_doc() -> None:
    pass


@pytest.mark.bdd
@scenario(
    "features/search_intent_gated_boosts.feature",
    "A PROCEDURAL query does NOT get a TEMPORAL boost",
)
def test_procedural_query_no_temporal_lift() -> None:
    pass
