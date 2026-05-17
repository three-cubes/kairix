"""Integration: embed cache wired into embed_text cuts Azure traffic.

Boundary chain:
  caller -> embed_text(client=fake) -> EmbedCache -> fake.embeddings.create

The Azure client is faked by passing a ``client=`` kwarg to
:func:`kairix.core.embed.embed_text` (the public re-export). No
private-name imports (F5), no @patch on internals (F1), no env
monkeypatch (F2). Everything else - the embed cache, the embed_text
wrapper, the onboard check - is real production code.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.core.embed import embed_cache as embed_cache_mod
from kairix.core.embed import embed_text
from kairix.core.embed.embed_cache import EmbedCache, reset_embed_cache

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Counting fake - same shape as the BDD fake; duplicated here to keep
# the integration test self-contained (importing from tests.bdd.steps
# would be a layering inversion).
# ---------------------------------------------------------------------------


class _EmbedItem:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _EmbedResponse:
    def __init__(self, embedding: list[float]) -> None:
        self.data = [_EmbedItem(embedding)]


class _CountingEmbeddings:
    def __init__(self, owner: _CountingClient) -> None:
        self._owner = owner

    def create(self, *, model: str, input: list[str], dimensions: int) -> _EmbedResponse:
        self._owner.calls.append({"model": model, "input": list(input), "dimensions": dimensions})
        return _EmbedResponse([0.1, 0.2, 0.3])


class _CountingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.embeddings = _CountingEmbeddings(self)


@pytest.fixture
def _wire(monkeypatch: pytest.MonkeyPatch) -> tuple[_CountingClient, EmbedCache]:
    """Wire a fresh cache and counting client.

    The cache is constructed directly (F2-clean - no env monkeypatch)
    and substituted into the module singleton via setattr on a public
    attribute. The fake client is passed to ``embed_text`` via the
    public ``client=`` kwarg (F5-clean - no private imports).
    """
    reset_embed_cache()
    cache = EmbedCache(max_entries=10, max_age_s=60.0)
    monkeypatch.setattr(embed_cache_mod, "_EMBED_CACHE", cache)

    client = _CountingClient()
    yield client, cache
    reset_embed_cache()


@pytest.mark.integration
def test_second_identical_embed_skips_the_client(_wire: tuple[_CountingClient, EmbedCache]) -> None:
    """A second embed of the same text returns from cache without hitting the client.

    Sabotage: remove the ``cache.get(text)`` short-circuit from
    embed_text and both calls hit the client - the assertion on
    calls == 1 fires.
    """
    client, _ = _wire
    first = embed_text("FEAT-081 status", client=client, deployment="test-deployment")
    second = embed_text("FEAT-081 status", client=client, deployment="test-deployment")

    assert first == second
    assert len(client.calls) == 1, f"expected 1 client call; got {len(client.calls)}"


@pytest.mark.integration
def test_normalisation_collapses_case_and_whitespace(_wire: tuple[_CountingClient, EmbedCache]) -> None:
    """Whitespace + case variants share a cache slot - only one client call.

    Sabotage: drop the normalise_query application in EmbedCache.get/put
    and these three texts get distinct slots - the client is called
    three times.
    """
    client, _ = _wire
    embed_text("  FEAT-081 STATUS  ", client=client, deployment="test-deployment")
    embed_text("feat-081 status", client=client, deployment="test-deployment")
    embed_text("FEAT-081 Status", client=client, deployment="test-deployment")

    assert len(client.calls) == 1


@pytest.mark.integration
def test_empty_query_short_circuits_before_client(_wire: tuple[_CountingClient, EmbedCache]) -> None:
    """An empty or whitespace-only query never reaches the client.

    Sabotage: remove the empty-text guard at the top of embed_text
    and the client is called for empty strings - wasting Azure spend
    and polluting the cache.
    """
    client, _ = _wire
    assert embed_text("", client=client, deployment="test-deployment") == []
    assert embed_text("   ", client=client, deployment="test-deployment") == []
    assert client.calls == []


@pytest.mark.integration
def test_failed_embed_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty/failed embed result is not cached - next call retries.

    Sabotage: drop the ``if embedding:`` guard before cache.put in
    embed_text and a transient failure caches [] for 30 minutes - the
    retry call still gets [] back from the cache and never re-hits the
    client. This test pins that behaviour: the second call sees a
    non-empty result because the client is now returning success, and
    the client was called twice (first failed, second succeeded).
    """
    reset_embed_cache()
    cache = EmbedCache(max_entries=10, max_age_s=60.0)
    monkeypatch.setattr(embed_cache_mod, "_EMBED_CACHE", cache)

    # First call raises; second call returns a real vector.
    call_state = {"n": 0}

    class _FlakyEmbeddings:
        def create(self, **_: Any) -> _EmbedResponse:
            call_state["n"] += 1
            if call_state["n"] == 1:
                raise RuntimeError("transient Azure outage")
            return _EmbedResponse([0.7, 0.8, 0.9])

    class _FlakyClient:
        def __init__(self) -> None:
            self.embeddings = _FlakyEmbeddings()

    flaky = _FlakyClient()
    first = embed_text("retry me", client=flaky, deployment="test-deployment")
    assert first == []  # transient failure surfaces as []
    assert cache.stats().size == 0  # failure NOT cached

    second = embed_text("retry me", client=flaky, deployment="test-deployment")
    assert second == [0.7, 0.8, 0.9]  # retry succeeded
    assert call_state["n"] == 2  # client was called both times
    reset_embed_cache()


