"""Unit tests for kairix.core.search.query_cache.QueryResultCache (#281).

Each test is sabotage-proof: the comment names a concrete mutation
in production that would break the assertion, so a future agent
maintaining the cache can't accidentally regress a behaviour by
removing the protective code that the test depends on.

Tests use stdlib monkeypatch on ``time.time`` only — never on a
kairix internal (F1). Tuples and an in-memory cache instance are
everything else needed.
"""

from __future__ import annotations

import threading

import pytest

from kairix.core.search.query_cache import (
    DEFAULT_MAX_AGE_S,
    DEFAULT_MAX_ENTRIES,
    CacheStats,
    QueryResultCache,
    make_cache_key,
    normalise_query,
)

pytestmark = pytest.mark.unit


# Repeated cache-key fragments — extracted to satisfy F17 (no string
# literal ≥10 chars duplicated ≥3 times in a module).
_KEY_A = ("a", "shared", "", ())
_KEY_B = ("b", "shared", "", ())
_KEY_C = ("c", "shared", "", ())
_KEY_D = ("d", "shared", "", ())


def test_get_returns_none_on_miss() -> None:
    """Empty cache returns None and increments misses, not hits.

    Sabotage: remove the ``self._misses += 1`` line in QueryResultCache.get
    and stats.misses stays at 0, but the operator-facing hit-rate then
    looks artificially high.
    """
    cache = QueryResultCache()
    assert cache.get(_KEY_A) is None
    stats = cache.stats()
    assert stats.misses == 1
    assert stats.hits == 0


def test_put_then_get_returns_value() -> None:
    """Round-trip: put a value, get it back.

    Sabotage: change ``self._entries[key] = (now, value)`` to drop the
    timestamp and the entry never lands in the dict — get returns None
    on the next read.
    """
    cache = QueryResultCache()
    cache.put(_KEY_A, "result-a")
    assert cache.get(_KEY_A) == "result-a"
    assert cache.stats().hits == 1


def test_normalise_query_collapses_whitespace_and_case() -> None:
    """Trivially-different queries collapse to the same normalised form.

    Sabotage: drop the ``.lower()`` in normalise_query and case
    variants get distinct cache slots — defeats the cache for any
    agent that capitalises differently.
    """
    assert normalise_query("  Hello   WORLD  ") == normalise_query("hello world")
    assert normalise_query("\tFoo\n bar") == "foo bar"


def test_eviction_when_max_entries_exceeded() -> None:
    """Filling past max_entries evicts the oldest entry and bumps evictions.

    Sabotage: remove the ``popitem(last=False)`` call and the cache
    grows unbounded — stats.evictions stays 0 and len(entries) > capacity.
    """
    cache = QueryResultCache(max_entries=2)
    cache.put(_KEY_A, "va")
    cache.put(_KEY_B, "vb")
    cache.put(_KEY_C, "vc")  # forces eviction of A
    assert cache.get(_KEY_A) is None  # evicted
    assert cache.get(_KEY_B) == "vb"
    assert cache.get(_KEY_C) == "vc"
    assert cache.stats().evictions == 1


def test_age_expiry() -> None:
    """Past max_age, an entry is treated as missing and counted as a miss.

    Sabotage: remove the ``if self._is_expired(...)`` branch in get and
    stale entries are returned as hits — the operator-facing stats
    over-count freshness.

    Drives the cache's clock via the public ``clock`` kwarg — the
    production DI seam — instead of patching the stdlib ``time``
    module. Patching ``time.time`` is unreliable across this kind of
    refactor: the cache captures the production default at import
    time, so a later ``monkeypatch.setattr(time, "time", fake)`` is
    silently a no-op. The kwarg seam makes the controlled clock
    explicit and immune to that capture.
    """
    fake_now = [1_000_000.0]
    cache = QueryResultCache(max_age_s=10.0, clock=lambda: fake_now[0])

    cache.put(_KEY_A, "value-a")
    assert cache.get(_KEY_A) == "value-a"  # fresh

    fake_now[0] += 20.0  # advance past max_age
    assert cache.get(_KEY_A) is None  # expired → miss
    stats = cache.stats()
    assert stats.misses >= 1
    # Expired-and-evicted entry should not count as a hit.
    assert stats.hits == 1  # only the first (fresh) read counted


