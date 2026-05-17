"""In-process embed cache for the embed roundtrip.

Lives in :mod:`kairix.transport.cache` — the universal endpoint
response cache. See docs/architecture/provider-plugin-architecture.md
for the three-layer split (core / transport / providers); this module
is the transport-layer cache that sits in front of every provider's
embed call, not a domain concern of any single provider.

LRU bounded by entry count + per-entry max age. Thread-safe (kairix
MCP serves multiple agents concurrently). Cache key is the normalised
query text — the same text embeds to the same vector regardless of
which agent / scope / collection asked for it, so this cache fills
the gap left by the result cache (#281), which keys on the full
``(query, scope, agent, collections)`` four-tuple and therefore misses
when two agents ask the same question from different scopes.

Hit value: ~5 ms memory lookup vs ~250-500 ms embed roundtrip (and
~1 s at conc=10). Even at conc=10 today's p95 = 3107 ms because
fresh queries still pay the full embed cost; this cache aims to take
that cost off the hot path whenever the SAME text has been embedded
recently.

Design notes:

- ``OrderedDict`` backs the LRU. ``move_to_end(key)`` promotes on
  access; ``popitem(last=False)`` evicts the oldest entry when the
  bound is exceeded. Same shape as
  :class:`kairix.core.search.query_cache.QueryResultCache`.
- Each entry stores ``(insertion_time_s, embedding)``. ``get`` checks
  age at read time so a stale-but-not-yet-evicted entry is reported as
  a miss (operator-facing stats stay honest).
- A single :class:`threading.RLock` guards all reads + writes. The
  cost of contention is dwarfed by the cost of the embed roundtrip
  the cache avoids on a hit.
- Default max-age is 30 min — longer than the result cache's 5 min
  because embed vectors depend only on the model + text, not on
  changing vault state. Two agents asking the same question 20 min
  apart will share an embedding even though their result sets differ.
- Invalidation is process-restart-only. A future ticket may add
  cache-bust on model-version change; that is out of scope here.
- :func:`normalise_query` is re-used from
  :mod:`kairix.core.search.query_cache` rather than re-defined — the
  result cache and the embed cache MUST agree on what "same text"
  means, or a result-cache miss could re-embed text that's already
  in the embed cache (and vice versa).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

# Re-export normalise_query so consumers (tests, integration code) can
# import it from a single canonical location regardless of which cache
# layer they are operating on. The result cache and the embed cache
# share the same normalisation rules — by re-exporting we anchor that
# invariant in code.
from kairix.core.search.query_cache import normalise_query

__all__ = [
    "DEFAULT_MAX_AGE_S",
    "DEFAULT_MAX_ENTRIES",
    "EmbedCache",
    "EmbedCacheStats",
    "get_embed_cache",
    "install_embed_cache",
    "normalise_query",
    "reset_embed_cache",
]

DEFAULT_MAX_ENTRIES = 1000
DEFAULT_MAX_AGE_S = 1800.0  # 30 minutes — embeddings depend on model + text only.


@dataclass(frozen=True)
class EmbedCacheStats:
    """Read-only snapshot of cache state for the onboard envelope.

    ``hit_rate`` is the convenience derivative used by the onboard
    JSON envelope; ``0.0`` when no queries have run yet so operators
    see "no data" rather than NaN.
    """

    size: int
    hits: int
    misses: int
    evictions: int
    oldest_entry_age_s: float
    hit_rate: float  # 0.0 to 1.0


class EmbedCache:
    """Bounded LRU cache keyed on normalised query text → embedding vector.

    Mirrors :class:`kairix.core.search.query_cache.QueryResultCache`
    exactly in shape, but keyed on just the text — so two agents asking
    the same question from different scopes / collections share the
    expensive embed roundtrip even though they don't share a final
    search result.

    Thread safety: a single :class:`threading.RLock` guards all reads +
    writes. Contention cost here is negligible vs the embed roundtrip
    we avoid on every hit.
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_age_s: float = DEFAULT_MAX_AGE_S,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._max_age_s = float(max_age_s)
        self._entries: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, query: str) -> list[float] | None:
        """Return the cached embedding or ``None``. Expired entries miss.

        Promotes the entry to most-recently-used on a successful hit
        so the LRU ordering reflects access, not insertion. Empty /
        whitespace-only queries are reported as misses without
        consulting the table — they never get cached either (see
        :meth:`put`), so this short-circuit keeps the lock window
        tight on that path.
        """
        if not query or not query.strip():
            return None
        key = normalise_query(query)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            inserted_at, value = entry
            if self._is_expired(inserted_at):
                # Drop the expired entry on the floor and report a miss.
                # Operators reading stats want stale reads counted as
                # misses, not hits — re-embedding is the same outcome as
                # serving stale text.
                del self._entries[key]
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            # Defensive copy so a caller mutating the returned list
            # can't corrupt the cached vector for the next reader.
            return list(value)

    def put(self, query: str, embedding: list[float]) -> None:
        """Insert or refresh an entry. Evicts the oldest when bounded.

        Empty / whitespace-only queries and empty embeddings are NOT
        cached — caching ``[]`` would lock the "embed failed" outcome
        in front of every same-text caller until the entry ages out.
        """
        if not query or not query.strip():
            return
        if not embedding:
            return
        key = normalise_query(query)
        with self._lock:
            now = time.time()
            # Defensive copy so the cache owns its own list and a
            # caller mutating the original argument after put() can't
            # change what we hand out on the next get().
            stored = list(embedding)
            if key in self._entries:
                # Existing key: refresh the timestamp and promote to MRU.
                self._entries[key] = (now, stored)
                self._entries.move_to_end(key)
                return
            self._entries[key] = (now, stored)
            if len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1

    def stats(self) -> EmbedCacheStats:
        """Return an atomic snapshot of cache state."""
        with self._lock:
            size = len(self._entries)
            oldest_age = 0.0
            if size > 0:
                # OrderedDict keeps insertion order; LRU oldest is the
                # leftmost entry. ``next(iter(...))`` is O(1) so the
                # lock window stays tight.
                oldest_key = next(iter(self._entries))
                oldest_inserted_at, _ = self._entries[oldest_key]
                oldest_age = max(0.0, time.time() - oldest_inserted_at)
            total = self._hits + self._misses
            hit_rate = (self._hits / total) if total > 0 else 0.0
            return EmbedCacheStats(
                size=size,
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                oldest_entry_age_s=oldest_age,
                hit_rate=hit_rate,
            )

    def clear(self) -> None:
        """Drop every cached entry and reset counters.

        Used by tests between cases and by any future cache-bust event
        (e.g. embed-model version change — out of scope here).
        """
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def _is_expired(self, inserted_at: float) -> bool:
        """Internal age check — caller already holds the lock."""
        return (time.time() - inserted_at) > self._max_age_s


