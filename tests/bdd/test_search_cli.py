"""pytest-bdd binding for search_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/search_cli.feature", "A successful search prints the intent and the top result")
def test_cli_human_output() -> None:
    pass


@pytest.mark.bdd
@scenario("features/search_cli.feature", "--json emits a machine-parseable JSON object with results array")
def test_cli_json_output() -> None:
    pass


@pytest.mark.bdd
@scenario("features/search_cli.feature", "--limit caps the number of results in the output")
def test_cli_limit_caps_results() -> None:
    pass


@pytest.mark.bdd
@scenario("features/search_cli.feature", "A pipeline error is reported and exits non-zero")
def test_cli_error_exits_nonzero() -> None:
    pass
