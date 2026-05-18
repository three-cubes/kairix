"""Unit tests for build_search_pipeline memoisation (#279).

Each `build_search_pipeline()` call costs ~2.3s + ~120 MB. The factory
memoises by config identity so repeat calls in the same process return
the cached instance instantly.

After v2026.5.17 the factory requires an explicit provider (the legacy
fallback was removed), so every test here passes ``provider="fake"``
and a ``FakeProviderRegistry`` so the factory's required-provider gate
doesn't fire — the memoisation behaviour, not the provider identity,
is what these scenarios pin.
"""

from __future__ import annotations

import pytest

from kairix.core.factory import build_search_pipeline, reset_search_pipeline_cache
from kairix.core.search.config import RetrievalConfig
from tests.fakes import FakeProvider, FakeProviderRegistry

pytestmark = pytest.mark.unit


def _registry() -> FakeProviderRegistry:
    """Production-shaped FakeProviderRegistry for memoisation tests."""
    return FakeProviderRegistry({"fake": FakeProvider(name="fake", vector=[0.1, 0.2, 0.3], dim=3)})


def _fake_cfg(**overrides: object) -> RetrievalConfig:
    """``RetrievalConfig.defaults()`` with ``provider="fake"`` and any overrides."""
    base = RetrievalConfig.defaults()
    from dataclasses import replace

    return replace(base, provider="fake", **overrides)  # type: ignore[arg-type] — dataclasses.replace accepts **kwargs but mypy can't narrow ``object`` to the per-field types


@pytest.fixture(autouse=True)
def _clear_cache_between_cases() -> None:
    """Each test starts from a clean cache so cases don't bleed state."""
    reset_search_pipeline_cache()


def test_second_call_returns_same_instance() -> None:
    """Two calls with the same config return the *same* pipeline object.

    Sabotage-proof: remove the cache check in build_search_pipeline and
    the identity assertion fails immediately.
    """
    cfg = _fake_cfg()
    registry = _registry()
    p1 = build_search_pipeline(config=cfg, registry=registry)
    p2 = build_search_pipeline(config=cfg, registry=registry)
    assert p1 is p2, "build_search_pipeline should memoise identical-config calls"


def test_reset_clears_the_cache() -> None:
    """reset_search_pipeline_cache forces a fresh build on the next call.

    Tests rely on this when they need an uncontaminated pipeline.
    """
    cfg = _fake_cfg()
    registry = _registry()
    p1 = build_search_pipeline(config=cfg, registry=registry)
    reset_search_pipeline_cache()
    p2 = build_search_pipeline(config=cfg, registry=registry)
    assert p1 is not p2, "after reset, the next call should build a fresh pipeline"


def test_equal_config_values_share_cache_across_distinct_instances() -> None:
    """Two RetrievalConfig instances with identical field values share one pipeline.

    Critical for the benchmark path: ``_retrieve_hybrid`` constructs a fresh
    config via ``resolve_retrieval_config`` per case. With object-identity
    cache keys, this would miss the cache on every call and rebuild the
    pipeline 200 times per benchmark run. With value-based hashing (frozen
    dataclass), equal configs collapse to one cached pipeline.

    Sabotage-proof: change the cache key back to ``id(config)`` and this test
    fails because the two equal configs get distinct pipelines.
    """
    cfg_a = _fake_cfg()
    cfg_b = _fake_cfg()
    assert cfg_a == cfg_b, "test premise: equal cfg constructors return equal values"
    registry = _registry()
    p_a = build_search_pipeline(config=cfg_a, registry=registry)
    p_b = build_search_pipeline(config=cfg_b, registry=registry)
    assert p_a is p_b, "equal configs from distinct instances should share the cached pipeline"


def test_different_config_values_cache_separately() -> None:
    """Two RetrievalConfig instances with different field values DON'T share.

    Caller that overrides a setting (e.g. fusion_strategy) gets its own
    cache entry so the override actually takes effect.
    """
    cfg_default = _fake_cfg()
    cfg_no_vector = _fake_cfg(skip_vector=True)
    assert cfg_default != cfg_no_vector, "test premise: differing field → unequal configs"
    registry = _registry()
    p_default = build_search_pipeline(config=cfg_default, registry=registry)
    p_no_vector = build_search_pipeline(config=cfg_no_vector, registry=registry)
    assert p_default is not p_no_vector, "different configs must get different cached pipelines"
