"""pytest-bdd test module for eval_tune.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "eval_tune.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Weak temporal category with date files recommends temporal boost")
def test_weak_temporal_recommends_boost():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Weak procedural category recommends path pattern extension")
def test_weak_procedural_recommends_patterns():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "All categories above floor produces no recommendations")
def test_all_above_floor_no_recs():
    """Body populated by @scenario from the .feature file."""
