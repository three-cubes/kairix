"""Unit tests for kairix.core.embed.embed_cache.EmbedCache.

Every test is sabotage-proof: each assertion's comment names a
concrete mutation in production that would break the assertion, so a
future agent maintaining the cache can't accidentally regress a
behaviour by removing the protective code that the test depends on.

Tests use stdlib monkeypatch on ``time.time`` only — never on a
kairix internal (F1). No env-var monkeypatching (F2) — tests
construct ``EmbedCache(max_entries=N)`` directly or call
:func:`reset_embed_cache` to drop the singleton between cases.
"""

from __future__ import annotations

import threading

import pytest

from kairix.core.embed.embed_cache import (
    DEFAULT_MAX_AGE_S,
    DEFAULT_MAX_ENTRIES,
    EmbedCache,
    EmbedCacheStats,
    get_embed_cache,
    normalise_query,
    reset_embed_cache,
)

pytestmark = pytest.mark.unit


# Canonical fixture vectors — float lists shaped like text-embedding-3-large
# output but smaller so the tests stay fast. F17: lifted to constants so the
# literal doesn't repeat across multiple assertions.
_VEC_A: list[float] = [0.1, 0.2, 0.3]
_VEC_B: list[float] = [0.4, 0.5, 0.6]
_VEC_C: list[float] = [0.7, 0.8, 0.9]
_VEC_D: list[float] = [1.0, 1.1, 1.2]


def test_get_returns_none_on_miss() -> None:
    """Empty cache returns None and increments misses, not hits.

    Sabotage: remove the ``self._misses += 1`` line in EmbedCache.get
    and stats.misses stays at 0, but the operator-facing hit-rate then
    looks artificially high.
    """
    cache = EmbedCache()
    assert cache.get("hello") is None
    stats = cache.stats()
    assert stats.misses == 1
    assert stats.hits == 0


def test_put_then_get_returns_vector() -> None:
    """Round-trip: put a vector, get it back.

    Sabotage: change ``self._entries[key] = (now, stored)`` to drop
    the timestamp and the entry never lands in the dict — get returns
    None on the next read.
    """
    cache = EmbedCache()
    cache.put("hello", _VEC_A)
    assert cache.get("hello") == _VEC_A
    assert cache.stats().hits == 1


def test_get_returns_defensive_copy() -> None:
    """Mutating the returned list does not corrupt the cache.

    Sabotage: change ``return list(value)`` to ``return value`` and a
    caller mutating the returned vector poisons every subsequent
    reader — the second get() returns the mutated vector.
    """
    cache = EmbedCache()
    cache.put("hello", _VEC_A)
    out = cache.get("hello")
    assert out is not None
    out.append(99.0)  # mutate the returned list
    # Second read should see the original cached vector, not the mutation.
    out2 = cache.get("hello")
    assert out2 == _VEC_A


def test_put_defensive_copy_on_input() -> None:
    """Mutating the input list after put() does not corrupt the cache.

    Sabotage: change ``stored = list(embedding)`` to ``stored = embedding``
    and a caller mutating their list after a put() poisons the cache
    for every subsequent reader.
    """
    cache = EmbedCache()
    original = [0.1, 0.2, 0.3]
    cache.put("hello", original)
    original.append(99.0)  # mutate caller's list after put()
    # Cached vector should still be the original 3-element list.
    out = cache.get("hello")
    assert out == [0.1, 0.2, 0.3]


def test_normalisation_collapses_whitespace_and_case() -> None:
    """Trivially-different queries collapse to the same normalised slot.

    Sabotage: drop the normalise_query call in get/put and case
    variants get distinct cache slots — defeats the cache for any
    caller that capitalises differently.
    """
    cache = EmbedCache()
    cache.put("  Hello  WORLD  ", _VEC_A)
    assert cache.get("hello world") == _VEC_A
    assert cache.get("HELLO  WORLD") == _VEC_A


def test_empty_query_is_not_cached() -> None:
    """Empty / whitespace-only queries don't get cached and don't pollute stats.

    Sabotage: drop the ``if not query or not query.strip(): return``
    short-circuit in put() and the empty-string key lands in the
    cache — operators see stale entries with confusing empty keys.
    """
    cache = EmbedCache()
    cache.put("", _VEC_A)
    cache.put("   ", _VEC_B)
    assert cache.stats().size == 0


