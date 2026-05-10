"""pytest-bdd test module for eval_judge.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "eval_judge.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Judge produces graded results when the model returns a valid response")
def test_judge_grades_valid_response() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Judge swallows backend errors and returns all-zero grades")
def test_judge_swallows_backend_errors() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Judge clamps out-of-range grades to the 0..2 rubric")
def test_judge_clamps_grades() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Calibration passes when anchors return their expected grades")
def test_calibration_passes() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Calibration fails when too many anchors return wrong grades")
def test_calibration_fails() -> None:
    """Body populated by @scenario from the .feature file."""
