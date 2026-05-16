"""pytest-bdd test module for worker.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "worker.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Worker status returns the structured envelope")
def test_worker_status_returns_envelope():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Worker pause writes the pause flag to the state file")
def test_worker_pause_writes_flag():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Worker resume clears the pause flag")
def test_worker_resume_clears_flag():
    """Body populated by @scenario from the .feature file."""
