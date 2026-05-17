"""pytest-bdd binding for embed_pool_config.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "embed_pool_config.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Default pool sizing applies when operator passes no configuration")
def test_default_pool_sizing() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Configured pool size flows through to the client")
def test_configured_pool_size_flows_through() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Configured keepalive count flows through to the client")
def test_configured_keepalive_flows_through() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Pool size and keepalive are independently configurable")
def test_pool_size_and_keepalive_independent() -> None:
    """Body populated by @scenario from the .feature file."""
