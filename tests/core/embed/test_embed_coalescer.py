"""Unit tests for kairix.transport.coalesce.EmbedCoalescer (#288).

Every test is sabotage-proof: each assertion's comment names a concrete
mutation in production that would break the assertion, so a future
agent maintaining the coalescer can't accidentally regress a behaviour
by removing the protective code that the test depends on.

F2-clean: tests construct ``EmbedCoalescer(embed_batch_fn=fake,
coalesce_window_ms=10, max_batch_size=5)`` directly. No env
monkeypatching. F1-clean: no @patch on kairix internals.
"""

from __future__ import annotations

import threading
import time

import pytest

# Imported from the defining module (not the package re-export) so the
# mutation-parity import graph (scripts/checks/mutation_parity.py) selects
# this file as embed_coalescer.py's own test when mutating that module.
from kairix.transport.coalesce.embed_coalescer import (
    DEFAULT_COALESCE_WINDOW_MS,
    DEFAULT_MAX_BATCH_SIZE,
    CoalescerStats,
    EmbedCoalescer,
    get_embed_coalescer,
    reset_embed_coalescer,
)

pytestmark = pytest.mark.unit


# F17: lift repeated short vectors to module-level constants so the
# literal isn't duplicated across multiple assertions.
_VEC_A: list[float] = [0.1, 0.2, 0.3]
_VEC_B: list[float] = [0.4, 0.5, 0.6]


class _CountingBatchFn:
    """Records every batch invocation so tests can pin call count + sizes.

    Returns a unique vector per text so per-request resolution can be
    verified independently of the call count.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._lock = threading.Lock()

    def __call__(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self.calls.append(list(texts))
        # Deterministic per-text vector so callers can verify the
        # right Future got the right result.
        return [[float(len(t)), 1.0, 2.0] for t in texts]


# ---------------------------------------------------------------------------
# Invariant 1: concurrent embeds collapse into a single batched call
# ---------------------------------------------------------------------------


def test_concurrent_embeds_collapse_to_one_batch() -> None:
    """Ten threads in the same window cause one batched call.

    Sabotage: in ``_dispatch_loop`` change the ``wait(timeout=window_s)``
    to ``wait(timeout=0)`` and each request fires its own batch — the
    call count grows past 1 and this assertion fires.
    """
    fake = _CountingBatchFn()
    # Long-ish window so all 10 threads land in the same batch even on
    # a slow CI box. max_batch_size=64 lets all 10 land in one batch.
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=200, max_batch_size=64)
    try:
        results: list[list[float]] = [[] for _ in range(10)]
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                results[i] = coalescer.embed(f"text-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"workers raised: {errors!r}"
        # Sabotage: if the dispatcher fires per-request the call count
        # explodes to 10 and this assertion fires.
        assert len(fake.calls) == 1, f"expected 1 batched call; got {len(fake.calls)}: {fake.calls!r}"
        assert len(fake.calls[0]) == 10, f"expected 10 texts in the one batch; got {len(fake.calls[0])}"
        # Each caller gets back its own embedding.
        for i, vec in enumerate(results):
            assert vec, f"worker {i} got empty result"
    finally:
        coalescer.shutdown()


# ---------------------------------------------------------------------------
# Invariant 2: a single caller pays the window only, not an unbounded wait
# ---------------------------------------------------------------------------


def test_single_caller_unblocks_within_bounded_window() -> None:
    """One caller, batch never fills — dispatcher must fire on timeout.

    Sabotage: remove the ``wait(timeout=self._window_s)`` argument and
    leave a bare ``wait()`` — the dispatcher never wakes for a single
    caller and the .result() blocks forever. Pytest would time out
    instead of returning quickly.
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=100, max_batch_size=16)
    try:
        start = time.monotonic()
        out = coalescer.embed("only-one")
        elapsed_ms = (time.monotonic() - start) * 1000
        assert out, "single caller should get a non-empty result"
        # The load-bearing, host-immune proof is the FLOOR: the caller
        # waited at least ~the window (so the dispatcher fired on the
        # window timeout, not instantly), and it returned at all. F82
        # (#493): the old ``< 1000`` ceiling was a flaky wall-clock bound;
        # the "never wakes ⇒ blocks forever" sabotage is caught
        # deterministically by the suite-level ``--timeout`` (the
        # ``embed`` call hangs, pytest-timeout kills it) — not by an
        # elapsed-time ceiling that measures host scheduling.
        assert elapsed_ms >= 80, f"expected to wait at least ~window_ms; got {elapsed_ms:.1f}ms"
    finally:
        coalescer.shutdown()


