"""pytest-bdd test module for mcp_agent_timeline.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "mcp_agent_timeline.feature")


@pytest.mark.bdd
@scenario(FEATURE, '"last week" is recognised as temporal')
def test_last_week_recognised():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, '"yesterday" produces a single-day window')
def test_yesterday_single_day():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Non-temporal query passes through unchanged")
def test_non_temporal_passthrough():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Timeline tool never raises")
def test_never_raises():
    """Body populated by @scenario from the .feature file."""
