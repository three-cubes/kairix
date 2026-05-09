"""pytest-bdd binding for setup_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/setup_cli.feature", "--help documents every flag operators need")
def test_setup_help() -> None:
    pass


@pytest.mark.bdd
@scenario("features/setup_cli.feature", "An invalid preset is rejected by argparse")
def test_setup_invalid_preset() -> None:
    pass


@pytest.mark.bdd
@scenario("features/setup_cli.feature", "--non-interactive --json --preset emits a JSON config to stdout")
def test_setup_non_interactive_json() -> None:
    pass