# ---------------------------------------------------------------------------
# Invariant 3: empty input bypasses the coalescer
# ---------------------------------------------------------------------------


def test_empty_text_bypasses_queue() -> None:
    """Empty string returns [] without queueing.

    Sabotage: remove the ``if not text or not text.strip()`` guard and
    the empty string lands in the buffer — the dispatcher fires a
    batch containing it, call count > 0 and this assertion fires.
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=50, max_batch_size=16)
    try:
        assert coalescer.embed("") == []
        assert coalescer.embed("   ") == []
        # Wait past the window — no dispatch should ever fire.
        time.sleep(0.15)
        assert fake.calls == [], f"empty inputs should not reach the batch fn; got {fake.calls!r}"
        # Stats should not count empty inputs as requests.
        stats = coalescer.stats()
        assert stats.requests == 0
        assert stats.batches == 0
    finally:
        coalescer.shutdown()


# ---------------------------------------------------------------------------
# Invariant 4: errors propagate as [] per Future
# ---------------------------------------------------------------------------


def test_batch_fn_exception_returns_empty_per_caller() -> None:
    """Azure 429 (or any exception) becomes [] per caller, not a raise.

    Sabotage: remove the try/except in ``_dispatch_batch`` and the
    exception poisons every blocked Future — callers see the
    exception propagate instead of the contracted [] return.
    """

    def boom(texts: list[str]) -> list[list[float]]:
        del texts
        raise RuntimeError("Azure 429: rate limited")

    coalescer = EmbedCoalescer(embed_batch_fn=boom, coalesce_window_ms=20, max_batch_size=16)
    try:
        out_a = coalescer.embed("a")
        out_b = coalescer.embed("b")
        assert out_a == []
        assert out_b == []
        stats = coalescer.stats()
        assert stats.errors >= 1
    finally:
        coalescer.shutdown()


def test_batch_fn_wrong_length_response_returns_empty() -> None:
    """A backend returning fewer/more vectors than texts → [] per Future.

    Sabotage: remove the ``len(results) != len(batch)`` check and the
    zip would partially align — some callers would get the wrong
    embedding (or an IndexError further down).
    """

    def short_response(texts: list[str]) -> list[list[float]]:
        del texts
        return [_VEC_A]  # Returns 1 vector regardless of input length.

    coalescer = EmbedCoalescer(
        embed_batch_fn=short_response,
        coalesce_window_ms=50,
        max_batch_size=16,
    )
    try:
        results: list[list[float]] = [[] for _ in range(3)]

        def worker(i: int) -> None:
            results[i] = coalescer.embed(f"text-{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All three callers get [] when the backend response mis-aligns.
        for i, vec in enumerate(results):
            assert vec == [], f"worker {i} got {vec!r}; expected []"
        assert coalescer.stats().errors >= 1
    finally:
        coalescer.shutdown()


# ---------------------------------------------------------------------------
# Invariant 5: shutdown is clean — pending Futures unblock
# ---------------------------------------------------------------------------


def test_shutdown_releases_pending_futures() -> None:
    """Calling shutdown() with pending requests resolves them with [].

    Sabotage: remove the ``for _text, future in self._pending: ...``
    cleanup loop in shutdown() and a caller blocked on a Future after
    shutdown waits forever (or for the default Future timeout).
    """
    slow_event = threading.Event()

    def slow_batch(texts: list[str]) -> list[list[float]]:
        # Block until the test releases the event — simulates an
        # in-flight Azure call that we never want to complete.
        slow_event.wait(timeout=5.0)
        return [[1.0] for _ in texts]

    coalescer = EmbedCoalescer(
        embed_batch_fn=slow_batch,
        coalesce_window_ms=10,
        max_batch_size=16,
    )

    # Submit a request, then shutdown before the window expires +
    # before the slow_batch returns.
    results: list[list[float]] = [[]]

    def worker() -> None:
        results[0] = coalescer.embed("pending")

    t = threading.Thread(target=worker)
    t.start()

    # Give the worker a moment to enqueue.
    time.sleep(0.005)
    coalescer.shutdown()
    slow_event.set()
    t.join(timeout=2.0)
    # If shutdown didn't release the Future, the worker would still
    # be blocked and t.is_alive() would be True.
    assert not t.is_alive(), "shutdown failed to release pending Future"


def test_shutdown_joins_dispatcher_thread() -> None:
    """shutdown() joins the dispatcher so no thread leaks past return.

    Sabotage: remove the ``self._dispatcher.join(timeout=2.0)`` call
    and a forgotten coalescer leaks a background thread per construction.
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=50, max_batch_size=16)
    dispatcher = coalescer._dispatcher  # access for verification only
    assert dispatcher is not None
    coalescer.shutdown()
    # The dispatcher should have terminated by the time shutdown returns.
    assert not dispatcher.is_alive(), "dispatcher thread did not exit after shutdown"


