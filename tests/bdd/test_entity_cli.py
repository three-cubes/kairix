"""pytest-bdd binding for entity_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/entity_cli.feature", "--help lists every entity subcommand")
def test_entity_help() -> None:
    pass


@pytest.mark.bdd
@scenario("features/entity_cli.feature", "No subcommand fails with argparse usage error")
def test_entity_no_subcommand() -> None:
    pass


@pytest.mark.bdd
@scenario("features/entity_cli.feature", "seed --dry-run reports the missing index")
def test_entity_seed_missing_index() -> None:
    pass
