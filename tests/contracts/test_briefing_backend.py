"""Contract: the briefing pipeline entry point keeps its public shape.

Checks that ``kairix.agents.briefing.pipeline.generate_briefing`` exists,
is callable, and accepts an ``agent`` parameter.
"""

import inspect

import pytest


@pytest.mark.contract
def test_generate_briefing_is_callable():
    """kairix.agents.briefing.pipeline.generate_briefing exists and is callable."""
    from kairix.agents.briefing.pipeline import generate_briefing

    assert callable(generate_briefing)


@pytest.mark.contract
def test_generate_briefing_accepts_agent_param():
    """generate_briefing accepts an 'agent' parameter."""
    from kairix.agents.briefing.pipeline import generate_briefing

    sig = inspect.signature(generate_briefing)
    assert "agent" in sig.parameters
