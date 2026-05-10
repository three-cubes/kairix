"""pytest-bdd binding for embed_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/embed_cli.feature", "--help lists every documented subcommand")
def test_embed_help() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/embed_cli.feature", "embed --help documents every flag")
def test_embed_subcommand_help() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/embed_cli.feature", "An unknown subcommand fails with argparse usage error")
def test_embed_unknown_subcommand() -> None:
    """Body populated by @scenario from the .feature file."""
