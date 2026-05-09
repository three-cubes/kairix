"""pytest-bdd test module for search_planner.feature.

Operator-visible BDD scenarios:
  - Multi-hop comparison query → >=2 sub-queries
  - Simple single-topic query → 1 sub-query
  - Failing LLM → fallback to original query
"""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "search_planner.feature")


@pytest.mark.bdd
@scenario(FEATURE, "A multi-hop comparison query is decomposed into multiple sub-queries")
def test_multi_hop_decomposes() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "A simple single-topic query passes through unchanged")
def test_simple_passthrough() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "A failing LLM falls back to the original query")
def test_failing_llm_fallback() -> None:
    """Body populated by @scenario from the .feature file."""