def test_shutdown_is_idempotent() -> None:
    """Calling shutdown twice is safe.

    Sabotage: remove the ``if self._stop`` early-return at the top of
    ``_dispatch_loop`` and the dispatcher might miss the second
    notify_all and hang on join.
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=50, max_batch_size=16)
    coalescer.shutdown()
    coalescer.shutdown()  # second call must not raise


# ---------------------------------------------------------------------------
# Window = 0 disables the coalescer (synchronous fall-through)
# ---------------------------------------------------------------------------


def test_window_zero_dispatches_synchronously() -> None:
    """window=0 mode bypasses the queue entirely — call fires now.

    Sabotage: drop the ``if self._window_s == 0`` short-circuit and
    a window=0 caller would block on the queue forever (no
    dispatcher thread is started in that mode).
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=0, max_batch_size=16)
    # The deterministic, host-immune proof that window=0 dispatches
    # synchronously is structural: NO dispatcher thread is started in
    # window=0 mode, so ``embed`` cannot enqueue-and-wait — it must run
    # the synchronous single-text path inline. F82 (#493): the old
    # ``elapsed_ms < 50`` ceiling was a flaky wall-clock bound that added
    # nothing over this structural assertion.
    assert coalescer._dispatcher is None

    out = coalescer.embed("hello")
    assert out, "window=0 should still return a result"
    # Each call is its own batch in window=0 mode (synchronous path).
    assert len(fake.calls) == 1
    assert fake.calls[0] == ["hello"]