def test_empty_query_get_does_not_increment_stats() -> None:
    """Empty-query get short-circuits without touching stats.

    Sabotage: remove the empty-query short-circuit in get() and the
    miss counter gets bumped for every empty call — a caller that
    flushes the cache by repeatedly asking for "" inflates the
    apparent miss rate, hiding the real cache effectiveness.
    """
    cache = EmbedCache()
    cache.get("")
    cache.get("   ")
    stats = cache.stats()
    assert stats.hits == 0
    assert stats.misses == 0


def test_empty_embedding_is_not_cached() -> None:
    """An empty embedding (Azure call failed) is NOT cached.

    Sabotage: drop the ``if not embedding: return`` guard in put()
    and every transient Azure failure caches an empty list for 30
    minutes — agents asking the same text again get [] back from the
    cache instead of retrying the embed call.
    """
    cache = EmbedCache()
    cache.put("hello", [])
    assert cache.stats().size == 0
    # Next get should still be a miss, not a hit on the cached []
    assert cache.get("hello") is None


def test_eviction_when_max_entries_exceeded() -> None:
    """Filling past max_entries evicts the oldest entry and bumps evictions.

    Sabotage: remove the ``popitem(last=False)`` call and the cache
    grows unbounded — stats.evictions stays 0 and len(entries) > capacity.
    """
    cache = EmbedCache(max_entries=2)
    cache.put("a", _VEC_A)
    cache.put("b", _VEC_B)
    cache.put("c", _VEC_C)  # forces eviction of "a"
    assert cache.get("a") is None  # evicted
    assert cache.get("b") == _VEC_B
    assert cache.get("c") == _VEC_C
    assert cache.stats().evictions == 1


