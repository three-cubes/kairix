"""In-process query-result cache for the search pipeline (#281).

LRU bounded by entry count + per-entry max age. Thread-safe (kairix MCP
serves multiple agents concurrently). Cache key is the normalised
``(query, scope, agent, collections)`` four-tuple — the same query under
the same constraints returns the same result.

Cache hits sidestep the entire pipeline including the dominant Azure
embed HTTP cost (~240 ms on cache miss vs sub-millisecond on hit). In
teaming sessions where multiple agents ask near-duplicate questions
within the cache window, this is the highest-leverage Tier 1 lever
identified in :doc:`docs/architecture/teaming-concurrency-strategy.md`.

Design notes:

- ``OrderedDict`` backs the LRU. ``move_to_end(key)`` promotes on
  access; ``popitem(last=False)`` evicts the oldest entry when the
  bound is exceeded.
- Each entry stores ``(insertion_time_s, value)``. ``get`` checks age
  at read time so a stale-but-not-yet-evicted entry is reported as a
  miss (and the operator-facing hit/miss stats stay honest).
- A single :class:`threading.RLock` guards all reads + writes. The
  cost of contention here is dwarfed by the cost of the Azure embed
  roundtrip the cache avoids on a hit.
- Invalidation is process-restart-only for now. A future ticket may
  add cache-bust on embed/store-crawl mutation events; that is out of
  scope for #281.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_ENTRIES = 500
DEFAULT_MAX_AGE_S = 300.0  # 5 minutes


@dataclass(frozen=True)
class CacheStats:
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


class QueryResultCache:
    """Bounded LRU cache for SearchPipeline results.

    Key shape: tuple of ``(query_normalised, scope, agent, collections_tuple)``.
    Value: the :class:`SearchResult` instance returned by
    :meth:`SearchPipeline.search`. Age is checked at get-time — expired
    entries are removed and treated as misses (so stats reflect
    operator-facing reality, not raw-LRU shape).

    Thread safety: a single :class:`threading.RLock` guards all reads +
    writes. The cost of contention at this lock is negligible vs the
    cost of an Azure embed roundtrip we're avoiding on every hit.
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_age_s: float = DEFAULT_MAX_AGE_S,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._max_age_s = float(max_age_s)
        self._entries: OrderedDict[tuple[Any, ...], tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: tuple[Any, ...]) -> Any | None:
        """Return the cached value or ``None``. Expired entries miss.

        Promotes the entry to most-recently-used on a successful hit
        so the LRU ordering reflects access, not insertion.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            inserted_at, value = entry
            if self._is_expired(inserted_at):
                # Drop the expired entry on the floor and report a miss.
                # Operators reading stats want stale reads counted as
                # misses, not hits — caching a 5-minute-old answer is
                # the same outcome as recomputing it.
                del self._entries[key]
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return value

    def put(self, key: tuple[Any, ...], value: Any) -> None:
        """Insert or refresh an entry. Evicts the oldest when bounded."""
        with self._lock:
            now = time.time()
            if key in self._entries:
                # Existing key: refresh the timestamp and promote to MRU.
                self._entries[key] = (now, value)
                self._entries.move_to_end(key)
                return
            self._entries[key] = (now, value)
            if len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1

    def stats(self) -> CacheStats:
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
            return CacheStats(
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
        (out of scope for #281, but the surface is here for it).
        """
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def _is_expired(self, inserted_at: float) -> bool:
        """Internal age check — caller already holds the lock."""
        return (time.time() - inserted_at) > self._max_age_s


def normalise_query(query: str) -> str:
    """Case-fold + collapse whitespace so trivially-different queries share cache slots.

    ``"  Hello   WORLD  "`` and ``"hello world"`` produce the same
    normalised form. Anything beyond casing + whitespace (synonyms,
    punctuation, paraphrases) is intentionally NOT collapsed — the
    cache pretends nothing it can't prove will return the same answer.
    """
    return " ".join(query.lower().split())


def make_cache_key(
    query: str,
    scope: Any,
    agent: str | None,
    collections: list[str] | None,
) -> tuple[Any, ...]:
    """Build the canonical 4-tuple key.

    Tuples are hashable so the LRU's ``OrderedDict`` can key on them
    directly. ``collections`` is sorted before tupling so callers that
    pass equivalent lists in different orders hit the same slot.
    ``agent=None`` and ``agent=""`` collapse to the same key — both
    mean "no agent supplied".
    """
    return (
        normalise_query(query),
        scope,
        agent or "",
        tuple(sorted(collections)) if collections else (),
    )
