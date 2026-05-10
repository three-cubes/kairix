"""pytest-bdd binding for wikilinks_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/wikilinks_cli.feature", "With no subcommand, prints usage and exits 0")
def test_wikilinks_no_subcommand() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/wikilinks_cli.feature", "--help prints usage and exits 0")
def test_wikilinks_help() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/wikilinks_cli.feature", "An unknown subcommand exits 1 and names the bad command")
def test_wikilinks_unknown_subcommand() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/wikilinks_cli.feature", "status reports entity counts even when empty")
def test_wikilinks_status() -> None:
    """Body populated by @scenario from the .feature file."""
