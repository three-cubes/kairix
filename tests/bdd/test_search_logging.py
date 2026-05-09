"""pytest-bdd test module for search_logging.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "search_logging.feature")


@pytest.mark.bdd
@scenario(FEATURE, "A search call appends a JSONL event the SRE can grep")
def test_search_call_appends_jsonl() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Multiple searches each append their own line")
def test_multiple_searches_append_each() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Logging failure does not break search")
def test_logging_failure_does_not_break_search() -> None:
    """Body populated by @scenario from the .feature file."""
