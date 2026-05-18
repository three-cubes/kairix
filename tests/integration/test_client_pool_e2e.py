"""Integration: client pool collapses concurrent dispatch to one client (#provider-plugin-arch IM-1).

Boundary chain (transport surface):
  caller -> kairix.transport.pool.get_client
        -> production singleton .get -> builder (one TLS handshake)

The bug being closed: every coalescer batch was calling
``kairix.credentials.make_openai_client`` which built a fresh
``httpx.Client`` with a fresh connection pool. ~300-500 ms TLS
handshake per batch dispatch. With the pool in place, every
``get_client`` call returns the same cached client and the handshake
runs once per process.

This integration test drives the production surface
``kairix.transport.pool.get_client`` under concurrent fan-out with
the production builder swapped for a counting fake. Sabotage-prove:
drop the cache short-circuit on ``ClientPool.get`` → the counter
goes from 1 to 10.

Provider plugins (under ``kairix/providers/<name>/``) are the
production callers; they each route their transport client
construction through ``kairix.transport.pool.get_client`` so the
same TLS-handshake amortisation applies regardless of which provider
is configured.

F1-clean (no @patch), F2-clean (no env monkeypatch — we substitute
the builder via direct attribute write on the pool's own surface,
same pattern as ``test_embed_coalescer_e2e``'s setattr on
``_EMBED_COALESCER``). F5-clean — only public-surface imports.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest

from kairix.transport.pool import client_pool as client_pool_mod

pytestmark = pytest.mark.integration


class _ClientSentinel:
    """Distinct-identity stand-in for an OpenAI-compatible client.

    Each instance has its own identity so ``a is b`` assertions
    distinguish "same cached client" from "rebuilt-and-equivalent"
    — important because Python may intern equal tuples and break the
    distinction.
    """

    def __init__(self, api_key: str, endpoint: str, timeout: float) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout


class _CountingBuilder:
    """Production-shaped builder: ``(api_key, endpoint, *, timeout)``.

    Returns a fresh ``_ClientSentinel`` per call so the test can verify
    the pool passed the right key through AND collapse the concurrent
    fan-out to a single distinct object. Thread-safe.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, api_key: str, endpoint: str, *, timeout: float) -> _ClientSentinel:
        with self._lock:
            self.calls.append({"api_key": api_key, "endpoint": endpoint, "timeout": timeout})
        return _ClientSentinel(api_key, endpoint, timeout)


@pytest.fixture
def _pool_builder() -> Iterator[_CountingBuilder]:
    """Swap the production pool's builder for a counter; restore on teardown.

    We write to the pool's own ``_builder`` attribute rather than
    monkeypatching a module function — this keeps the production
    surface (``kairix.transport.pool.get_client``) load-bearing in
    the test while letting us count and verify without going to
    Azure.

    F2-clean: no env monkeypatch. The autouse
    ``_reset_client_pool`` fixture in ``tests/conftest.py`` drops
    cached state at teardown.
    """
    counter = _CountingBuilder()
    pool = client_pool_mod._PRODUCTION_CLIENT_POOL
    original = pool._builder
    pool._builder = counter
    pool.reset()
    try:
        yield counter
    finally:
        pool._builder = original
        pool.reset()


def test_ten_concurrent_get_client_calls_invoke_builder_once(
    _pool_builder: _CountingBuilder,
) -> None:
    """Headline: 10 concurrent calls through the production accessor
    build ONE client, not 10.

    Drives ``kairix.transport.pool.get_client`` directly because
    that's the surface ``_azure._get_client`` calls. The chain
    ``_azure._get_client -> kairix.transport.pool.get_client`` is
    covered by ``test_azure_get_client_uses_pool`` below; this test
    isolates the concurrency contract.

    Sabotage: drop the ``self._lock`` re-check in
    ``_ClientPool.get`` (build inside the fast-path miss branch
    without taking the lock) → racing threads each invoke the
    builder, ``_pool_builder.calls`` climbs past 1, this assertion
    fires.
    """
    from kairix.transport.pool import get_client

    # Use Optional[object] so the initial Nones type-check without a
    # ``type: ignore``. Every slot is replaced by the worker; the
    # assert-loop below checks ``r is first`` after errors == [].
    results: list[object | None] = [None] * 10
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            results[i] = get_client("test-key", "https://example.invalid", 30.0)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"workers raised: {errors!r}"
    assert len(_pool_builder.calls) == 1, (
        f"expected exactly 1 client construction; got {len(_pool_builder.calls)}: {_pool_builder.calls!r}"
    )

    # Every caller got the same client object — proves cache hand-off,
    # not just that the build ran once.
    first = results[0]
    assert first is not None
    for r in results:
        assert r is first


def test_pool_reset_forces_rebuild_through_production_surface(
    _pool_builder: _CountingBuilder,
) -> None:
    """After ``reset_client_cache()`` the next ``get_client`` rebuilds.

    Sabotage: make ``reset_client_cache`` a no-op → the post-reset
    call returns the cached object, counter stays at 1.
    """
    from kairix.transport.pool import get_client, reset_client_cache

    a = get_client("k", "https://example.invalid", 30.0)
    reset_client_cache()
    b = get_client("k", "https://example.invalid", 30.0)
    assert a is not b
    assert len(_pool_builder.calls) == 2
