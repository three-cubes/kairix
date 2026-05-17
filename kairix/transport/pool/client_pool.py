"""Process-shared client pool for the OpenAI-compatible transport.

See docs/architecture/provider-plugin-architecture.md (Wave 2, IM-1).

Background: ``httpx.Client`` owns a connection pool. A fresh client
gets a fresh pool, so without caching every coalescer batch dispatch
opens a new TCP socket and pays a fresh TLS handshake (~300-500 ms
cold). Caching makes the pool's keepalive connections process-shared
so warm requests pay only the HTTP roundtrip.

Two-level surface:

* ``ClientPool`` — DI-clean class. The builder is injected (no
  ``None`` default), so tests construct ``ClientPool(builder=fake)``
  directly without monkeypatching (CLAUDE.md
  ``feedback_no_monkeypatch``).
* ``get_client`` / ``reset_client_cache`` — module-level helpers
  that wrap the production singleton wired to
  ``kairix.credentials.make_openai_client``. Production callers use
  the helpers; the singleton's state is reset between tests by the
  ``_reset_client_pool`` autouse fixture in ``tests/conftest.py``.

Concurrency model: double-checked locking. The fast path is a
lock-free read of the cached client; on miss we take the lock,
re-check (in case another thread populated while we waited), and
then build.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class ClientPool:
    """Process-shared cache for one OpenAI-compatible client.

    The pool is keyed on the ``(api_key, endpoint, timeout)`` tuple. A
    cache miss invokes the injected builder; the result is cached and
    every subsequent call returns the same client object, so the
    underlying ``httpx.Client``'s keepalive pool is process-shared.

    DI seam: ``builder`` is required (no ``None`` default). Tests
    construct ``ClientPool(builder=fake_builder)`` directly — no
    ``monkeypatch.setattr`` needed and no production code path with a
    test-only ``builder=None`` branch (F6).

    Double-checked locking: the fast path reads ``self._client``
    without holding ``self._lock``. On miss the thread takes the lock,
    re-checks the cache (another thread may have populated it while we
    were waiting), and only then invokes the builder. Result: under
    concurrent first-touch fan-out exactly one thread runs the
    builder, every other thread reuses the cached client.
    """

    def __init__(self, builder: Callable[..., Any]) -> None:
        self._builder = builder
        self._lock = threading.Lock()
        # Cached client + the key it was built for. ``None`` means
        # "not yet populated"; a populated entry survives until
        # ``reset()`` drops it.
        self._client: Any | None = None
        self._key: tuple[str, str, float] | None = None

    def get(self, api_key: str, endpoint: str, timeout: float) -> Any:
        """Return the cached client, building it on first call.

        If the cached client was built for a different
        ``(api_key, endpoint, timeout)`` tuple — e.g. an operator
        rotated credentials and called ``reset_client_cache()`` in
        between — the next call rebuilds for the new key.
        """
        key = (api_key, endpoint, timeout)
        # Fast path: lock-free read. Snapshot both fields so a
        # concurrent reset() can't observe an inconsistent state.
        cached = self._client
        cached_key = self._key
        if cached is not None and cached_key == key:
            return cached
        # Slow path: take the lock, re-check, build.
        with self._lock:
            if self._client is not None and self._key == key:
                return self._client
            client = self._builder(api_key, endpoint, timeout=timeout)
            self._client = client
            self._key = key
            return client

    def reset(self) -> None:
        """Drop the cached client.

        Tests call this between scenarios so each test sees a fresh
        pool. Production callers call this when operator credentials
        rotate and the next ``get()`` should rebuild.
        """
        with self._lock:
            self._client = None
            self._key = None


def _build_production_client(api_key: str, endpoint: str, *, timeout: float) -> Any:
    """Builder bound into the production singleton.

    Imports ``kairix.credentials.make_openai_client`` lazily so this
    module doesn't pay the openai SDK import cost when tests construct
    ``ClientPool`` with a fake builder.
    """
    from kairix.credentials import make_openai_client

    return make_openai_client(api_key, endpoint, timeout=timeout)


_PRODUCTION_CLIENT_POOL = ClientPool(builder=_build_production_client)


def get_client(api_key: str, endpoint: str, timeout: float) -> Any:
    """Return the process-shared OpenAI-compatible client.

    Thin module-level wrapper over the production
    ``_PRODUCTION_CLIENT_POOL`` singleton. Production callers
    (``kairix/_azure.py`` and the future per-provider plugins) use
    this; tests that want to inject a fake builder construct their own
    ``ClientPool`` instance.
    """
    return _PRODUCTION_CLIENT_POOL.get(api_key, endpoint, timeout)


def reset_client_cache() -> None:
    """Drop the cached production client.

    The ``_reset_client_pool`` autouse fixture in
    ``tests/conftest.py`` calls this on test teardown so the singleton
    does not leak builder-side state across tests.
    """
    _PRODUCTION_CLIENT_POOL.reset()
