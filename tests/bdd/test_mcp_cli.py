"""pytest-bdd binding for mcp_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/mcp_cli.feature", "--help lists the serve subcommand")
def test_mcp_help() -> None:
    pass


@pytest.mark.bdd
@scenario("features/mcp_cli.feature", "serve --help documents every transport choice")
def test_mcp_serve_help() -> None:
    pass


@pytest.mark.bdd
@scenario("features/mcp_cli.feature", "No subcommand prints help and exits non-zero")
def test_mcp_no_subcommand() -> None:
    pass


@pytest.mark.bdd
@scenario("features/mcp_cli.feature", "serve rejects an unknown transport via argparse")
def test_mcp_serve_invalid_transport() -> None:
    pass
