"""pytest-bdd test module for bootstrap.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "bootstrap.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Bootstrap returns the canonical orientation envelope for a known agent")
def test_bootstrap_returns_canonical_envelope_for_known_agent():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Bootstrap with a missing document root returns a structured error")
def test_bootstrap_missing_document_root_returns_structured_error():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Bootstrap envelope is JSON-serialisable")
def test_bootstrap_envelope_is_json_serialisable():
    """Body populated by @scenario from the .feature file."""
