"""pytest-bdd binding for embed_cache.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "embed_cache.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Identical queries from different agents share the embed cache")
def test_identical_queries_share_cache() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Whitespace and case differences collapse to the same cache slot")
def test_whitespace_case_normalisation() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Empty queries do not pollute the cache")
def test_empty_queries_skip_cache() -> None:
    """Body populated by @scenario from the .feature file."""
