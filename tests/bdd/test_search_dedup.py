"""pytest-bdd test module for search_dedup.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "search_dedup.feature")


@pytest.mark.bdd
@scenario(FEATURE, "No duplicates when same document indexed at different paths")
def test_no_duplicate_paths():
    """Body populated by @scenario from the .feature file."""
