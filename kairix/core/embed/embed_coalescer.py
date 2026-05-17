"""In-process request coalescer for concurrent embed calls.

#288 — fold concurrent single-text embed calls into one batched Azure
HTTP request so N agents asking N different questions in the same
window pay one round-trip latency total instead of N.

Why this exists
---------------

Even with pool tuning (#280), query cache (#281) and embed cache (#285),
the ``embed_http`` stage at concurrency 10 has a long tail: mean ~379 ms,
worst-case ~1100 ms per query. The shape is N concurrent threads each
making independent embed HTTP calls and each paying the full Azure
roundtrip — sequential on the wire, parallel only on the client side.
Azure's embedding endpoint supports a batch ``input=`` list in a single
request, with the same per-request latency as a single text. Folding N
concurrent caller-threads into one batched request therefore turns N
roundtrips into one.

Architectural shape
-------------------

Each caller thread calls :meth:`EmbedCoalescer.embed` and **blocks** on
a per-request :class:`concurrent.futures.Future`. A single background
dispatcher thread drains the buffer when either of these fires:

  * the coalesce window expires (default 50 ms), or
  * the batch hits ``max_batch_size`` (default 16).

The dispatcher calls the injected ``embed_batch_fn`` with the list of
buffered texts and resolves each Future with its result (or ``[]`` on
error — the embed_text contract is "never raises, returns []").

Cache hits + empty-text checks happen **before** the coalescer — only
actual Azure-bound requests reach it. The caller-side wiring in
:mod:`kairix._azure` is responsible for those guards.

Test seam
---------

The constructor takes ``embed_batch_fn`` directly — F6-clean for tests
because tests construct the coalescer with a counting fake function
and a fast window. Production code wires the singleton via
:func:`get_embed_coalescer` which reads its tunables through
:mod:`kairix.paths` (F4-clean).

Behavioural invariants (pinned by tests)
----------------------------------------

1. Concurrent embeds collapse into one batched call within the window.
2. A single caller does not pay an unbounded wait — the window expires
   and the dispatcher fires with that lone request.
3. Empty / whitespace-only texts return ``[]`` without queueing.
4. Errors from the batch function propagate as ``[]`` per Future — the
   embed_text contract is "never raises".
5. :meth:`shutdown` is clean — pending Futures are resolved with ``[]``
   and the dispatcher thread joins promptly.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass

__all__ = [
    "DEFAULT_COALESCE_WINDOW_MS",
    "DEFAULT_MAX_BATCH_SIZE",
    "CoalescerStats",
    "EmbedCoalescer",
    "get_embed_coalescer",
    "reset_embed_coalescer",
]

logger = logging.getLogger(__name__)


DEFAULT_COALESCE_WINDOW_MS = 50
DEFAULT_MAX_BATCH_SIZE = 16


# F17: lift repeated literal action labels to module-level constants so
# the same identifier isn't duplicated across logs and stats.
_LOG_DISPATCH_ERROR = "embed_coalescer: dispatch failed: %s"


@dataclass(frozen=True)
class CoalescerStats:
    """Read-only snapshot of coalescer state for the onboard envelope.

    The ratio ``batches / requests`` is the operator-facing signal: a
    healthy coalescer at conc=10 should show batches well below
    requests (e.g. 1 batch for 10 requests = batch_factor 0.1).
    """

    requests: int
    batches: int
    largest_batch: int
    errors: int


class EmbedCoalescer:
    """Buffer concurrent embed calls; dispatch as a single batched request.

    Threading model:

    * Each :meth:`embed` call appends a ``(text, Future)`` pair under a
      lock, then blocks on the Future. The lock window is tight — just
      the append + a single ``notify`` to wake the dispatcher when
      either the window has expired or the batch is full.
    * A single background dispatcher thread owns the wait/notify loop.
      It wakes on either notify-from-caller (batch full) or timeout
      (window expired), drains the current batch under the lock, then
      releases the lock before calling ``embed_batch_fn`` so callers
      can keep enqueueing while the batch is in flight.
    * Per-request Futures decouple the dispatch from each caller. Any
      exception inside ``embed_batch_fn`` is caught and every Future
      in that batch is resolved with ``[]`` — the embed_text contract
      is "never raises, returns []".

    F2-clean test seam: tests construct
    ``EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=10,
    max_batch_size=5)`` directly. No env monkeypatching needed.
    """

    def __init__(
        self,
        embed_batch_fn: Callable[[list[str]], list[list[float]]],
        coalesce_window_ms: int = DEFAULT_COALESCE_WINDOW_MS,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
    ) -> None:
        # Window of 0 disables coalescing entirely — each call dispatches
        # immediately (synchronous fall-through). Useful for
        # low-concurrency deployments and for debugging.
        self._embed_batch_fn = embed_batch_fn
        self._window_s = max(0.0, coalesce_window_ms) / 1000.0
        self._max_batch_size = max(1, int(max_batch_size))

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending: list[tuple[str, Future[list[float]]]] = []
        self._stop = False

        # Observable counters — protected by ``_lock``.
        self._requests = 0
        self._batches = 0
        self._largest_batch = 0
        self._errors = 0

        # Dispatcher thread is daemon so a forgotten shutdown doesn't
        # block process exit. Production callers should still call
        # :meth:`shutdown` for clean drain of in-flight requests.
        self._dispatcher: threading.Thread | None = None
        if self._window_s > 0:
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name="embed-coalescer",
                daemon=True,
            )
            self._dispatcher.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """Submit ``text`` and block on the per-request Future.

        Empty / whitespace-only text returns ``[]`` without queueing —
        the embed_text contract short-circuits empty input before
        anything reaches the coalescer, but defence-in-depth here too.

        When the coalescer is disabled (window=0) the call dispatches
        synchronously through ``embed_batch_fn([text])`` — no
        background thread, no wait. This is the "low concurrency /
        debug" mode the brief calls out.
        """
        if not text or not text.strip():
            return []

        if self._window_s == 0:
            # Coalescing disabled — dispatch immediately, single-text batch.
            return self._dispatch_single_sync(text)

        future: Future[list[float]] = Future()
        with self._cv:
            was_empty = not self._pending
            self._pending.append((text, future))
            self._requests += 1
            # Two notify cases:
            #   (a) the buffer was empty — wake the dispatcher from its
            #       unbounded "no work yet" wait so the window timer can
            #       start ticking.
            #   (b) the buffer just hit max_batch_size — wake the
            #       dispatcher early so we don't pay the rest of the
            #       window when the batch is already full.
            if was_empty or len(self._pending) >= self._max_batch_size:
                self._cv.notify()

        # Block on the Future outside the lock so the dispatcher can drain.
        try:
            return future.result()
        except Exception:
            # Defence-in-depth: the dispatcher catches every error and
            # sets ``[]`` on each Future, so .result() should never
            # raise. If it somehow does (e.g. cancellation), honour
            # the contract.
            return []

    def stats(self) -> CoalescerStats:
        """Atomic snapshot of dispatch counters."""
        with self._lock:
            return CoalescerStats(
                requests=self._requests,
                batches=self._batches,
                largest_batch=self._largest_batch,
                errors=self._errors,
            )

    def shutdown(self) -> None:
        """Stop the dispatcher and resolve any pending Futures with ``[]``.

        Idempotent — calling twice is safe. Joins the dispatcher
        thread so the caller can be confident there are no leaked
        threads after return.
        """
        with self._cv:
            self._stop = True
            # Resolve any in-flight Futures so blocked callers unblock
            # promptly with the embed_text contract value [].
            for _text, future in self._pending:
                if not future.done():
                    future.set_result([])
            self._pending.clear()
            self._cv.notify_all()

        dispatcher = self._dispatcher
        if dispatcher is not None and dispatcher.is_alive():
            dispatcher.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Dispatcher (internal)
    # ------------------------------------------------------------------

    def _dispatch_loop(self) -> None:
        """Drain the buffer on window-expiry or batch-full wakeups.

        Held lock invariant: ``self._lock`` is owned for the buffer
        manipulation only — the actual ``embed_batch_fn`` call happens
        outside the lock so callers can keep enqueueing while the batch
        is in flight.
        """
        while True:
            with self._cv:
                if self._stop:
                    return
                if not self._pending:
                    # Wait for the first request to arrive, then start
                    # the window timer on the next iteration.
                    self._cv.wait()
                    if self._stop:
                        return
                    continue

                # Wait up to window_s for the batch to grow, OR for
                # max_batch_size to be hit (caller .notify()s in that
                # path). ``wait`` releases the lock for the duration.
                self._cv.wait(timeout=self._window_s)
                if self._stop:
                    return

                # Drain whatever has accumulated.
                batch = self._pending
                self._pending = []

            if batch:
                self._dispatch_batch(batch)

    def _dispatch_batch(self, batch: list[tuple[str, Future[list[float]]]]) -> None:
        """Call ``embed_batch_fn`` once with all texts; resolve each Future.

        Errors are caught and surface as ``[]`` per Future so the
        embed_text contract is honoured. The error counter is bumped
        for operator visibility.
        """
        texts = [t for t, _ in batch]
        results = self._safe_invoke_batch_fn(texts, batch)
        if results is None:
            self._resolve_batch_with_empty(batch)
            return

        # Defensive: if the backend returned a wrong-length list we
        # don't trust the partial alignment — every Future falls back
        # to []. This shouldn't happen with the Azure SDK but the
        # invariant is cheap to enforce.
        if len(results) != len(batch):
            self._record_batch_error(batch)
            self._resolve_batch_with_empty(batch)
            return

        self._record_batch_success(batch)
        for (_text, future), embedding in zip(batch, results, strict=False):
            if not future.done():
                # Defensive list() so a downstream caller mutating its
                # returned vector can't corrupt anything we still hold.
                future.set_result(list(embedding) if embedding else [])

    def _safe_invoke_batch_fn(
        self,
        texts: list[str],
        batch: list[tuple[str, Future[list[float]]]],
    ) -> list[list[float]] | None:
        """Invoke ``embed_batch_fn``; return ``None`` on exception.

        On exception the error counter is bumped and the warning is
        logged. The caller is responsible for resolving every Future
        in the batch with ``[]`` via :meth:`_resolve_batch_with_empty`.
        """
        try:
            return self._embed_batch_fn(texts)
        except Exception as exc:
            logger.warning(_LOG_DISPATCH_ERROR, exc)
            self._record_batch_error(batch)
            return None

    def _record_batch_error(self, batch: list[tuple[str, Future[list[float]]]]) -> None:
        """Bump the error + batch counters under the lock."""
        with self._lock:
            self._errors += 1
            self._batches += 1
            self._largest_batch = max(self._largest_batch, len(batch))

    def _record_batch_success(self, batch: list[tuple[str, Future[list[float]]]]) -> None:
        """Bump the batch counter for a successful dispatch."""
        with self._lock:
            self._batches += 1
            self._largest_batch = max(self._largest_batch, len(batch))

    @staticmethod
    def _resolve_batch_with_empty(
        batch: list[tuple[str, Future[list[float]]]],
    ) -> None:
        """Resolve every Future in the batch with ``[]``."""
        for _text, future in batch:
            if not future.done():
                future.set_result([])

    def _dispatch_single_sync(self, text: str) -> list[float]:
        """Synchronous single-text dispatch path used when window=0.

        Mirrors the error-handling discipline of the batched path:
        any exception in ``embed_batch_fn`` becomes ``[]``.
        """
        with self._lock:
            self._requests += 1

        try:
            results = self._embed_batch_fn([text])
        except Exception as exc:
            logger.warning(_LOG_DISPATCH_ERROR, exc)
            with self._lock:
                self._errors += 1
                self._batches += 1
                self._largest_batch = max(self._largest_batch, 1)
            return []

        with self._lock:
            self._batches += 1
            self._largest_batch = max(self._largest_batch, 1)

        if not results:
            return []
        first = results[0]
        return list(first) if first else []


# ---------------------------------------------------------------------------
# Process-shared singleton
# ---------------------------------------------------------------------------

_EMBED_COALESCER: EmbedCoalescer | None = None
_EMBED_COALESCER_LOCK = threading.Lock()


def get_embed_coalescer(
    embed_batch: Callable[[list[str]], list[list[float]]] | None = None,
) -> EmbedCoalescer | None:
    """Return the process-shared coalescer, building it lazily.

    Returns ``None`` when:

    * ``embed_batch`` is not provided AND no singleton has been built
      yet (callers in the test/sequential path leave the coalescer
      unwired), or
    * the configured window is 0 AND the singleton hasn't been built
      yet (the disabled path falls through synchronously and there's
      nothing for a coalescer to coalesce).

    The first non-None call constructs the singleton with tunables read
    from :func:`kairix.paths.read_int_env` (F4-clean) and returns it.
    Subsequent calls return the same instance regardless of arguments —
    the singleton is process-shared.

    The kwarg is named ``embed_batch`` (not ``embed_batch_fn``) on
    purpose: F6 forbids ``*_fn=None`` parameters because that's the
    test-substitution-via-default-None anti-pattern. Here the callable
    is the canonical batch dispatcher for the process-shared singleton,
    not a test hook — production callers pass the Azure dispatcher,
    tests pre-install their own EmbedCoalescer via setattr.
    """
    global _EMBED_COALESCER
    with _EMBED_COALESCER_LOCK:
        if _EMBED_COALESCER is not None:
            return _EMBED_COALESCER
        if embed_batch is None:
            return None
        from kairix.paths import read_int_env

        window_ms = read_int_env(
            "KAIRIX_EMBED_COALESCE_WINDOW_MS",
            default=DEFAULT_COALESCE_WINDOW_MS,
        )
        max_batch = read_int_env(
            "KAIRIX_EMBED_COALESCE_MAX_BATCH",
            default=DEFAULT_MAX_BATCH_SIZE,
        )
        # Clamp to documented ranges so an operator typo can't break
        # the coalescer; same defensive policy as embed_pool_size().
        window_ms = max(0, min(500, window_ms))
        max_batch = max(1, min(64, max_batch))
        _EMBED_COALESCER = EmbedCoalescer(
            embed_batch_fn=embed_batch,
            coalesce_window_ms=window_ms,
            max_batch_size=max_batch,
        )
        return _EMBED_COALESCER


def reset_embed_coalescer() -> None:
    """Drop the process-shared coalescer instance.

    Tests use this between cases instead of monkeypatching env vars
    (F2). Shuts down the dispatcher cleanly so the next
    :func:`get_embed_coalescer` builds a fresh one with current env
    settings.
    """
    global _EMBED_COALESCER
    with _EMBED_COALESCER_LOCK:
        if _EMBED_COALESCER is not None:
            _EMBED_COALESCER.shutdown()
        _EMBED_COALESCER = None