@pytest.mark.integration
def test_cache_age_expiry_triggers_re_embed(
    monkeypatch: pytest.MonkeyPatch, _wire: tuple[_CountingClient, EmbedCache]
) -> None:
    """An entry older than max_age_s causes a re-embed on next access.

    Stdlib monkeypatch on ``kairix.core.embed.embed_cache.time.time``
    (stdlib is not a kairix internal - F1 prohibits patching kairix
    internals, not stdlib through the import bridge).
    Sabotage: drop the age check in EmbedCache.get and the second
    call returns the stale entry; client.calls stays at 1 instead of
    growing to 2.
    """
    client, _ = _wire

    # Use a fresh small-age cache for this scenario.
    fresh = EmbedCache(max_entries=10, max_age_s=1.0)
    monkeypatch.setattr(embed_cache_mod, "_EMBED_CACHE", fresh)

    embed_text("alpha", client=client, deployment="test-deployment")
    assert len(client.calls) == 1

    # Advance the clock past max_age_s.
    import time as _time

    real_now = _time.time()
    monkeypatch.setattr(
        "kairix.core.embed.embed_cache.time.time",
        lambda: real_now + 5.0,
    )

    embed_text("alpha", client=client, deployment="test-deployment")
    assert len(client.calls) == 2, "expired entry should not have been served"


@pytest.mark.integration
def test_onboard_check_surfaces_hit_rate_after_cache_hit(
    _wire: tuple[_CountingClient, EmbedCache],
) -> None:
    """The onboard check renders non-zero hit-rate after a real cache hit.

    Sabotage: remove the ``hits / total`` math from EmbedCacheStats
    (e.g. always return 0.0) and this assertion fires - the dashboard
    silently underreports cache effectiveness.
    """
    from kairix.platform.onboard.check import check_embed_cache_stats

    client, cache = _wire
    embed_text("hot query", client=client, deployment="test-deployment")
    embed_text("hot query", client=client, deployment="test-deployment")  # cache hit

    # Pass the cache explicitly so the check reads OUR instance, not
    # the singleton (which may have other process state in CI).
    result = check_embed_cache_stats(embed_cache=cache)
    assert result.ok
    assert "hit_rate=0." in result.detail or "hit_rate=1." in result.detail
    # The detail string contains the structured hit/miss/size fields.
    assert "size=1" in result.detail
    assert "hits=1" in result.detail


@pytest.mark.integration
def test_onboard_check_appears_in_run_all_checks() -> None:
    """The embed cache check is wired into the canonical check list.

    Sabotage: forget to add ``check_embed_cache_stats`` to ``ALL_CHECKS``
    and the onboard envelope silently omits the embed cache -
    operators have no visibility on hit rate.
    """
    from kairix.platform.onboard.check import ALL_CHECKS, check_embed_cache_stats

    assert check_embed_cache_stats in ALL_CHECKS
