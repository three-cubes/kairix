"""pytest-bdd test module for search_config_validation.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "search_config_validation.feature")


@pytest.mark.bdd
@scenario(FEATURE, "A retrieval override typo names the bad key")
def test_retrieval_override_typo() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "An agent_pattern missing the placeholder is named explicitly")
def test_agent_pattern_missing_placeholder() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Two agents writing to overlapping paths are both named")
def test_overlapping_write_paths() -> None:
    """Body populated by @scenario from the .feature file."""
