"""pytest-bdd binding for curator_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/curator_cli.feature", "--help lists the curator subcommands")
def test_curator_help() -> None:
    pass


@pytest.mark.bdd
@scenario("features/curator_cli.feature", "No subcommand fails with argparse usage error")
def test_curator_no_subcommand() -> None:
    pass


@pytest.mark.bdd
@scenario("features/curator_cli.feature", "health --format json emits structured output with ok + total_entities")
def test_curator_health_json() -> None:
    pass