def test_window_zero_empty_text_short_circuits() -> None:
    """Empty text in window=0 mode still returns [] without dispatch.

    Sabotage: move the empty-text guard below the window=0 check and
    a window=0 caller could ship an empty string to the backend.
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=0, max_batch_size=16)
    assert coalescer.embed("") == []
    assert fake.calls == []


def test_window_zero_exception_returns_empty() -> None:
    """In window=0 mode, batch fn exceptions still return [].

    Sabotage: remove the try/except in ``_dispatch_single_sync`` and a
    window=0 caller would see the exception propagate.
    """

    def boom(texts: list[str]) -> list[list[float]]:
        del texts
        raise RuntimeError("transient outage")

    coalescer = EmbedCoalescer(embed_batch_fn=boom, coalesce_window_ms=0, max_batch_size=16)
    assert coalescer.embed("hello") == []
    assert coalescer.stats().errors == 1


def test_window_zero_empty_response_returns_empty() -> None:
    """In window=0 mode, an empty backend response yields [].

    Sabotage: drop the ``if not results`` guard in
    ``_dispatch_single_sync`` and the IndexError propagates.
    """

    def empty(texts: list[str]) -> list[list[float]]:
        del texts
        return []

    coalescer = EmbedCoalescer(embed_batch_fn=empty, coalesce_window_ms=0, max_batch_size=16)
    assert coalescer.embed("hello") == []


# ---------------------------------------------------------------------------
# Per-request result resolution
# ---------------------------------------------------------------------------


def test_each_future_gets_its_own_embedding() -> None:
    """Two concurrent callers each receive their own backend-assigned vector.

    Sabotage: swap ``zip(batch, results)`` to ``zip(results, batch)``
    in ``_dispatch_batch`` and Futures resolve with the wrong vectors;
    this assertion fires because the deterministic per-text vector
    won't match the caller's text.
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=100, max_batch_size=16)
    try:
        results: dict[str, list[float]] = {}
        lock = threading.Lock()

        def worker(text: str) -> None:
            vec = coalescer.embed(text)
            with lock:
                results[text] = vec

        texts = ["short", "much-longer-text"]
        threads = [threading.Thread(target=worker, args=(t,)) for t in texts]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        # The fake returns ``[len(text), 1.0, 2.0]`` so a misalignment
        # is detectable by checking the first element matches len.
        assert results["short"][0] == float(len("short"))
        assert results["much-longer-text"][0] == float(len("much-longer-text"))
    finally:
        coalescer.shutdown()


# ---------------------------------------------------------------------------
# Max-batch wakes the dispatcher early
# ---------------------------------------------------------------------------


