"""pytest-bdd test module for search_intents.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "search_intents.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Intent is classified correctly for canonical queries")
def test_intent_canonical_routing():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Search never raises on empty input")
def test_empty_input():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Search never raises on garbage input")
def test_garbage_input():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Temporal intent takes priority over entity")
def test_temporal_beats_entity():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Multi-hop intent takes priority over entity")
def test_multi_hop_beats_entity():
    """Body populated by @scenario from the .feature file."""
