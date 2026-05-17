"""pytest-bdd binding for transport_retry.feature (#provider-plugin-arch IM-6).

Steps live in ``tests/bdd/steps/transport_retry_steps.py`` and are
registered in the root ``conftest.py``'s ``pytest_plugins`` list.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "transport_retry.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "A provider that succeeds on the first attempt is called once")
def test_success_on_first_attempt() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A provider that fails twice then succeeds is retried until success")
def test_fails_twice_then_succeeds() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A provider that exceeds max attempts surfaces RetryExhausted to the caller")
def test_exceeds_max_attempts_raises_exhausted() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A 4xx client error is not retried")
def test_client_error_not_retried() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Backoff inserts a measurable delay between retry attempts")
def test_backoff_measurable_delay() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Each retry attempt is logged with its attempt number")
def test_per_attempt_telemetry() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A 403 forbidden error short-circuits the retry path")
def test_403_short_circuits() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A 404 not-found error short-circuits the retry path")
def test_404_short_circuits() -> None:
    """Body populated by @scenario from the .feature file."""
