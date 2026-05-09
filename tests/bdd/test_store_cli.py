"""pytest-bdd binding for store_cli.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/store_cli.feature", "A dry-run crawl prints counts without writing to Neo4j")
def test_store_dry_run_crawl() -> None:
    pass


@pytest.mark.bdd
@scenario("features/store_cli.feature", "store health --json emits structured output")
def test_store_health_json() -> None:
    pass


@pytest.mark.bdd
@scenario("features/store_cli.feature", "store with no subcommand prints help and exits non-zero")
def test_store_no_subcommand() -> None:
    pass
