"""pytest-bdd test module for chunk_date_fallback.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "chunk_date_fallback.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Documents with a date field use that date")
def test_frontmatter_date():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(
    FEATURE,
    "Documents without a date field still get a date from when the file was last changed",
)
def test_mtime_fallback():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "A date in the filename is preferred over the file modification date")
def test_path_date_priority():
    """Body populated by @scenario from the .feature file."""
