"""pytest-bdd binding for query_cache.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "query_cache.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "First call misses, second identical call hits the cache")
def test_first_miss_second_hit() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Whitespace and case differences collapse to the same cache slot")
def test_whitespace_case_normalisation() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Different agents share no cache slot for the same query")
def test_per_agent_cache_isolation() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Errors returned by the search pipeline are not cached")
def test_error_envelopes_are_not_cached() -> None:
    """Body populated by @scenario from the .feature file."""