def test_age_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past max_age, an entry is treated as missing and counted as a miss.

    Sabotage: remove the ``if self._is_expired(...)`` branch in get()
    and stale entries are returned as hits — the operator-facing
    stats over-count freshness.
    """
    # Drive ``time.time`` from a list so we can advance the clock without
    # mutating any kairix internal. ``time`` is stdlib so F1 isn't engaged.
    fake_now = [1_000_000.0]

    def _fake_time() -> float:
        return fake_now[0]

    import time as _time

    monkeypatch.setattr(_time, "time", _fake_time)

    cache = EmbedCache(max_age_s=10.0)
    cache.put("hello", _VEC_A)
    assert cache.get("hello") == _VEC_A  # fresh

    fake_now[0] += 20.0  # advance past max_age
    assert cache.get("hello") is None  # expired → miss
    stats = cache.stats()
    assert stats.misses >= 1
    # Expired-and-evicted entry should not count as a hit.
    assert stats.hits == 1  # only the first (fresh) read counted


def test_lru_ordering_get_promotes_to_recent() -> None:
    """Access promotes an entry to MRU so it survives the next eviction.

    Sabotage: drop the ``self._entries.move_to_end(key)`` in get() and
    a recently-accessed entry gets evicted instead of the truly oldest.
    """
    cache = EmbedCache(max_entries=3)
    cache.put("a", _VEC_A)
    cache.put("b", _VEC_B)
    cache.put("c", _VEC_C)
    # Touch "a" so it becomes MRU.
    assert cache.get("a") == _VEC_A
    # Insert "d" — the LRU is now "b" (because "a" was just promoted).
    cache.put("d", _VEC_D)
    assert cache.get("b") is None  # b evicted, not a
    assert cache.get("a") == _VEC_A
    assert cache.get("d") == _VEC_D


def test_put_refreshes_existing_entry() -> None:
    """A second put() on an existing key refreshes the timestamp + value.

    Sabotage: remove the existing-key branch in put() and the second
    put inserts a new entry alongside the old one — stats.size grows
    and the eviction policy gets confused.
    """
    cache = EmbedCache(max_entries=3)
    cache.put("hello", _VEC_A)
    cache.put("hello", _VEC_B)  # refresh
    assert cache.get("hello") == _VEC_B
    assert cache.stats().size == 1


def test_stats_round_trip() -> None:
    """Stats reflect hits, misses, and evictions; hit_rate is derived correctly.

    Sabotage: change the hit_rate computation to ``hits / hits`` and
    the assertion against the known fraction fails.
    """
    cache = EmbedCache(max_entries=2)
    cache.put("a", _VEC_A)
    cache.put("b", _VEC_B)
    # 2 hits
    cache.get("a")
    cache.get("b")
    # 1 miss
    cache.get("missing")
    # 1 eviction (puts past capacity)
    cache.put("c", _VEC_C)
    stats = cache.stats()
    assert isinstance(stats, EmbedCacheStats)
    assert stats.hits == 2
    assert stats.misses == 1
    assert stats.evictions == 1
    assert stats.hit_rate == pytest.approx(2 / 3)


def test_concurrent_access_does_not_deadlock_and_counts_correctly() -> None:
    """5 threads doing 100 ops each: counters sum to ops, no exceptions raised.

    Sabotage: remove the ``threading.RLock`` and the counter writes
    race — total hits+misses drifts below the operation count under
    contention. With 500 total ops the drift is detectable.
    """
    cache = EmbedCache(max_entries=50)
    n_threads = 5
    ops_per_thread = 100
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        try:
            for i in range(ops_per_thread):
                # Mix puts and gets across a small key space so threads
                # collide on the lock — that's where a missing RLock
                # would bite.
                key = f"q-{i % 7}"
                if i % 2 == 0:
                    cache.put(key, [float(tid), float(i)])
                else:
                    cache.get(key)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent ops raised: {errors!r}"

    stats = cache.stats()
    # Each thread did ops_per_thread/2 gets and ops_per_thread/2 puts.
    # Puts never increment hits/misses; only gets do. So total
    # hits+misses must equal n_threads * (ops_per_thread // 2).
    expected_gets = n_threads * (ops_per_thread // 2)
    assert stats.hits + stats.misses == expected_gets


def test_normalisation_is_shared_with_query_cache() -> None:
    """The embed cache shares :func:`normalise_query` with the result cache.

    Sabotage: re-implement normalise_query locally and let the two
    diverge (e.g. one does ``.casefold()``, the other does ``.lower()``).
    Then a result-cache miss could re-embed text that's already in
    the embed cache (or vice versa) — the latency story breaks.
    """
    from kairix.core.search.query_cache import normalise_query as q_norm

    # Same callable — must literally be the same object so we anchor
    # the "single source of truth" invariant.
    assert normalise_query is q_norm


def test_defaults_match_documented_bounds() -> None:
    """Default constants match the values the dispatch brief committed to.

    Sabotage: change DEFAULT_MAX_ENTRIES from 1000 to e.g. 50 and the
    behavioural contract documented in the cache module + the
    KAIRIX_EMBED_CACHE_MAX_ENTRIES env var defaults drift apart.
    """
    assert DEFAULT_MAX_ENTRIES == 1000
    assert DEFAULT_MAX_AGE_S == 1800.0


def test_clear_resets_state() -> None:
    """clear() drops entries and zeroes counters.

    Sabotage: forget to zero ``_hits`` in clear() and tests that
    reuse a cache across cases see stats bleed across boundaries.
    """
    cache = EmbedCache()
    cache.put("a", _VEC_A)
    cache.get("a")
    cache.get("missing")  # miss
    cache.clear()
    stats = cache.stats()
    assert stats.size == 0
    assert stats.hits == 0
    assert stats.misses == 0
    assert stats.evictions == 0
    assert cache.get("a") is None


def test_oldest_entry_age_reflects_insertion_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stats.oldest_entry_age_s`` reports the age of the LRU entry.

    Sabotage: change ``oldest_age = max(0.0, time.time() - oldest_inserted_at)``
    to a constant (e.g. 0.0) and the operator dashboard reports every
    cache as freshly-warmed even when entries are minutes old.
    """
    fake_now = [1_000_000.0]

    def _fake_time() -> float:
        return fake_now[0]

    import time as _time

    monkeypatch.setattr(_time, "time", _fake_time)

    cache = EmbedCache(max_age_s=60.0)
    cache.put("hello", _VEC_A)
    fake_now[0] += 5.0
    stats = cache.stats()
    assert stats.oldest_entry_age_s == pytest.approx(5.0)


def test_get_embed_cache_returns_singleton() -> None:
    """get_embed_cache returns the same instance across calls.

    Sabotage: drop the ``if _EMBED_CACHE is None`` guard and every
    call rebuilds the cache, wiping accumulated state — the singleton
    invariant relied upon by the onboard check breaks.
    """
    reset_embed_cache()
    try:
        a = get_embed_cache()
        b = get_embed_cache()
        assert a is b
    finally:
        reset_embed_cache()


def test_reset_embed_cache_drops_singleton() -> None:
    """reset_embed_cache rebuilds on next access.

    Sabotage: have reset_embed_cache only call ``.clear()`` instead of
    nulling the singleton — a test that needed a fresh instance with
    different bounds (via env or direct construction) would keep
    seeing the old instance's settings.
    """
    reset_embed_cache()
    try:
        first = get_embed_cache()
        first.put("hello", _VEC_A)
        reset_embed_cache()
        second = get_embed_cache()
        assert second is not first
        # New instance starts empty.
        assert second.stats().size == 0
    finally:
        reset_embed_cache()
