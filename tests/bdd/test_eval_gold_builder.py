"""pytest-bdd test module for eval_gold_builder.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "eval_gold_builder.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Pooling combines candidates from multiple retrieval systems")
def test_pooling_combines_systems() -> None:
    pass


@pytest.mark.bdd
@scenario(FEATURE, "Pooling deduplicates documents that appear in multiple systems")
def test_pooling_deduplicates() -> None:
    pass


@pytest.mark.bdd
@scenario(FEATURE, "Building an independent gold suite produces graded YAML output")
def test_building_produces_graded_yaml() -> None:
    pass


@pytest.mark.bdd
@scenario(FEATURE, "Building skips queries where no candidates are pooled")
def test_building_skips_no_candidates() -> None:
    pass


@pytest.mark.bdd
@scenario(FEATURE, "Building short-circuits when no credentials are available")
def test_building_no_credentials() -> None:
    pass
