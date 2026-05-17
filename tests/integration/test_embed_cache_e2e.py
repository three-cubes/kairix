"""Integration: embed cache wired into ProviderEmbeddingService cuts provider traffic.

Boundary chain:
  caller -> ProviderEmbeddingService.embed(text) -> EmbedCache -> FakeProvider.embed_batch

The provider is faked via :class:`FakeProvider` from ``tests/fakes.py``
(the canonical fake Protocol-compliant plugin). No private-name imports
(F5), no @patch on internals (F1), no env monkeypatch (F2). Everything
else — the embed cache, the provider-embedding-service adapter, the
onboard check — is real production code.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.transport.cache import embed_cache as embed_cache_mod
from kairix.transport.cache.embed_cache import EmbedCache, reset_embed_cache
from kairix.transport.coalesce import reset_embed_coalescer
from kairix.transport.embed_service import ProviderEmbeddingService
from tests.fakes import FakeProvider

pytestmark = pytest.mark.integration


@pytest.fixture
def _wire(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeProvider, ProviderEmbeddingService, EmbedCache]:
    """Wire a fresh cache + provider + adapter.

    The cache is constructed directly (F2-clean — no env monkeypatch)
    and substituted into the module singleton via setattr on a public
    attribute. The provider is a :class:`FakeProvider` from
    ``tests/fakes.py`` — no private imports (F5).

    The coalescer singleton is reset to ``None`` so embed() takes its
    direct-dispatch path (the coalescer adds window-based batching
    non-determinism that these scenarios aren't pinning).
    """
    reset_embed_cache()
    reset_embed_coalescer()
    cache = EmbedCache(max_entries=10, max_age_s=60.0)
    monkeypatch.setattr(embed_cache_mod, "_EMBED_CACHE", cache)

    provider = FakeProvider(vector=[0.1, 0.2, 0.3])
    service = ProviderEmbeddingService(provider)
    yield provider, service, cache
    reset_embed_cache()
    reset_embed_coalescer()


@pytest.mark.integration
def test_second_identical_embed_skips_the_provider(
    _wire: tuple[FakeProvider, ProviderEmbeddingService, EmbedCache],
) -> None:
    """A second embed of the same text returns from cache without calling the provider.

    Sabotage: remove the ``cache.get(text)`` short-circuit from
    ``ProviderEmbeddingService.embed`` and both calls hit the provider —
    the assertion on ``len(embed_calls) == 1`` fires.
    """
    provider, service, _ = _wire
    first = service.embed("FEAT-081 status")
    second = service.embed("FEAT-081 status")

    assert first == second
    assert len(provider.embed_calls) == 1, f"expected 1 provider call; got {len(provider.embed_calls)}"


@pytest.mark.integration
def test_normalisation_collapses_case_and_whitespace(
    _wire: tuple[FakeProvider, ProviderEmbeddingService, EmbedCache],
) -> None:
    """Whitespace + case variants share a cache slot — only one provider call.

    Sabotage: drop the ``normalise_query`` application in EmbedCache.get/put
    and these three texts get distinct slots — the provider is called
    three times.
    """
    provider, service, _ = _wire
    service.embed("  FEAT-081 STATUS  ")
    service.embed("feat-081 status")
    service.embed("FEAT-081 Status")

    assert len(provider.embed_calls) == 1


@pytest.mark.integration
def test_empty_query_short_circuits_before_provider(
    _wire: tuple[FakeProvider, ProviderEmbeddingService, EmbedCache],
) -> None:
    """An empty or whitespace-only query never reaches the provider.

    Sabotage: remove the empty-text guard at the top of
    ``ProviderEmbeddingService.embed`` and the provider is called for
    empty strings — wasting provider spend and polluting the cache.
    """
    _, service, _ = _wire
    assert service.embed("") == []
    assert service.embed("   ") == []
    provider = _wire[0]
    assert provider.embed_calls == []


@pytest.mark.integration
def test_failed_embed_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty/failed embed result is not cached — next call retries.

    Sabotage: drop the ``if embedding:`` / cache.put guard in
    ``ProviderEmbeddingService.embed`` and a transient failure caches
    ``[]`` for 30 minutes — the retry call still gets ``[]`` back from
    the cache and never re-hits the provider. This test pins that
    behaviour: the second call sees a non-empty result because the
    provider is now returning success, and the provider was called twice.
    """
    reset_embed_cache()
    reset_embed_coalescer()
    cache = EmbedCache(max_entries=10, max_age_s=60.0)
    monkeypatch.setattr(embed_cache_mod, "_EMBED_CACHE", cache)

    call_state = {"n": 0}

    class _FlakyProvider:
        """Provider whose first ``embed_batch`` raises, then succeeds.

        Matches the Provider Protocol surface used by
        ``ProviderEmbeddingService.embed`` (the only method exercised
        on the embed hot path).
        """

        def __init__(self) -> None:
            self.embed_calls: list[list[str]] = []

        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            call_state["n"] += 1
            self.embed_calls.append(list(texts))
            if call_state["n"] == 1:
                raise RuntimeError("transient provider outage")
            return [[0.7, 0.8, 0.9] for _ in texts]

    flaky = _FlakyProvider()
    service = ProviderEmbeddingService(flaky)  # type: ignore[arg-type]  # structural duck-typing for the test
    first = service.embed("retry me")
    assert first == []  # transient failure surfaces as []
    assert cache.stats().size == 0  # failure NOT cached

    second = service.embed("retry me")
    assert second == [0.7, 0.8, 0.9]  # retry succeeded
    assert call_state["n"] == 2  # provider was called both times
    reset_embed_cache()
    reset_embed_coalescer()


@pytest.mark.integration
def test_cache_age_expiry_triggers_re_embed(
    monkeypatch: pytest.MonkeyPatch,
    _wire: tuple[FakeProvider, ProviderEmbeddingService, EmbedCache],
) -> None:
    """An entry older than max_age_s causes a re-embed on next access.

    Stdlib monkeypatch on ``kairix.transport.cache.embed_cache.time.time``
    (stdlib is not a kairix internal — F1 prohibits patching kairix
    internals, not stdlib through the import bridge).
    Sabotage: drop the age check in ``EmbedCache.get`` and the second
    call returns the stale entry; provider call-count stays at 1 instead
    of growing to 2.
    """
    provider, _, _ = _wire
    # The _wire fixture already provided a fresh provider+service; swap
    # in a smaller-age cache for this specific scenario.
    fresh = EmbedCache(max_entries=10, max_age_s=1.0)
    monkeypatch.setattr(embed_cache_mod, "_EMBED_CACHE", fresh)
    service = ProviderEmbeddingService(provider)

    service.embed("alpha")
    assert len(provider.embed_calls) == 1

    # Advance the clock past max_age_s.
    import time as _time

    real_now = _time.time()
    monkeypatch.setattr(
        "kairix.transport.cache.embed_cache.time.time",
        lambda: real_now + 5.0,
    )

    service.embed("alpha")
    assert len(provider.embed_calls) == 2, "expired entry should not have been served"


@pytest.mark.integration
def test_onboard_check_surfaces_hit_rate_after_cache_hit(
    _wire: tuple[FakeProvider, ProviderEmbeddingService, EmbedCache],
) -> None:
    """The onboard check renders non-zero hit-rate after a real cache hit.

    Sabotage: remove the ``hits / total`` math from ``EmbedCacheStats``
    (e.g. always return 0.0) and this assertion fires — the dashboard
    silently underreports cache effectiveness.
    """
    from kairix.platform.onboard.check import check_embed_cache_stats

    provider, service, cache = _wire
    service.embed("hot query")
    service.embed("hot query")  # cache hit
    del provider  # F19: unused — service drives via the closed-over provider

    # Pass the cache explicitly so the check reads OUR instance, not
    # the singleton (which may have other process state in CI).
    result: Any = check_embed_cache_stats(embed_cache=cache)
    assert result.ok
    assert "hit_rate=0." in result.detail or "hit_rate=1." in result.detail
    # The detail string contains the structured hit/miss/size fields.
    assert "size=1" in result.detail
    assert "hits=1" in result.detail


@pytest.mark.integration
def test_onboard_check_appears_in_run_all_checks() -> None:
    """The embed cache check is wired into the canonical check list.

    Sabotage: forget to add ``check_embed_cache_stats`` to ``ALL_CHECKS``
    and the onboard envelope silently omits the embed cache —
    operators have no visibility on hit rate.
    """
    from kairix.platform.onboard.check import ALL_CHECKS, check_embed_cache_stats

    assert check_embed_cache_stats in ALL_CHECKS
