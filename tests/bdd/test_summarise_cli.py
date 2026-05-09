"""pytest-bdd binding for summarise_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/summarise_cli.feature", "Status reports zero coverage on an empty summaries database")
def test_status_empty() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/summarise_cli.feature", "Status reports the count of stored L0 and L1 summaries")
def test_status_populated() -> None:
    """Body populated by @scenario from the .feature file."""
