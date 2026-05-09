"""pytest-bdd binding for curator_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/curator_cli.feature", "--help lists the curator subcommands")
def test_curator_help() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/curator_cli.feature", "No subcommand fails with argparse usage error")
def test_curator_no_subcommand() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(
    "features/curator_cli.feature",
    "health --format json reports neo4j_available=false when Neo4j is offline",
)
def test_curator_health_json() -> None:
    """Body populated by @scenario from the .feature file."""
