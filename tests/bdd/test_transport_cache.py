"""pytest-bdd binding for transport_cache.feature (#provider-plugin-arch IM-6).

Steps live in ``tests/bdd/steps/transport_cache_steps.py`` and are
registered in the root ``conftest.py``'s ``pytest_plugins`` list.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "transport_cache.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Asking for the same text twice serves the second call from cache")
def test_same_text_serves_from_cache() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Distinct texts produce distinct cache keys and two provider calls")
def test_distinct_texts_distinct_keys() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A cold cache delegates the first request straight to the provider")
def test_cold_cache_delegates() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "A mixed batch of cached and uncached texts only fetches the uncached ones")
def test_mixed_batch_splits() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Cache lookup on the read path returns the stored vector without provider involvement")
def test_warm_lookup_no_provider() -> None:
    """Body populated by @scenario from the .feature file."""
