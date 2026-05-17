"""pytest-bdd binding for transport_timeout.feature (#provider-plugin-arch IM-6).

Steps live in ``tests/bdd/steps/transport_timeout_steps.py`` and are
registered in the root ``conftest.py``'s ``pytest_plugins`` list.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "transport_timeout.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "A provider that responds within the timeout returns its result")
def test_responds_within_timeout() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A slow provider raises TimeoutExceeded to the caller")
def test_slow_provider_raises_timeout() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A timeout fires and releases the underlying socket")
def test_timeout_releases_socket() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Repeated timeouts do not accumulate leaked file descriptors")
def test_repeated_timeouts_no_leak() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Timeout policy can be tightened per call without rebuilding the transport")
def test_per_call_override() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A timeout during a coalesced batch fails each caller in the batch")
def test_coalesced_batch_timeout_fails_each_caller() -> None:
    """Body populated by @scenario from the .feature file."""