def test_max_batch_full_wakes_dispatcher_early() -> None:
    """Filling the batch wakes the dispatcher before the window expires.

    Sabotage: remove the ``if len(self._pending) >= self._max_batch_size:
    self._cv.notify()`` and the dispatcher waits the full window even
    when the batch is full — defeats the latency benefit at high
    concurrency.

    F82 rewrite (#493): the old shape asserted ``elapsed_ms < 400``, a
    wall-clock ceiling that flaked under concurrent-gate CPU contention
    (the 3 worker threads can be scheduled late on a loaded host even
    when the early-wake path fired correctly). The deterministic property
    is liveness, not latency: with a window set FAR longer than any test
    budget (10s), the ONLY way all three callers can unblock promptly is
    the batch-full ``notify()`` — a full-window wait would block them for
    the entire 10s. So we set the long window and assert every worker
    thread *finishes* (its blocking ``embed`` returned) within a generous
    2s liveness join — which is 5x the contended-scheduling slack but
    still 5x shorter than the window, so it passes on any host when
    early-wake works and HANGS (join times out, ``is_alive()`` stays
    True) when the notify is removed. This is a liveness floor, not a
    tight per-commit ceiling, and it is not an ``assert elapsed < const``
    shape.

    Sabotage-proof (executed): removed the batch-full ``self._cv.notify()``
    in ``EmbedCoalescer.embed`` → the workers blocked on the 10s window,
    the 2s join timed out, ``alive`` was non-empty and this test failed.
    Restoring the notify restored green.
    """
    fake = _CountingBatchFn()
    # Window FAR longer than the liveness join below, so a prompt return
    # can ONLY come from the max-batch notify path — never the window
    # timeout. (A full-window wait would block the workers for 10s.)
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=10_000, max_batch_size=3)
    try:
        results: list[list[float]] = [[] for _ in range(3)]

        def worker(i: int) -> None:
            results[i] = coalescer.embed(f"text-{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        # Liveness floor: every worker must UNBLOCK well inside the 10s
        # window. 2s is huge slack for thread scheduling under load yet
        # 5x shorter than the window — so this can only pass via the
        # early batch-full wake, and times out (hangs) if it's removed.
        for t in threads:
            t.join(timeout=2.0)
        alive = [i for i, t in enumerate(threads) if t.is_alive()]

        assert not alive, (
            f"batch-full must wake the dispatcher early; workers {alive} were still blocked "
            "on the coalesce window after 2s (notify path broken)"
        )
        # The batch-full path drains exactly the one full batch of 3.
        assert len(fake.calls) == 1
        assert len(fake.calls[0]) == 3
    finally:
        coalescer.shutdown()


def test_already_full_batch_dispatches_without_waiting_the_window() -> None:
    """A batch that is already full when the dispatcher reaches its window
    wait is dispatched at once — never parked for the full window.

    Regression (lost wakeup, PLA-472 / #810 CI): the window wait was an
    unconditional ``wait(timeout=window)``. When the batch-full ``notify()``
    landed while the dispatcher was not yet in that wait (e.g. still waking
    from the "no work yet" wait), the notify was lost and the full batch sat
    out the whole window. ``max_batch_size=1`` makes it deterministic: the
    single caller's notify is always consumed by the first wait, so the
    pre-fix dispatcher ALWAYS waited the full 10s window here.

    Sabotage-proof (executed): restore the unconditional
    ``self._cv.wait(timeout=self._window_s)`` → the worker is still blocked
    after the 2s liveness join and this test fails. Restored.
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=10_000, max_batch_size=1)
    try:
        result: list[list[float]] = []
        worker = threading.Thread(target=lambda: result.append(coalescer.embed("only-text")))
        worker.start()
        worker.join(timeout=2.0)

        assert not worker.is_alive(), "a full batch must not wait out the 10s coalesce window"
        assert len(fake.calls) == 1
        assert fake.calls[0] == ["only-text"]
    finally:
        coalescer.shutdown()


# ---------------------------------------------------------------------------
# Stats round-trip
# ---------------------------------------------------------------------------


def test_stats_tracks_requests_and_batches() -> None:
    """Stats counters reflect requests and batches dispatched.

    Sabotage: remove the ``self._requests += 1`` inside embed() and
    operator visibility on coalescer throughput dies — dashboards show
    zero traffic.
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=100, max_batch_size=16)
    try:
        # 3 concurrent → one batch.
        results: list[list[float]] = [[] for _ in range(3)]

        def worker(i: int) -> None:
            results[i] = coalescer.embed(f"t-{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = coalescer.stats()
        assert isinstance(stats, CoalescerStats)
        assert stats.requests == 3
        assert stats.batches == 1
        assert stats.largest_batch == 3
        assert stats.errors == 0
    finally:
        coalescer.shutdown()


# ---------------------------------------------------------------------------
# Thread-safety stress: 100 threads x 10 calls each
# ---------------------------------------------------------------------------


def test_high_concurrency_no_exceptions_and_batches_below_naive() -> None:
    """100 threads x 10 calls -- every Future resolves, batches < 1000.

    Sabotage: drop the lock around ``self._pending.append`` and the
    list mutation races, leading to occasional lost requests or
    crashes. The 1000-batch upper bound also catches a sabotage
    where the dispatcher fires per-request (1000 calls → 1000 batches).
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=20, max_batch_size=32)
    try:
        n_threads = 100
        ops_per_thread = 10
        errors: list[BaseException] = []
        results: list[list[float]] = []
        results_lock = threading.Lock()

        def worker(tid: int) -> None:
            try:
                for j in range(ops_per_thread):
                    vec = coalescer.embed(f"thread-{tid}-call-{j}")
                    with results_lock:
                        results.append(vec)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"workers raised: {errors!r}"
        total_ops = n_threads * ops_per_thread
        assert len(results) == total_ops
        # Every Future resolved with a non-empty vector.
        for vec in results:
            assert vec, "got empty result from concurrent embed"
        stats = coalescer.stats()
        assert stats.requests == total_ops
        # Coalescing must have done *something* — batches strictly less
        # than total ops. A naive (per-request) dispatch would be 1000.
        assert stats.batches < total_ops, f"expected coalescing; got {stats.batches} batches for {total_ops} requests"
    finally:
        coalescer.shutdown()


# ---------------------------------------------------------------------------
# Window timing within bounded range
# ---------------------------------------------------------------------------


def test_window_100ms_single_call_latency_floor_proves_window_applied() -> None:
    """With window=100ms, a single call waits at least ~the window.

    The deterministic, host-immune property is the FLOOR: a single caller
    that never fills the batch must wait for the window timer, so its
    latency is at least ~window_ms. That catches the
    window-collapsed-to-zero sabotage (``self._window_s`` accidentally 0 →
    the call returns in <10ms and the floor fires). F82 (#493): the old
    companion ``< 500`` ceiling (meant to catch an off-by-10 window
    inflation) was a wall-clock ceiling that measured host scheduling and
    flaked under load; the window-applied property the suite actually
    relies on is the floor, which is slow-host-immune — a contended host
    only makes the caller wait *longer*, never shorter. The
    never-wakes-at-all hang remains caught by the suite-level
    ``--timeout``.
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=100, max_batch_size=16)
    try:
        start = time.monotonic()
        coalescer.embed("solo")
        elapsed_ms = (time.monotonic() - start) * 1000
        # Floor only: if the window collapsed to 0 the call returns in
        # <10ms and this fires. A loaded host can only push elapsed up.
        assert elapsed_ms >= 80, f"single call returned too fast: {elapsed_ms:.1f}ms"
    finally:
        coalescer.shutdown()


# ---------------------------------------------------------------------------
# Singleton lifecycle
# ---------------------------------------------------------------------------


def test_get_embed_coalescer_returns_none_without_batch_fn() -> None:
    """Without a batch_fn and no prior singleton, returns None.

    Sabotage: remove the ``if embed_batch_fn is None: return None``
    guard and the function would raise ``TypeError`` on the eventual
    EmbedCoalescer constructor call with a missing required arg.
    """
    reset_embed_coalescer()
    try:
        assert get_embed_coalescer() is None
    finally:
        reset_embed_coalescer()


def test_get_embed_coalescer_builds_singleton_on_first_batch_fn() -> None:
    """First call with batch_fn builds; subsequent calls return same instance.

    Sabotage: drop the ``if _EMBED_COALESCER is not None`` early return
    and every call rebuilds — wasting a dispatcher thread per call and
    breaking the singleton invariant.
    """
    reset_embed_coalescer()
    try:
        fake = _CountingBatchFn()
        a = get_embed_coalescer(embed_batch=fake)
        b = get_embed_coalescer(embed_batch=fake)
        assert a is not None
        assert a is b
    finally:
        reset_embed_coalescer()


def test_reset_embed_coalescer_drops_singleton_and_shuts_down() -> None:
    """reset_embed_coalescer rebuilds on next access and stops the dispatcher.

    Sabotage: have reset only ``_EMBED_COALESCER = None`` without
    calling shutdown — the dispatcher thread from the old instance
    leaks; the next test sees a phantom batch dispatcher.
    """
    reset_embed_coalescer()
    fake = _CountingBatchFn()
    first = get_embed_coalescer(embed_batch=fake)
    assert first is not None
    first_dispatcher = first._dispatcher
    assert first_dispatcher is not None and first_dispatcher.is_alive()

    reset_embed_coalescer()
    # Old dispatcher should be joined (dead) after reset.
    assert not first_dispatcher.is_alive()

    second = get_embed_coalescer(embed_batch=fake)
    assert second is not None
    assert second is not first
    reset_embed_coalescer()


def test_defaults_match_documented_bounds() -> None:
    """Default constants match the dispatch brief committed values.

    Sabotage: change DEFAULT_COALESCE_WINDOW_MS from 50 to 500 and the
    docs / env-var default / behavioural contract drift apart.
    """
    assert DEFAULT_COALESCE_WINDOW_MS == 50
    assert DEFAULT_MAX_BATCH_SIZE == 16


# NOTE: env-var read tests for paths.embed_coalesce_* live in
# tests/test_paths.py (F2-baselined home for kairix.paths env-var
# round-trip tests). Construction-by-kwarg is the recommended pattern
# for coalescer unit tests; the paths-read mechanism is tested
# separately, mirroring #280's embed_pool_size policy.
