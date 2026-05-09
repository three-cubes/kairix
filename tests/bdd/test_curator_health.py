"""pytest-bdd test module for curator_health.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "curator_health.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Healthy graph passes all checks")
def test_healthy_graph():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Unavailable Neo4j returns graceful report")
def test_unavailable_neo4j():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Entity with missing vault_path is flagged")
def test_missing_vault_path():
    """Body populated by @scenario from the .feature file."""
