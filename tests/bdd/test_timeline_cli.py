"""pytest-bdd binding for timeline_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/timeline_cli.feature", "--help lists every documented flag")
def test_timeline_help() -> None:
    pass


@pytest.mark.bdd
@scenario("features/timeline_cli.feature", "Missing query argument fails with argparse usage error")
def test_timeline_no_args() -> None:
    pass


@pytest.mark.bdd
@scenario("features/timeline_cli.feature", "Invalid --since date is rejected with a clear error")
def test_timeline_invalid_since() -> None:
    pass


@pytest.mark.bdd
@scenario("features/timeline_cli.feature", "Invalid --type choice rejected by argparse")
def test_timeline_invalid_type() -> None:
    pass