# ---------------------------------------------------------------------------
# Process-shared singleton
# ---------------------------------------------------------------------------

_EMBED_CACHE: EmbedCache | None = None
_EMBED_CACHE_LOCK = threading.Lock()


def get_embed_cache() -> EmbedCache:
    """Return the process-shared :class:`EmbedCache`, building it lazily.

    Bounds are read from env vars on first construction:
      - ``KAIRIX_EMBED_CACHE_MAX_ENTRIES`` (int, default 1000)
      - ``KAIRIX_EMBED_CACHE_MAX_AGE_S`` (float seconds, default 1800)

    F4-clean: env reads route through :mod:`kairix.paths`.
    """
    global _EMBED_CACHE
    with _EMBED_CACHE_LOCK:
        if _EMBED_CACHE is None:
            from kairix.paths import read_float_env, read_int_env

            max_entries = read_int_env("KAIRIX_EMBED_CACHE_MAX_ENTRIES", default=DEFAULT_MAX_ENTRIES)
            max_age_s = read_float_env("KAIRIX_EMBED_CACHE_MAX_AGE_S", default=DEFAULT_MAX_AGE_S)
            _EMBED_CACHE = EmbedCache(max_entries=max_entries, max_age_s=max_age_s)
        return _EMBED_CACHE


def reset_embed_cache() -> None:
    """Drop the process-shared cache instance.

    Tests use this between cases instead of monkey-patching env vars
    (F2). After ``reset_embed_cache()`` the next :func:`get_embed_cache`
    call rebuilds the cache fresh — so a test wanting a smaller bound
    can set the env var, call reset, then call get; but the
    *recommended* pattern is to construct ``EmbedCache(max_entries=N)``
    directly and skip the singleton entirely.
    """
    global _EMBED_CACHE
    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE = None


def install_embed_cache(cache: EmbedCache | None) -> None:
    """Install ``cache`` as the process-shared singleton.

    Pass an :class:`EmbedCache` (or ``None`` to clear) and the next
    :func:`get_embed_cache` returns it. Tests use this to inject a
    pre-built cache with custom bounds through the public write
    accessor instead of reassigning the module attribute.
    """
    global _EMBED_CACHE
    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE = cache
