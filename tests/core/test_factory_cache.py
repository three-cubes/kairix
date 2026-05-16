"""Unit tests for build_search_pipeline memoisation (#279).

Live profiling on v2026.5.16a3 showed each `build_search_pipeline()` call
costs ~2.3s + ~120 MB. The factory now memoises by config identity so
repeat calls in the same process return the cached instance instantly.
"""

from __future__ import annotations

import pytest

from kairix.core.factory import build_search_pipeline, reset_search_pipeline_cache

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_cache_between_cases() -> None:
    """Each test starts from a clean cache so cases don't bleed state."""
    reset_search_pipeline_cache()


def test_second_call_returns_same_instance() -> None:
    """Two calls with the same config (None) return the *same* pipeline object.

    Sabotage-proof: remove the cache check in build_search_pipeline and
    the identity assertion fails immediately.
    """
    p1 = build_search_pipeline()
    p2 = build_search_pipeline()
    assert p1 is p2, "build_search_pipeline should memoise identical-config calls"


def test_reset_clears_the_cache() -> None:
    """reset_search_pipeline_cache forces a fresh build on the next call.

    Tests rely on this when they need an uncontaminated pipeline.
    """
    p1 = build_search_pipeline()
    reset_search_pipeline_cache()
    p2 = build_search_pipeline()
    assert p1 is not p2, "after reset, the next call should build a fresh pipeline"


def test_different_config_objects_cache_separately() -> None:
    """Two distinct RetrievalConfig instances get distinct cached pipelines.

    A caller that overrides config is explicitly opting out of the shared
    default-config cache entry.
    """
    from kairix.core.search.config import RetrievalConfig

    cfg_a = RetrievalConfig.defaults()
    cfg_b = RetrievalConfig.defaults()
    p_a = build_search_pipeline(config=cfg_a)
    p_b = build_search_pipeline(config=cfg_b)
    assert p_a is not p_b, "different config instances must not share a cached pipeline"
    # But the SAME config instance hits the cache.
    p_a_again = build_search_pipeline(config=cfg_a)
    assert p_a_again is p_a, "same config instance should re-use the cached pipeline"
