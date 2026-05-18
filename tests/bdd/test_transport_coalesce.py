"""pytest-bdd binding for transport_coalesce.feature (#provider-plugin-arch IM-6).

Steps live in ``tests/bdd/steps/transport_coalesce_steps.py`` and are
registered in the root ``conftest.py``'s ``pytest_plugins`` list.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "transport_coalesce.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Ten concurrent requests in one window collapse into one batched call")
def test_ten_concurrent_one_batch() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A lonely request waits up to the window then dispatches alone")
def test_lonely_request_waits_then_dispatches() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Seventeen concurrent requests with max batch sixteen split into two batches")
def test_seventeen_split_into_two_batches() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A coalescer with a zero window dispatches every request synchronously")
def test_zero_window_synchronous() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Distinct windows do not merge into a single batched call")
def test_distinct_windows_do_not_merge() -> None:
    """Body populated by @scenario from the .feature file."""
