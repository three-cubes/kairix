"""pytest-bdd test module for research_synthesis.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "research_synthesis.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Agent gets a best-effort answer when evidence is incomplete")
def test_low_confidence_synthesis():
    """Body populated by @scenario from the .feature file."""
