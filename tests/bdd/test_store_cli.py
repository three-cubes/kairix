"""pytest-bdd binding for store_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/store_cli.feature", "A dry-run crawl prints counts without writing to Neo4j")
def test_store_dry_run_crawl() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(
    "features/store_cli.feature",
    "store health --json emits structured output reflecting Neo4j unavailability",
)
def test_store_health_json() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/store_cli.feature", "store with no subcommand prints help and exits non-zero")
def test_store_no_subcommand() -> None:
    """Body populated by @scenario from the .feature file."""
