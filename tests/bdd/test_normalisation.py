"""pytest-bdd test module for normalisation.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "normalisation.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Boilerplate files are filtered")
def test_boilerplate_filtered():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Frontmatter is injected")
def test_frontmatter_injected():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "CC-BY-SA sources are excluded")
def test_ccbysa_excluded():
    """Body populated by @scenario from the .feature file."""
