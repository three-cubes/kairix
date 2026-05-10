"""pytest-bdd test module for timeline_absolute.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "timeline_absolute.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Agent asks about a specific month and gets the right date range")
def test_absolute_month_year():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Agent asks about last week and the system understands")
def test_relative_and_absolute():
    """Body populated by @scenario from the .feature file."""
