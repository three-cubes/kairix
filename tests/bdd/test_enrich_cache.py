"""pytest-bdd test module for enrich_cache.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "enrich_cache.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Repeated chunk-date lookup for the same paths hits the cache")
def test_repeated_same_paths_hits_cache() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Different path sets trigger separate SQL lookups")
def test_disjoint_paths_separate_sql() -> None:
    """Body populated by @scenario from the .feature file."""
