"""pytest-bdd binding for transport_pool.feature (#provider-plugin-arch IM-1).

Steps live in ``tests/bdd/steps/transport_pool_steps.py`` and are
registered in the root ``conftest.py``'s ``pytest_plugins`` list.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "transport_pool.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "One hundred sequential embed calls reuse a single HTTP client")
def test_sequential_reuse_single_client() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Concurrent fan-out at concurrency ten still builds one client")
def test_concurrent_fan_out_one_client() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "The pooled client survives across distinct call types")
def test_pooled_client_across_call_types() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "The pool releases the client when the transport is closed")
def test_pool_releases_on_close() -> None:
    """Body populated by @scenario from the .feature file."""
