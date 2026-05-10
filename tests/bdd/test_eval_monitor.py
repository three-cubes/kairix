"""pytest-bdd test module for eval_monitor.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "eval_monitor.feature")


@pytest.mark.bdd
@scenario(FEATURE, "First run with no previous log records does not flag a regression")
def test_first_run_no_regression() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "A drop beyond the alert threshold flags regression and names the baseline")
def test_drop_beyond_threshold_flags_regression() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "A drop within the alert threshold does not flag a regression")
def test_drop_within_threshold_no_regression() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "vec_failed_count reflects cases whose retrieval reported vector-search failure")
def test_vec_failed_count_reflects_failed_cases() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Each run appends an entry to the JSONL log")
def test_each_run_appends_log_entry() -> None:
    """Body populated by @scenario from the .feature file."""
