"""Per-directory fixtures for kairix core tests.

Resets the build_search_pipeline memoisation cache between tests so
factory-state pollution from earlier cases doesn't shape later
assertions (#279).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_search_pipeline_cache() -> None:
    """Each test in tests/core/ starts with a clean factory cache."""
    from kairix.core.factory import reset_search_pipeline_cache

    reset_search_pipeline_cache()
    yield
    reset_search_pipeline_cache()
