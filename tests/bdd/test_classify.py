"""pytest-bdd test module for classify.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "classify.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Content with strong domain signals classifies into the matching collection")
def test_content_with_strong_signals_classifies():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Explicit type override beats the rule-based auto-classifier")
def test_explicit_type_override_beats_rule_classifier():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Classify with an unknown agent returns a structured error")
def test_classify_with_unknown_agent_returns_structured_error():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Rule classifier raises ValueError — CLI exits 1 with structured error envelope")
def test_rule_value_error_exits_1():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Rule classifier raises a generic exception — CLI masks the message")
def test_rule_generic_exception_masks_message():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, 'LLM fallback engages when the rule classifier returns "unknown"')
def test_llm_fallback_when_rule_unknown():
    """Body populated by @scenario from the .feature file."""
