"""pytest-bdd test module for warm.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "warm.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Warm reports per-step success in the envelope")
def test_warm_reports_per_step_success():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Warm continues past a failing step and reports the failure")
def test_warm_continues_past_failure():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Warm is idempotent — second call after success is cheap")
def test_warm_second_call_is_cheap():
    """Body populated by @scenario from the .feature file."""
