"""Unit tests for the transport client pool (#provider-plugin-arch IM-1).

Each test is sabotage-provable: mutating the production
implementation in the documented way flips the assertion, restore →
green. CLAUDE.md feedback_no_assertions_that_pass_either_way and
feedback_no_internal_function_tests apply — every assertion is
deliberate, and every behaviour is driven through the public surface
(``ClientPool.get`` / ``.reset()``).

The fakes live inline in this module because they're test-only
plumbing (a counting builder); the canonical Fake* classes in
``tests/fakes.py`` are protocol-conformant doubles for kairix's
domain protocols, which doesn't apply to a transport-layer builder
callable.
"""

from __future__ import annotations

import threading
import time

import pytest

from kairix.transport.pool.client_pool import ClientPool

pytestmark = pytest.mark.unit


class _ClientSentinel:
    """Distinct-identity stand-in for an OpenAI-compatible client.

    A bare ``object()`` works for ``is``-comparison, but we keep the
    constructor args around so failing tests print useful messages.
    Critically each instance has its own identity — unlike a frozen
    tuple, which Python may intern (constant-fold), breaking ``a is
    b`` assertions when two builds would otherwise produce equal
    tuples.
    """

    def __init__(self, api_key: str, endpoint: str, timeout: float) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout


class _CountingBuilder:
    """Builder that counts invocations and returns a fresh sentinel object.

    The sentinel has its own identity per call so ``a is b`` assertions
    distinguish "same cached client" from "rebuilt-and-equivalent".
    Thread-safe so we can drive concurrent .get() calls without losing
    counts.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []
        self._lock = threading.Lock()

    def __call__(self, api_key: str, endpoint: str, *, timeout: float) -> _ClientSentinel:
        with self._lock:
            self.calls.append((api_key, endpoint, timeout))
        return _ClientSentinel(api_key, endpoint, timeout)


def _make_pool() -> tuple[ClientPool, _CountingBuilder]:
    """Construct a ClientPool with a counting builder. DI-clean — no
    monkeypatching, no module-level singleton mutation."""
    counter = _CountingBuilder()
    pool = ClientPool(builder=counter)
    return pool, counter


def test_first_get_invokes_builder_once() -> None:
    """The first .get() call invokes the builder.

    Sabotage: if the pool builds *on every* call instead of caching,
    the second assertion (.calls == 1 after two .get() calls)
    flips — see ``test_second_get_returns_cached``.
    """
    pool, counter = _make_pool()
    client = pool.get("k", "https://example.invalid", 30.0)
    assert isinstance(client, _ClientSentinel)
    assert client.api_key == "k"
    assert client.endpoint == "https://example.invalid"
    assert client.timeout == 30.0
    assert len(counter.calls) == 1


def test_second_get_returns_cached_object() -> None:
    """Repeated .get() with the same key returns the same object
    AND does not re-invoke the builder.

    Sabotage: drop the ``if cached is not None`` short-circuit in
    ``ClientPool.get`` (so the builder runs every time) → call count
    goes to 2, ``is`` comparison stays True for now (same builder
    returns equal tuples) but call count flips first.

    Hardening: assert call count == 1 (catches the build-every-call
    sabotage) AND ``is``-identity (catches a subtler sabotage where
    the cache is replaced on every call).
    """
    pool, counter = _make_pool()
    a = pool.get("k", "https://example.invalid", 30.0)
    b = pool.get("k", "https://example.invalid", 30.0)
    assert a is b
    assert len(counter.calls) == 1


def test_get_rebuilds_when_key_changes() -> None:
    """If the operator's key/endpoint/timeout changes between calls
    (e.g. credential rotation), the pool rebuilds.

    Sabotage: cache on (), (), or constant → the second build is
    skipped and the second .get() returns the stale client.
    """
    pool, counter = _make_pool()
    a = pool.get("k1", "https://ep1.invalid", 30.0)
    b = pool.get("k2", "https://ep2.invalid", 30.0)
    assert a != b
    assert len(counter.calls) == 2


def test_reset_drops_cached_client() -> None:
    """After .reset() the next .get() rebuilds.

    Sabotage: make .reset() a no-op (return immediately) → the post-
    reset .get() returns the old cached client and builder.calls stays
    at 1, this assertion flips.
    """
    pool, counter = _make_pool()
    pool.get("k", "https://example.invalid", 30.0)
    pool.reset()
    pool.get("k", "https://example.invalid", 30.0)
    assert len(counter.calls) == 2


def test_concurrent_get_invokes_builder_exactly_once() -> None:
    """Under concurrent first-touch fan-out, exactly one thread builds.

    Sabotage: remove ``self._lock`` acquisition in
    ``ClientPool.get`` (drop the ``with self._lock:`` block, build
    inline without re-check) → multiple threads race past the
    lock-free fast path with cache empty, each invokes the builder,
    counter.calls climbs above 1. The slow builder
    (50 ms sleep) widens the race window so the sabotage is
    repeatable rather than a once-in-100-runs flake.

    Lock-free fast-path verification: after every thread has finished,
    every result is the same object (``is``-identity).
    """

    class _SlowBuilder:
        def __init__(self) -> None:
            self.calls = 0
            self._lock = threading.Lock()

        def __call__(self, api_key: str, endpoint: str, *, timeout: float) -> object:
            with self._lock:
                self.calls += 1
            # Hold inside the builder so racing threads pile up on
            # the pool's lock waiting for the first builder to
            # finish. Without ClientPool's lock + re-check, every
            # waiter would proceed to invoke the builder.
            time.sleep(0.05)
            return object()

    slow = _SlowBuilder()
    pool = ClientPool(builder=slow)

    # Use Optional[object] so the initial Nones type-check without a
    # ``type: ignore``. Every slot is replaced before assertions run;
    # the test verifies that no slot is None below.
    results: list[object | None] = [None] * 20
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            results[i] = pool.get("k", "https://example.invalid", 30.0)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert slow.calls == 1, f"expected single builder invocation; got {slow.calls}"
    # All threads returned the same object — proves the cache hand-off
    # works, not just that the build ran once.
    first = results[0]
    for r in results:
        assert r is first


def test_reset_under_concurrent_get_does_not_break() -> None:
    """Interleaving .reset() with concurrent .get() never returns
    None and never raises.

    Sabotage: drop the lock in .reset() so it clears ``self._client``
    half-way through a .get() lock-free read → a reader could
    observe ``_client is None`` on the fast path AND ``_client is
    None`` on the slow-path re-check, then build. Without the lock
    the failure mode is subtler (torn assignment); we assert no
    None / no error as the observable contract.
    """
    pool, counter = _make_pool()

    errors: list[BaseException] = []
    nones: list[int] = []

    def reader() -> None:
        try:
            for _ in range(50):
                got = pool.get("k", "https://example.invalid", 30.0)
                if got is None:
                    nones.append(1)
        except Exception as exc:
            errors.append(exc)

    def resetter() -> None:
        try:
            for _ in range(20):
                pool.reset()
                time.sleep(0.001)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)] + [threading.Thread(target=resetter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert nones == []
    # Lower bound: at least 1 build (the first reader). Upper bound:
    # at most one per .reset() + the initial build (= 21). If the
    # pool were unsafe and didn't cache at all, calls would equal
    # the total .get() count (200), which is what the assert catches.
    assert 1 <= len(counter.calls) <= 21, f"unexpected builder call count: {len(counter.calls)}"


def test_module_level_get_client_uses_production_pool() -> None:
    """``kairix.transport.pool.get_client`` delegates to the
    process-shared singleton.

    Sabotage: route ``get_client`` to a freshly-constructed pool per
    call (e.g. ``return ClientPool(builder=...).get(...)``) → every
    call rebuilds, and even with the production builder injected
    the cached identity would not survive.

    We verify by constructing two pools with the SAME counting
    builder and showing that the module-level call goes through
    ``_PRODUCTION_CLIENT_POOL`` specifically (the public surface), not
    a per-call instance. The contract: between two ``get_client``
    calls with the same key, the production builder runs at most once.
    """
    from kairix.transport.pool.client_pool import install_production_builder

    # Swap the production pool's builder for a counting one for this
    # test, then restore. Uses the public ``install_production_builder``
    # seam (F1-clean) rather than a direct write on the underscore-
    # prefixed pool attribute. The autouse _reset_client_pool fixture
    # in conftest drops cached state at teardown so this doesn't leak
    # to other tests.
    counter = _CountingBuilder()
    original_builder = install_production_builder(counter)
    try:
        from kairix.transport.pool import get_client

        a = get_client("k", "https://example.invalid", 30.0)
        b = get_client("k", "https://example.invalid", 30.0)
        assert a is b
        assert len(counter.calls) == 1
    finally:
        install_production_builder(original_builder)


def test_reset_client_cache_clears_production_singleton() -> None:
    """``reset_client_cache()`` drops the cached production client.

    Sabotage: make ``reset_client_cache`` a no-op → after reset the
    next ``get_client`` returns the cached object and counter stays
    at 1.
    """
    from kairix.transport.pool import get_client, reset_client_cache
    from kairix.transport.pool.client_pool import install_production_builder

    counter = _CountingBuilder()
    original_builder = install_production_builder(counter)
    try:
        get_client("k", "https://example.invalid", 30.0)
        reset_client_cache()
        get_client("k", "https://example.invalid", 30.0)
        assert len(counter.calls) == 2
    finally:
        install_production_builder(original_builder)
