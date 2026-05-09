"""pytest-bdd test module for mcp_agent_contradict.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "mcp_agent_contradict.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Agent verifies a non-conflicting fact and gets the all-clear")
def test_no_contradictions():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Agent detects a conflict and gets an explanation")
def test_contradiction_detected():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Agent gets a safe response even when the system has issues")
def test_never_raises():
    """Body populated by @scenario from the .feature file."""