def test_lru_ordering_get_promotes_to_recent() -> None:
    """Access promotes an entry to MRU so it survives the next eviction.

    Sabotage: drop the ``self._entries.move_to_end(key)`` in get and a
    recently-accessed entry gets evicted instead of the truly oldest.
    """
    cache = QueryResultCache(max_entries=3)
    cache.put(_KEY_A, "va")
    cache.put(_KEY_B, "vb")
    cache.put(_KEY_C, "vc")
    # Touch A so it becomes MRU.
    assert cache.get(_KEY_A) == "va"
    # Insert D — the LRU is now B (because A was just promoted).
    cache.put(_KEY_D, "vd")
    assert cache.get(_KEY_B) is None  # B evicted, not A
    assert cache.get(_KEY_A) == "va"
    assert cache.get(_KEY_D) == "vd"


def test_stats_round_trip() -> None:
    """Stats reflect hits, misses, and evictions; hit_rate is derived correctly.

    Sabotage: change the hit_rate computation to ``hits / hits`` and
    the assertion against the known fraction fails.
    """
    cache = QueryResultCache(max_entries=2)
    cache.put(_KEY_A, "va")
    cache.put(_KEY_B, "vb")
    # 2 hits
    cache.get(_KEY_A)
    cache.get(_KEY_B)
    # 1 miss
    cache.get(_KEY_C)
    # 1 eviction (puts past capacity)
    cache.put(_KEY_C, "vc")
    stats = cache.stats()
    assert isinstance(stats, CacheStats)
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
    cache = QueryResultCache(max_entries=50)
    n_threads = 5
    ops_per_thread = 100
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        try:
            for i in range(ops_per_thread):
                # Mix puts and gets across a small key space so threads
                # collide on the lock — that's where a missing RLock
                # would bite.
                key = ("k", "shared", "", (str(i % 7),))
                if i % 2 == 0:
                    cache.put(key, f"v-{tid}-{i}")
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


def test_make_cache_key_deterministic() -> None:
    """Same inputs produce the same key; equivalent collection orders collapse.

    Sabotage: drop the ``sorted(collections)`` in make_cache_key and
    two callers passing ['a','b'] vs ['b','a'] miss each other's cache.
    """
    k1 = make_cache_key("Hello WORLD", "shared", "alpha", ["c1", "c2"])
    k2 = make_cache_key("hello world", "shared", "alpha", ["c2", "c1"])
    assert k1 == k2
    # Different agent → different key
    k3 = make_cache_key("hello world", "shared", "beta", ["c1", "c2"])
    assert k1 != k3
    # None vs "" agent collapses
    k_none = make_cache_key("q", "shared", None, None)
    k_empty = make_cache_key("q", "shared", "", None)
    assert k_none == k_empty


def test_defaults_match_documented_bounds() -> None:
    """Default constants match the values the dispatch brief committed to.

    Sabotage: change DEFAULT_MAX_ENTRIES from 500 to e.g. 50 and the
    behavioural contract documented in the cache module + the
    KAIRIX_QUERY_CACHE_MAX_ENTRIES env var defaults drift apart.
    """
    assert DEFAULT_MAX_ENTRIES == 500
    assert DEFAULT_MAX_AGE_S == 300.0


def test_clear_resets_state() -> None:
    """clear() drops entries and zeroes counters.

    Sabotage: forget to zero ``_hits`` in clear() and tests that reuse
    a cache across cases see stats bleed across boundaries.
    """
    cache = QueryResultCache()
    cache.put(_KEY_A, "va")
    cache.get(_KEY_A)
    cache.get(_KEY_B)  # miss
    cache.clear()
    stats = cache.stats()
    assert stats.size == 0
    assert stats.hits == 0
    assert stats.misses == 0
    assert stats.evictions == 0
    assert cache.get(_KEY_A) is None
