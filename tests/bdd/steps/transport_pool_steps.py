"""Step definitions for transport_pool.feature (#provider-plugin-arch).

Drives :class:`kairix.transport.pool.client_pool.ClientPool` with a
fake provider that counts:

* HTTP clients constructed (the pool's builder invocation count)
* embed calls served (per-pool client method invocations)
* chat calls served (same)
* "HTTP client closed exactly once" (provider's close hook)

The "transport pool wrapping the fake provider" in the feature file
is the ``ClientPool`` instance. The fake provider's HTTP-client
construction is what the pool's builder produces; the provider's
embed / chat methods reach into the pool to fetch (or build) the
shared client and increment counters.

F1-clean (no @patch on internals), F2-clean (no env monkeypatch),
F5-clean (only public symbols imported from
``kairix.transport.pool``).
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.transport.pool import ClientPool

pytestmark = pytest.mark.bdd


# ---------------------------------------------------------------------------
# Fake provider — counts HTTP clients built + embed/chat calls served
# ---------------------------------------------------------------------------


class _FakeHTTPClient:
    """Stand-in for ``httpx.Client``. Owns a closed-flag so the
    "client released on transport.close()" scenario can verify the
    fd / socket reclaim path."""

    def __init__(self, owner: _FakeProvider, client_id: int) -> None:
        self.owner = owner
        self.client_id = client_id
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.owner.closed_count += 1


class _FakeProvider:
    """A provider that counts HTTP-client constructions and per-method calls.

    The provider is the test seam called out in the feature file's
    test-seam comment. The transport pool wraps it: each
    ``embed`` / ``chat`` call asks the pool for the HTTP client (built
    on first miss via ``_build_client``, reused thereafter).
    """

    def __init__(self) -> None:
        self.http_clients_built = 0
        self.embed_calls_served = 0
        self.chat_calls_served = 0
        self.closed_count = 0
        self._lock = threading.Lock()
        # Wrap our builder so the pool's builder count == our
        # http_clients_built count. The pool's lock-free fast path +
        # double-checked locking is what we're proving collapses
        # concurrent fan-out to a single build.
        self._pool = ClientPool(builder=self._build_client)

    def _build_client(self, api_key: str, endpoint: str, *, timeout: float) -> _FakeHTTPClient:
        """The pool calls this on cache miss. Counts builds."""
        del api_key, endpoint, timeout  # F19: unused, kept for production-shape parity
        with self._lock:
            self.http_clients_built += 1
            return _FakeHTTPClient(self, client_id=self.http_clients_built)

    def embed(self, text: str) -> tuple[str, int]:
        """Resolve client via the pool, record the embed call.

        Returns a (text, client_id) tuple so the caller can assert
        every call landed on the same client (i.e. the pool handed
        back the cached one).
        """
        client = self._pool.get("k", "https://example.invalid", 30.0)
        with self._lock:
            self.embed_calls_served += 1
        return (text, client.client_id)

    def chat(self, prompt: str) -> tuple[str, int]:
        client = self._pool.get("k", "https://example.invalid", 30.0)
        with self._lock:
            self.chat_calls_served += 1
        return (prompt, client.client_id)

    def close(self) -> None:
        """Drop the cached HTTP client + close it.

        The pool stores the client object directly, so we read it,
        call .close(), and then reset the pool. Production analogue:
        a transport with an explicit shutdown path that drains the
        keepalive pool.
        """
        # Read the cached client BEFORE reset (reset drops the
        # reference). ClientPool exposes the cached client via .get
        # — we already have a key from prior calls so .get returns
        # it without rebuilding.
        client = self._pool.get("k", "https://example.invalid", 30.0)
        client.close()
        self._pool.reset()


# ---------------------------------------------------------------------------
# Fixture — per-scenario state
# ---------------------------------------------------------------------------


@pytest.fixture
def _transport_pool_state() -> dict[str, Any]:
    """Per-scenario state container.

    Holds the fake provider (which owns its own ``ClientPool``
    inline), the worker pool concurrency, and the responses collected
    from the embed / chat calls. The provider's counters are the
    primary assertion target.
    """
    return {
        "provider": None,
        "concurrency": 1,
        "embed_responses": [],
        "chat_responses": [],
    }


# ---------------------------------------------------------------------------
# Given — wiring
# ---------------------------------------------------------------------------


@given("a fake provider that counts how many HTTP clients it builds")
def _given_fake_provider(_transport_pool_state: dict[str, Any]) -> None:
    _transport_pool_state["provider"] = _FakeProvider()


@given("a transport pool wrapping the fake provider")
def _given_pool_wrapping(_transport_pool_state: dict[str, Any]) -> None:
    """No-op — the provider owns its ``ClientPool`` inline.

    Documented here because the feature file's Background expects
    the binding to exist; the pool is constructed in ``_FakeProvider.__init__``.
    """
    assert _transport_pool_state["provider"] is not None


@given(parsers.parse("the fake provider has built {n:d} HTTP clients"))
def _given_initial_built_count(_transport_pool_state: dict[str, Any], n: int) -> None:
    """Sanity-check the starting counter — every scenario starts at 0."""
    provider: _FakeProvider = _transport_pool_state["provider"]
    assert provider.http_clients_built == n, f"expected {n} at start; got {provider.http_clients_built}"


@given(parsers.parse("a worker pool with concurrency {n:d}"))
def _given_worker_pool_concurrency(_transport_pool_state: dict[str, Any], n: int) -> None:
    _transport_pool_state["concurrency"] = n


# ---------------------------------------------------------------------------
# When — dispatch
# ---------------------------------------------------------------------------


@when(parsers.parse("the caller dispatches {n:d} sequential embed requests through the transport pool"))
def _when_sequential_embed(_transport_pool_state: dict[str, Any], n: int) -> None:
    provider: _FakeProvider = _transport_pool_state["provider"]
    responses = [provider.embed(f"text-{i}") for i in range(n)]
    _transport_pool_state["embed_responses"] = responses


@when(parsers.parse("the worker pool dispatches {n:d} embed requests through the transport pool"))
def _when_concurrent_embed(_transport_pool_state: dict[str, Any], n: int) -> None:
    """Concurrent fan-out at the configured concurrency.

    Spins up ``concurrency`` worker threads, each pulling work off a
    shared counter until ``n`` requests are issued. This is the
    scenario that catches a pool with no lock / no double-check: a
    racing first-touch would have N threads each invoke the builder
    before the cache populated.
    """
    provider: _FakeProvider = _transport_pool_state["provider"]
    concurrency = _transport_pool_state["concurrency"]

    # Use Optional[tuple] so the initial Nones type-check without a
    # ``type: ignore``. The "N responses present" Then-step asserts
    # no slot is None.
    responses: list[tuple[str, int] | None] = [None] * n
    next_idx = [0]
    next_lock = threading.Lock()
    errors: list[BaseException] = []

    def worker() -> None:
        while True:
            with next_lock:
                i = next_idx[0]
                if i >= n:
                    return
                next_idx[0] = i + 1
            try:
                responses[i] = provider.embed(f"text-{i}")
            except Exception as exc:
                errors.append(exc)
                return

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"workers raised: {errors!r}"
    _transport_pool_state["embed_responses"] = responses


@when(parsers.parse("the caller dispatches {n:d} embed requests through the transport pool"))
def _when_caller_embed(_transport_pool_state: dict[str, Any], n: int) -> None:
    """Sequential variant — used by scenarios that mix embed + chat."""
    provider: _FakeProvider = _transport_pool_state["provider"]
    responses = [provider.embed(f"text-{i}") for i in range(n)]
    _transport_pool_state["embed_responses"].extend(responses)


@when(parsers.parse("the caller dispatches {n:d} chat requests through the transport pool"))
def _when_caller_chat(_transport_pool_state: dict[str, Any], n: int) -> None:
    provider: _FakeProvider = _transport_pool_state["provider"]
    responses = [provider.chat(f"prompt-{i}") for i in range(n)]
    _transport_pool_state["chat_responses"].extend(responses)


@when("the caller closes the transport pool")
def _when_close(_transport_pool_state: dict[str, Any]) -> None:
    provider: _FakeProvider = _transport_pool_state["provider"]
    provider.close()


# ---------------------------------------------------------------------------
# Then — assertions
# ---------------------------------------------------------------------------


@then(parsers.parse("the caller receives {n:d} successful embed responses"))
def _then_n_responses(_transport_pool_state: dict[str, Any], n: int) -> None:
    """Sabotage: drop the embed return value → responses is empty / None entries → length mismatch."""
    responses = _transport_pool_state["embed_responses"]
    assert len(responses) == n, f"expected {n} responses; got {len(responses)}"
    for r in responses:
        assert r is not None, "found a missing response slot"


@then(parsers.parse("the fake provider reports exactly {n:d} HTTP client constructed"))
def _then_n_http_clients(_transport_pool_state: dict[str, Any], n: int) -> None:
    """Sabotage: remove the cache short-circuit in ``ClientPool.get``
    → every embed call invokes the builder, ``http_clients_built``
    climbs to the number of embed calls dispatched.
    """
    provider: _FakeProvider = _transport_pool_state["provider"]
    assert provider.http_clients_built == n, (
        f"expected exactly {n} HTTP clients constructed; got {provider.http_clients_built}"
    )
    # All responses (when present) should be tagged with the same
    # client_id — proves the cache hand-off, not just the build count.
    all_responses = _transport_pool_state["embed_responses"] + _transport_pool_state["chat_responses"]
    if all_responses and n == 1:
        client_ids = {r[1] for r in all_responses}
        assert client_ids == {1}, f"responses span multiple clients: {client_ids}"


@then(parsers.parse("the fake provider reports {n:d} embed calls served"))
def _then_n_embed_calls(_transport_pool_state: dict[str, Any], n: int) -> None:
    """Sabotage: short-circuit ``embed`` (don't increment) → counter
    stays at 0, this fails.
    """
    provider: _FakeProvider = _transport_pool_state["provider"]
    assert provider.embed_calls_served == n, f"expected {n} embed calls served; got {provider.embed_calls_served}"


@then(parsers.parse("the fake provider reports {n:d} chat calls served"))
def _then_n_chat_calls(_transport_pool_state: dict[str, Any], n: int) -> None:
    """Sabotage: short-circuit ``chat`` → counter stays at 0, fails."""
    provider: _FakeProvider = _transport_pool_state["provider"]
    assert provider.chat_calls_served == n, f"expected {n} chat calls served; got {provider.chat_calls_served}"


@then("the fake provider reports its HTTP client was closed exactly once")
def _then_closed_once(_transport_pool_state: dict[str, Any]) -> None:
    """Sabotage: make ``_FakeProvider.close`` a no-op → counter
    stays at 0, this fails — catches the fd / socket leak class
    called out in the feature file's sabotage comment.
    """
    provider: _FakeProvider = _transport_pool_state["provider"]
    assert provider.closed_count == 1, f"expected close-count 1; got {provider.closed_count}"
