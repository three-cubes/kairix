"""pytest-bdd binding for kairix_cli_top_level.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/kairix_cli_top_level.feature", "--help prints the subcommand list and exits 0")
def test_help_lists_subcommands() -> None:
    pass


@pytest.mark.bdd
@scenario("features/kairix_cli_top_level.feature", "-h is a synonym for --help")
def test_short_help_synonym() -> None:
    pass


@pytest.mark.bdd
@scenario("features/kairix_cli_top_level.feature", "--version prints the package version and exits 0")
def test_version_prints() -> None:
    pass


@pytest.mark.bdd
@scenario(
    "features/kairix_cli_top_level.feature",
    "An unknown subcommand exits non-zero with operator-actionable text",
)
def test_unknown_command_actionable_error() -> None:
    pass
