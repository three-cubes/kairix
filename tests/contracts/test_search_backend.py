"""Contract: SearchPipeline.search keeps its public call signature.

Checks that ``kairix.core.search.pipeline.SearchPipeline.search`` takes
``query`` first, plus ``agent`` (default ``None``) and ``budget``.
"""

import inspect

import pytest


@pytest.mark.contract
def test_pipeline_search_has_query_param():
    """SearchPipeline.search accepts 'query' parameter."""
    from kairix.core.search.pipeline import SearchPipeline

    sig = inspect.signature(SearchPipeline.search)
    assert "query" in sig.parameters


@pytest.mark.contract
def test_pipeline_search_has_agent_param():
    """SearchPipeline.search accepts 'agent' parameter."""
    from kairix.core.search.pipeline import SearchPipeline

    sig = inspect.signature(SearchPipeline.search)
    assert "agent" in sig.parameters


@pytest.mark.contract
def test_pipeline_search_agent_default_is_none():
    """SearchPipeline.search 'agent' defaults to None."""
    from kairix.core.search.pipeline import SearchPipeline

    sig = inspect.signature(SearchPipeline.search)
    assert sig.parameters["agent"].default is None


@pytest.mark.contract
def test_pipeline_search_has_budget_param():
    """SearchPipeline.search accepts 'budget' parameter (token budget)."""
    from kairix.core.search.pipeline import SearchPipeline

    sig = inspect.signature(SearchPipeline.search)
    assert "budget" in sig.parameters


@pytest.mark.contract
def test_pipeline_search_query_is_first_positional():
    """'query' is the first positional parameter of SearchPipeline.search (after self)."""
    from kairix.core.search.pipeline import SearchPipeline

    sig = inspect.signature(SearchPipeline.search)
    params = list(sig.parameters.keys())
    # First param is 'self', second should be 'query'
    assert params[1] == "query"
