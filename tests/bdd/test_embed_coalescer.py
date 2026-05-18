"""pytest-bdd binding for embed_coalescer.feature (#288)."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "embed_coalescer.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Ten concurrent embed calls collapse to one batched HTTP request")
def test_ten_concurrent_collapse_to_one_batch() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Sequential embed calls fire immediately within the bounded window")
def test_sequential_within_bounded_window() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Empty input bypasses the coalescer entirely")
def test_empty_input_bypasses() -> None:
    """Body populated by @scenario from the .feature file."""
