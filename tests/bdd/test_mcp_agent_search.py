"""BDD test runner for MCP agent search tool scenarios."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "mcp_agent_search.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Agent receives structured results for a keyword query")
def test_keyword_query_returns_structured_results():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Agent receives empty results gracefully on unknown topic")
def test_unknown_topic_returns_empty():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Entity query returns entity graph result first")
def test_entity_query_returns_graph_first():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Search tool never raises to the caller")
def test_search_never_raises():
    """Body populated by @scenario from the .feature file."""
