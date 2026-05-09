"""pytest-bdd test module for entity_extraction.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "entity_extraction.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Known entities are extracted")
def test_known_entities_extracted():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Entity relationships are created")
def test_entity_relationships_created():
    """Body populated by @scenario from the .feature file."""
