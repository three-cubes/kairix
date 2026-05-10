"""pytest-bdd binding for setup_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/setup_cli.feature", "--help documents every flag operators need")
def test_setup_help() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/setup_cli.feature", "An invalid preset is rejected by argparse")
def test_setup_invalid_preset() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(
    "features/setup_cli.feature",
    "--non-interactive --json --preset emits a JSON config rooted at the supplied path",
)
def test_setup_non_interactive_json() -> None:
    """Body populated by @scenario from the .feature file."""
