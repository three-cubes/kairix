"""pytest-bdd binding for brief_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/brief_cli.feature", "An invalid agent name is rejected with a helpful stderr")
def test_brief_invalid_agent() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/brief_cli.feature", "Help text lists every valid agent")
def test_brief_help() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/brief_cli.feature", "A missing agent argument produces a usage error")
def test_brief_missing_agent() -> None:
    """Body populated by @scenario from the .feature file."""
