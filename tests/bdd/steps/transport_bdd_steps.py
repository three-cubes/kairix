"""Step definitions for the four transport BDD features (#provider-plugin-arch IM-6).

Bundled into one module because several step phrases are shared across
``transport_coalesce.feature``, ``transport_cache.feature``,
``transport_retry.feature``, and ``transport_timeout.feature`` (the
clearest examples are ``"N callers concurrently request embeddings
within the same M millisecond window"`` which appears in both the
coalesce and the timeout-composition scenarios, and
``"the caller dispatches one embed request"`` which appears in both
retry and timeout features). pytest-bdd's step registry is global —
splitting the steps across modules would let one definition shadow
another at random based on plugin-import order. One module guarantees
each step has exactly one definition.

A single per-scenario state fixture (``_transport_state``) carries
the substate for whichever feature is running. The fixture decides at
construction time which mode (cache / coalesce / retry / timeout) the
scenario belongs to based on the Background's Given steps. Step
implementations read state via keys that are set by the relevant
Given steps; cross-mode keys default to ``None`` so a step that fires
in the wrong scenario will surface an explicit error rather than
silently passing.

F1-clean (no @patch), F2-clean (no env monkeypatch), F5-clean
(public symbols only from ``kairix.transport.{cache,coalesce,retry,timeout}``
and ``kairix.providers``).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.providers import ClientError, RetryExhausted, TimeoutExceeded
from kairix.transport.cache import EmbedCache
from kairix.transport.coalesce import EmbedCoalescer
from kairix.transport.retry import AttemptEvent, RetryPolicy
from kairix.transport.timeout import TimeoutBudget
from tests.fakes import FakeClock, FakeProvider

pytestmark = pytest.mark.bdd


# ---------------------------------------------------------------------------
# Per-scenario fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def _transport_state() -> Any:
    """Per-scenario state shared across all four transport features.

    Each feature's Given steps populate the keys they care about;
    cross-feature keys default to ``None`` so a step firing in the
    wrong scenario surfaces a typed lookup error rather than silently
    no-op'ing.
    """
    provider = FakeProvider(name="fake-transport", dim=3)
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="transport-bdd")
    state: dict[str, Any] = {
        # Universal
        "provider": provider,
        "executor": executor,
        "results": [],
        "errors": [],
        "elapsed_ms": None,
        # Cache
        "cache": None,
        "cache_wrapper": None,
        # Coalesce
        "coalescer": None,
        "window_ms": None,
        # Retry
        "scripted": None,
        "clock": None,
        "events": [],
        "max_attempts": 3,
        "backoff_s": 0.0,
        "policy": None,
        "result": None,
        "raised": None,
        # Timeout
        "budget": None,
        "default_budget_ms": 200,
        "raised_per_caller": [],
        "timeout_dispatcher": None,
    }
    yield state
    # Teardown — order matters: dispatchers / coalescers shut down
    # first so their dispatcher threads aren't holding references to
    # the executor or budget that's about to disappear.
    disp = state["timeout_dispatcher"]
    if disp is not None:
        disp.shutdown()
    coalescer = state["coalescer"]
    if coalescer is not None:
        coalescer.shutdown()
    budget = state["budget"]
    if budget is not None:
        budget.shutdown()
    executor.shutdown(wait=False, cancel_futures=True)


# ===========================================================================
# COALESCE — transport_coalesce.feature
# ===========================================================================


@given("a fake provider that records each batched embed call it serves")
def _given_coalesce_provider(_transport_state: dict[str, Any]) -> None:
    """Anchor Background step — fixture wired the provider."""
    assert _transport_state["provider"] is not None


@given("a transport coalescer wrapping the fake provider")
def _given_coalesce_wrap(_transport_state: dict[str, Any]) -> None:
    """No-op anchor — the coalescer is built when the window step fires."""


@given(
    parsers.parse(
        "the coalescer is configured with a {window_ms:d} millisecond window and max batch size {max_batch:d}"
    )
)
def _given_coalescer_config(_transport_state: dict[str, Any], window_ms: int, max_batch: int) -> None:
    """Build the coalescer with the scenario's window + batch size."""
    provider: FakeProvider = _transport_state["provider"]
    coalescer = EmbedCoalescer(
        embed_batch_fn=provider.embed_batch,
        coalesce_window_ms=window_ms,
        max_batch_size=max_batch,
    )
    _transport_state["coalescer"] = coalescer
    _transport_state["window_ms"] = window_ms


def _fan_out(state: dict[str, Any], n: int, prefix: str = "text") -> None:
    """Helper: spin up ``n`` worker threads, all hitting the coalescer.

    Each worker submits a unique text so the dispatcher can split on
    text count without depending on dedup behaviour. Results land in
    ``state["results"]`` for later assertion.
    """
    coalescer: EmbedCoalescer = state["coalescer"]
    results: list[list[float]] = [[] for _ in range(n)]
    errors: list[BaseException] = []

    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        try:
            # Barrier ensures all workers arrive at the coalescer
            # within the same millisecond — without it, slow thread
            # startup on CI can stagger arrivals across the window.
            barrier.wait(timeout=2.0)
            results[i] = coalescer.embed(f"{prefix}-{i}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    state["results"].extend(results)
    state["errors"].extend(errors)


@when(
    parsers.re(
        r"^(?P<n>\d+) callers concurrently request embeddings within the same (?P<window_ms>\d+) millisecond window$"
    )
)
def _when_concurrent_within_window(_transport_state: dict[str, Any], n: str, window_ms: str) -> None:
    """Fan-out N callers; window is informational (already configured).

    Used by both transport_coalesce.feature and (via the composition
    scenario) transport_timeout.feature. The implementation forks on
    whether a timeout dispatcher is wired in front of the coalescer:
    if so, drive the dispatcher; otherwise drive the bare coalescer.
    """
    del window_ms
    dispatcher = _transport_state["timeout_dispatcher"]
    if dispatcher is not None:
        _coalesced_timeout_fan_out(_transport_state, int(n), dispatcher)
    else:
        _fan_out(_transport_state, int(n))


@when("1 caller requests an embedding and no other callers arrive")
def _when_lonely_caller(_transport_state: dict[str, Any]) -> None:
    """Single caller → must wait the window before the dispatcher fires."""
    coalescer: EmbedCoalescer = _transport_state["coalescer"]
    start = time.monotonic()
    vec = coalescer.embed("solo")
    elapsed = (time.monotonic() - start) * 1000
    _transport_state["results"].append(vec)
    _transport_state["elapsed_ms"] = elapsed


@when(parsers.re(r"^(?P<n>\d+) callers concurrently request embeddings$"))
def _when_concurrent_no_window(_transport_state: dict[str, Any], n: str) -> None:
    """Variant without 'within window' — used by the zero-window scenario."""
    _fan_out(_transport_state, int(n))


@when(parsers.re(r"^after the window closes (?P<n>\d+) more callers concurrently request embeddings$"))
def _when_after_window(_transport_state: dict[str, Any], n: str) -> None:
    """Sleep just past the window then fan-out the second wave."""
    window_ms = _transport_state["window_ms"]
    # Two windows of slack so we're certain the first batch dispatched
    # before the second wave arrives.
    time.sleep((window_ms / 1000.0) * 2.5)
    _fan_out(_transport_state, int(n), prefix="wave2")


@then("every caller receives a non-empty embedding vector")
def _then_every_non_empty(_transport_state: dict[str, Any]) -> None:
    """Coalesce + cache happy path: every caller got a non-empty vector."""
    assert _transport_state["errors"] == [], f"workers raised: {_transport_state['errors']!r}"
    results = _transport_state["results"]
    assert results, "no results recorded"
    for i, r in enumerate(results):
        assert r, f"caller {i} got empty vector"


@then(parsers.parse("the fake provider records exactly {n:d} batched embed call"))
def _then_one_batched_call(_transport_state: dict[str, Any], n: int) -> None:
    """Sabotage: per-caller dispatch → counter explodes past {n}."""
    provider: FakeProvider = _transport_state["provider"]
    assert len(provider.embed_calls) == n, (
        f"expected exactly {n} batched embed call; got {len(provider.embed_calls)}: {provider.embed_calls!r}"
    )


@then(parsers.parse("the fake provider records exactly {n:d} batched embed calls"))
def _then_n_batched_calls(_transport_state: dict[str, Any], n: int) -> None:
    """Plural variant."""
    provider: FakeProvider = _transport_state["provider"]
    assert len(provider.embed_calls) == n, (
        f"expected exactly {n} batched embed calls; got {len(provider.embed_calls)}: {provider.embed_calls!r}"
    )


@then(parsers.parse("the batched embed call carried {n:d} texts"))
def _then_call_carried_texts_plural(_transport_state: dict[str, Any], n: int) -> None:
    provider: FakeProvider = _transport_state["provider"]
    assert len(provider.embed_calls) == 1, f"expected a single batched call to inspect; got {len(provider.embed_calls)}"
    assert len(provider.embed_calls[0]) == n, (
        f"expected the batch to carry {n} texts; got {len(provider.embed_calls[0])}"
    )


@then(parsers.parse("the batched embed call carried {n:d} text"))
def _then_call_carried_texts_singular(_transport_state: dict[str, Any], n: int) -> None:
    provider: FakeProvider = _transport_state["provider"]
    assert len(provider.embed_calls) == 1, f"expected a single batched call to inspect; got {len(provider.embed_calls)}"
    assert len(provider.embed_calls[0]) == n, (
        f"expected the batch to carry {n} text; got {len(provider.embed_calls[0])}"
    )


@then(parsers.parse("one batched embed call carried {n:d} texts"))
def _then_one_call_carried_texts(_transport_state: dict[str, Any], n: int) -> None:
    provider: FakeProvider = _transport_state["provider"]
    sizes = [len(c) for c in provider.embed_calls]
    assert n in sizes, f"expected a batch of {n} texts among {sizes}"


@then(parsers.parse("one batched embed call carried {n:d} text"))
def _then_one_call_carried_text(_transport_state: dict[str, Any], n: int) -> None:
    provider: FakeProvider = _transport_state["provider"]
    sizes = [len(c) for c in provider.embed_calls]
    assert n in sizes, f"expected a batch of {n} text among {sizes}"


@then("every batched embed call carried 1 text")
def _then_every_batched_carried_one(_transport_state: dict[str, Any]) -> None:
    provider: FakeProvider = _transport_state["provider"]
    sizes = [len(c) for c in provider.embed_calls]
    assert all(s == 1 for s in sizes), f"expected every batched call to carry exactly 1 text; got sizes {sizes}"


@then(parsers.parse("each batched embed call carried {n:d} texts"))
def _then_each_batched_carried(_transport_state: dict[str, Any], n: int) -> None:
    provider: FakeProvider = _transport_state["provider"]
    sizes = [len(c) for c in provider.embed_calls]
    assert all(s == n for s in sizes), f"expected every batched call to carry {n} texts; got sizes {sizes}"


@then(parsers.parse("the caller receives a non-empty embedding vector within {budget_ms:d} milliseconds"))
def _then_lonely_within_budget(_transport_state: dict[str, Any], budget_ms: int) -> None:
    elapsed = _transport_state["elapsed_ms"]
    assert elapsed is not None, "no elapsed time recorded"
    assert elapsed < budget_ms, f"lonely caller exceeded budget: elapsed={elapsed:.1f}ms, budget={budget_ms}ms"
    results = _transport_state["results"]
    assert results and results[-1], "lonely caller got empty vector"


# ===========================================================================
# CACHE — transport_cache.feature
# ===========================================================================


class _CacheBackedProvider:
    """Cache-aware wrapper that delegates only on cache misses.

    Mirrors the production wiring: every embed call consults the
    transport cache first; cache hits short-circuit, misses fall
    through to ``provider.embed_batch`` and the result is stored.
    """

    def __init__(self, provider: FakeProvider, cache: EmbedCache) -> None:
        self._provider = provider
        self._cache = cache

    def embed_one(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        vectors = self._provider.embed_batch([text])
        if vectors and vectors[0]:
            self._cache.put(text, vectors[0])
            return vectors[0]
        return []

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float] | None] = [self._cache.get(t) for t in texts]
        uncached_idx = [i for i, r in enumerate(results) if r is None]
        if uncached_idx:
            uncached_texts = [texts[i] for i in uncached_idx]
            fresh = self._provider.embed_batch(uncached_texts)
            for src_pos, idx in enumerate(uncached_idx):
                vec = fresh[src_pos] if src_pos < len(fresh) else []
                if vec:
                    self._cache.put(texts[idx], vec)
                results[idx] = vec
        return [r if r is not None else [] for r in results]


class _TextKeyedProvider(FakeProvider):
    """FakeProvider variant whose embed_batch returns per-text-deterministic vectors.

    The default :class:`FakeProvider` returns one fixed vector for
    every text. We need per-text uniqueness so the cache-key tests
    can falsify "the cache returns the wrong vector for distinct texts".
    """

    def embed_batch(self, texts: list[str]) -> list[list[float]]:  # type: ignore[override] — override signature matches FakeProvider intentionally; we return per-text-deterministic vectors instead of the parent's single fixed vector
        self.embed_calls.append(list(texts))
        return [self._vector_for(t) for t in texts]

    @staticmethod
    def _vector_for(text: str) -> list[float]:
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [
            int.from_bytes(digest[0:4], "big") / (2**32 - 1) * 2 - 1,
            int.from_bytes(digest[4:8], "big") / (2**32 - 1) * 2 - 1,
            int.from_bytes(digest[8:12], "big") / (2**32 - 1) * 2 - 1,
        ]


@given("a fake provider that returns deterministic vectors keyed by text")
def _given_cache_provider(_transport_state: dict[str, Any]) -> None:
    """Swap the universal provider for the text-keyed variant + build the cache."""
    text_keyed = _TextKeyedProvider(name="fake-cache-backed")
    cache = EmbedCache(max_entries=64, max_age_s=600)
    _transport_state["provider"] = text_keyed
    _transport_state["cache"] = cache
    _transport_state["cache_wrapper"] = _CacheBackedProvider(provider=text_keyed, cache=cache)


@given("a transport cache wrapping the fake provider")
def _given_cache_wrap(_transport_state: dict[str, Any]) -> None:
    assert _transport_state["cache_wrapper"] is not None


@given("the transport cache is empty")
def _given_cache_empty(_transport_state: dict[str, Any]) -> None:
    cache: EmbedCache = _transport_state["cache"]
    assert cache.stats().size == 0, "cache should start empty"


@given(parsers.parse('the transport cache contains a vector for the text "{text}"'))
def _given_cache_warm(_transport_state: dict[str, Any], text: str) -> None:
    provider: _TextKeyedProvider = _transport_state["provider"]
    cache: EmbedCache = _transport_state["cache"]
    vector = provider._vector_for(text)
    cache.put(text, vector)


@when(parsers.parse('the caller embeds the text "{text}"'))
def _when_embed_text(_transport_state: dict[str, Any], text: str) -> None:
    wrapper: _CacheBackedProvider = _transport_state["cache_wrapper"]
    vec = wrapper.embed_one(text)
    _transport_state["results"].append((text, vec))


@when(parsers.parse('the caller embeds the text "{text}" a second time'))
def _when_embed_text_again(_transport_state: dict[str, Any], text: str) -> None:
    wrapper: _CacheBackedProvider = _transport_state["cache_wrapper"]
    vec = wrapper.embed_one(text)
    _transport_state["results"].append((text, vec))


@when(parsers.parse('the caller batch-embeds the texts "{a}", "{b}", "{c}", "{d}"'))
def _when_batch_embed(_transport_state: dict[str, Any], a: str, b: str, c: str, d: str) -> None:
    wrapper: _CacheBackedProvider = _transport_state["cache_wrapper"]
    texts = [a, b, c, d]
    vectors = wrapper.embed_many(texts)
    for text, vec in zip(texts, vectors, strict=True):
        _transport_state["results"].append((text, vec))


@then("both calls return the same embedding vector")
def _then_same_vector(_transport_state: dict[str, Any]) -> None:
    results = _transport_state["results"]
    assert len(results) >= 2, f"expected at least 2 results; got {len(results)}"
    first_vec = results[0][1]
    second_vec = results[1][1]
    assert first_vec == second_vec, f"expected same vector; got {first_vec!r} vs {second_vec!r}"


@then(parsers.parse("the fake provider reports exactly {n:d} embed call served"))
def _then_provider_calls_singular(_transport_state: dict[str, Any], n: int) -> None:
    provider: FakeProvider = _transport_state["provider"]
    assert len(provider.embed_calls) == n, (
        f"expected exactly {n} embed call served; got {len(provider.embed_calls)}: {provider.embed_calls!r}"
    )


@then(parsers.parse("the fake provider reports exactly {n:d} embed calls served"))
def _then_provider_calls_plural(_transport_state: dict[str, Any], n: int) -> None:
    provider: FakeProvider = _transport_state["provider"]
    assert len(provider.embed_calls) == n, (
        f"expected exactly {n} embed calls served; got {len(provider.embed_calls)}: {provider.embed_calls!r}"
    )


@then(parsers.parse("the fake provider reports {n:d} embed calls served"))
def _then_provider_calls_unrestrictive(
    _transport_state: dict[str, Any],
    _transport_pool_state: dict[str, Any],
    n: int,
) -> None:
    """Used by both transport_cache.feature ("reports 0 embed calls served")
    and transport_pool.feature ("reports 100 embed calls served").

    Forks on which fixture's provider is wired: pool's inline
    _FakeProvider uses an ``embed_calls_served`` int counter; the
    canonical FakeProvider in tests/fakes.py uses an ``embed_calls``
    list whose length is the served count. We dispatch on whichever
    has a non-None provider in scope.
    """
    pool_provider = _transport_pool_state.get("provider")
    if pool_provider is not None and hasattr(pool_provider, "embed_calls_served"):
        assert pool_provider.embed_calls_served == n, (
            f"expected {n} embed calls served; got {pool_provider.embed_calls_served}"
        )
        return
    provider: FakeProvider = _transport_state["provider"]
    assert len(provider.embed_calls) == n, (
        f"expected {n} embed calls served; got {len(provider.embed_calls)}: {provider.embed_calls!r}"
    )


@then("the two calls return different embedding vectors")
def _then_distinct_vectors(_transport_state: dict[str, Any]) -> None:
    results = _transport_state["results"]
    assert len(results) == 2, f"expected exactly 2 results; got {len(results)}"
    assert results[0][1] != results[1][1], f"expected distinct vectors; got identical: {results[0][1]!r}"


@then("the caller receives a non-empty embedding vector")
def _then_non_empty_singular(_transport_state: dict[str, Any]) -> None:
    """Cache scenario: caller receives a non-empty vector (singular caller)."""
    results = _transport_state["results"]
    assert results, "no result recorded"
    last_text, last_vec = results[-1] if isinstance(results[-1], tuple) else (None, results[-1])
    assert last_vec, f"expected non-empty vector for {last_text!r}; got {last_vec!r}"


@then("every returned vector matches the cache for warm texts")
def _then_warm_match(_transport_state: dict[str, Any]) -> None:
    cache: EmbedCache = _transport_state["cache"]
    provider: _TextKeyedProvider = _transport_state["provider"]
    results = _transport_state["results"]
    for text, vec in results:
        if text.startswith("warm"):
            expected = provider._vector_for(text)
            assert vec == expected, f"warm text {text!r}: cached {vec!r} != expected {expected!r}"
            cached_now = cache.get(text)
            assert cached_now is not None, f"cache lost warm entry for {text!r}"


@then(parsers.parse("the fake provider's last embed call carried exactly {n:d} texts"))
def _then_last_call_size(_transport_state: dict[str, Any], n: int) -> None:
    provider: FakeProvider = _transport_state["provider"]
    assert provider.embed_calls, "no embed calls recorded"
    last_call = provider.embed_calls[-1]
    assert len(last_call) == n, f"expected last embed call to carry {n} texts; got {len(last_call)}: {last_call!r}"


@then("the caller receives the stored vector")
def _then_receives_stored(_transport_state: dict[str, Any]) -> None:
    cache: EmbedCache = _transport_state["cache"]
    results = _transport_state["results"]
    assert results, "no result recorded"
    text, vec = results[-1]
    stored = cache.get(text)
    assert stored is not None, f"cache should still hold {text!r} after read"
    assert stored == vec, f"caller vector {vec!r} != stored vector {stored!r}"


# ===========================================================================
# RETRY — transport_retry.feature
# ===========================================================================


class _ScriptedProvider:
    """Wraps :class:`FakeProvider` with a per-attempt response script."""

    def __init__(self, provider: FakeProvider) -> None:
        self._provider = provider
        self._script: list[Any] = []
        self.attempts: int = 0

    def push(self, item: Any) -> None:
        self._script.append(item)

    def push_n(self, item: Any, n: int) -> None:
        for _ in range(n):
            self._script.append(item)

    def __call__(self) -> list[list[float]]:
        self.attempts += 1
        item: Any
        if self._script:
            item = self._script.pop(0)
        else:
            item = "success"
        if isinstance(item, BaseException):
            self._provider.embed_calls.append(["scripted-failure"])
            raise item
        return self._provider.embed_batch(["scripted-success"])


def _build_retry_policy(state: dict[str, Any]) -> RetryPolicy:
    policy = RetryPolicy(
        max_attempts=state["max_attempts"],
        backoff_factor=state["backoff_s"],
        sleep=state["clock"].sleep,
        clock=state["clock"].now,
        telemetry_sink=state["events"].append,
    )
    state["policy"] = policy
    return policy


@given("a fake provider whose response script the test can program")
def _given_scripted_provider(_transport_state: dict[str, Any]) -> None:
    """Wire the scripted provider + FakeClock for the retry scenarios."""
    scripted = _ScriptedProvider(_transport_state["provider"])
    _transport_state["scripted"] = scripted
    _transport_state["clock"] = FakeClock(start=0.0)


@given(parsers.parse("a transport retry policy wrapping the fake provider with max {n:d} attempts"))
def _given_retry_policy(_transport_state: dict[str, Any], n: int) -> None:
    _transport_state["max_attempts"] = n
    _build_retry_policy(_transport_state)


def _pad_script(scripted: _ScriptedProvider, slot: int) -> None:
    needed = slot - 1 - len(scripted._script)
    if needed > 0:
        scripted.push_n("success", needed)


@given(parsers.parse("the fake provider is scripted to succeed on attempt {n:d}"))
def _given_succeed_on(_transport_state: dict[str, Any], n: int) -> None:
    scripted: _ScriptedProvider = _transport_state["scripted"]
    _pad_script(scripted, n)
    scripted.push("success")


@given(parsers.parse("the fake provider is scripted to raise a transient 503 on attempt {n:d}"))
def _given_503_on(_transport_state: dict[str, Any], n: int) -> None:
    scripted: _ScriptedProvider = _transport_state["scripted"]
    _pad_script(scripted, n)
    scripted.push(RuntimeError("transient 503 from fake provider"))


@given(parsers.parse("the fake provider is scripted to raise a transient 503 on attempts {a:d} and {b:d}"))
def _given_503_on_two(_transport_state: dict[str, Any], a: int, b: int) -> None:
    scripted: _ScriptedProvider = _transport_state["scripted"]
    for slot in sorted([a, b]):
        _pad_script(scripted, slot)
        scripted.push(RuntimeError("transient 503 from fake provider"))


@given("the fake provider is scripted to raise a transient 503 on every attempt")
def _given_503_every(_transport_state: dict[str, Any]) -> None:
    scripted: _ScriptedProvider = _transport_state["scripted"]
    scripted.push_n(RuntimeError("transient 503 from fake provider"), _transport_state["max_attempts"])


@given(parsers.parse("the fake provider is scripted to raise a 401 unauthorised on attempt {n:d}"))
def _given_401_on(_transport_state: dict[str, Any], n: int) -> None:
    scripted: _ScriptedProvider = _transport_state["scripted"]
    _pad_script(scripted, n)
    scripted.push(ClientError(status=401, message="unauthorised"))


@given(parsers.parse("the fake provider is scripted to raise a 403 forbidden on attempt {n:d}"))
def _given_403_on(_transport_state: dict[str, Any], n: int) -> None:
    scripted: _ScriptedProvider = _transport_state["scripted"]
    _pad_script(scripted, n)
    scripted.push(ClientError(status=403, message="forbidden"))


@given(parsers.parse("the fake provider is scripted to raise a 404 not-found on attempt {n:d}"))
def _given_404_on(_transport_state: dict[str, Any], n: int) -> None:
    scripted: _ScriptedProvider = _transport_state["scripted"]
    _pad_script(scripted, n)
    scripted.push(ClientError(status=404, message="not found"))


@given(parsers.parse("the retry policy uses a base backoff of {ms:d} milliseconds"))
def _given_backoff(_transport_state: dict[str, Any], ms: int) -> None:
    _transport_state["backoff_s"] = ms / 1000.0
    _build_retry_policy(_transport_state)


@when("the caller dispatches one embed request")
def _when_dispatch_one(_transport_state: dict[str, Any]) -> None:
    """Drive one call through the active policy (retry or timeout).

    The dispatch routes through whichever policy is wired:
    - timeout-only test: drive through TimeoutBudget.with_timeout
    - retry-only test: drive through RetryPolicy.with_retry
    Both store result/raised on the state so downstream Then steps work.
    """
    if _transport_state["policy"] is not None:
        _dispatch_through_retry(_transport_state)
    elif _transport_state["budget"] is not None:
        _dispatch_through_budget(_transport_state, budget_override=None)
    else:
        raise AssertionError("neither retry policy nor timeout budget is wired — Background did not set one up")


def _dispatch_through_retry(state: dict[str, Any]) -> None:
    policy: RetryPolicy = state["policy"]
    scripted: _ScriptedProvider = state["scripted"]
    try:
        state["result"] = policy.with_retry(scripted)
    except (RetryExhausted, ClientError) as exc:
        state["raised"] = exc


def _dispatch_through_budget(state: dict[str, Any], budget_override: int | None) -> None:
    budget: TimeoutBudget = state["budget"]
    provider: FakeProvider = state["provider"]
    start = time.monotonic()
    try:
        if budget_override is None:
            state["result"] = budget.with_timeout(lambda: provider.embed_batch(["x"]))
        else:
            state["result"] = budget.with_timeout(lambda: provider.embed_batch(["x"]), budget_ms=budget_override)
    except TimeoutExceeded as exc:
        state["raised"] = exc
    state["elapsed_ms"] = (time.monotonic() - start) * 1000.0


@then("the caller receives a successful embed response")
def _then_retry_successful(_transport_state: dict[str, Any]) -> None:
    """Retry-feature success: result is a non-empty embedding list."""
    assert _transport_state["raised"] is None, f"expected success; got exception {_transport_state['raised']!r}"
    result = _transport_state["result"]
    assert result is not None, "expected a successful result; got None"
    assert result and result[0], f"expected non-empty embedding result; got {result!r}"


@then(parsers.parse("the fake provider records exactly {n:d} attempt"))
def _then_n_attempts_singular(_transport_state: dict[str, Any], n: int) -> None:
    scripted: _ScriptedProvider = _transport_state["scripted"]
    assert scripted.attempts == n, f"expected {n} attempt; got {scripted.attempts}"


@then(parsers.parse("the fake provider records exactly {n:d} attempts"))
def _then_n_attempts_plural(_transport_state: dict[str, Any], n: int) -> None:
    scripted: _ScriptedProvider = _transport_state["scripted"]
    assert scripted.attempts == n, f"expected {n} attempts; got {scripted.attempts}"


@then("the caller sees a RetryExhausted error")
def _then_retry_exhausted(_transport_state: dict[str, Any]) -> None:
    raised = _transport_state["raised"]
    assert isinstance(raised, RetryExhausted), f"expected RetryExhausted; got {type(raised).__name__}: {raised!r}"


@then(parsers.parse("the RetryExhausted error reports {n:d} attempts made"))
def _then_retry_exhausted_attempts(_transport_state: dict[str, Any], n: int) -> None:
    raised = _transport_state["raised"]
    assert isinstance(raised, RetryExhausted)
    assert raised.attempts == n, f"RetryExhausted.attempts: expected {n}, got {raised.attempts}"


@then("the RetryExhausted error wraps the last underlying transient cause")
def _then_retry_exhausted_cause(_transport_state: dict[str, Any]) -> None:
    raised = _transport_state["raised"]
    assert isinstance(raised, RetryExhausted)
    assert raised.last_cause is not None, "RetryExhausted should wrap the last transient cause; got None"
    assert isinstance(raised.last_cause, RuntimeError), (
        f"expected last_cause to be RuntimeError; got {type(raised.last_cause).__name__}"
    )


@then(parsers.parse("the caller sees a typed client error reporting status {status:d}"))
def _then_client_error(_transport_state: dict[str, Any], status: int) -> None:
    raised = _transport_state["raised"]
    assert isinstance(raised, ClientError), f"expected ClientError; got {type(raised).__name__}: {raised!r}"
    assert raised.status == status, f"ClientError.status: expected {status}, got {raised.status}"


@then(parsers.parse("the elapsed time between attempt {a:d} and attempt {b:d} is at least {ms:d} milliseconds"))
def _then_elapsed_between(_transport_state: dict[str, Any], a: int, b: int, ms: int) -> None:
    events: list[AttemptEvent] = _transport_state["events"]
    by_attempt = {e.attempt: e for e in events}
    assert a in by_attempt and b in by_attempt, f"missing attempt event(s) for {a} and {b}: have {sorted(by_attempt)}"
    elapsed_ms = (by_attempt[b].timestamp - by_attempt[a].timestamp) * 1000.0
    assert elapsed_ms >= ms, f"elapsed between attempt {a} and {b}: expected ≥ {ms}ms, got {elapsed_ms:.2f}ms"


@then(parsers.parse("the transport telemetry records {n:d} attempt events"))
def _then_telemetry_count(_transport_state: dict[str, Any], n: int) -> None:
    events: list[AttemptEvent] = _transport_state["events"]
    assert len(events) == n, f"expected {n} attempt events; got {len(events)}: {events!r}"


@then("each attempt event carries its sequential attempt number")
def _then_telemetry_sequential(_transport_state: dict[str, Any]) -> None:
    events: list[AttemptEvent] = _transport_state["events"]
    assert events, "no telemetry events recorded"
    numbers = [e.attempt for e in events]
    assert numbers == list(range(1, len(numbers) + 1)), f"attempt numbers not sequential: {numbers}"


# ===========================================================================
# TIMEOUT — transport_timeout.feature
# ===========================================================================


@given("a fake provider that delays each response by a programmable interval")
def _given_timeout_provider(_transport_state: dict[str, Any]) -> None:
    """Anchor — Background step. Provider already wired by the universal fixture."""
    assert _transport_state["provider"] is not None


@given(
    parsers.parse("a transport timeout policy wrapping the fake provider with a {ms:d} millisecond per-request timeout")
)
def _given_budget(_transport_state: dict[str, Any], ms: int) -> None:
    _transport_state["default_budget_ms"] = ms
    budget = TimeoutBudget(
        budget_ms=ms,
        counter=_transport_state["provider"],
        executor=_transport_state["executor"],
    )
    _transport_state["budget"] = budget


@given("a fixture-tracked socket counter starting at zero")
def _given_counter_zero(_transport_state: dict[str, Any]) -> None:
    provider: FakeProvider = _transport_state["provider"]
    assert provider.opened == 0
    assert provider.closed == 0
    assert provider.peak_open == 0


@given(parsers.parse("the fake provider is configured to delay each response by {ms:d} milliseconds"))
def _given_delay(_transport_state: dict[str, Any], ms: int) -> None:
    provider: FakeProvider = _transport_state["provider"]
    provider._embed_delay_s = ms / 1000.0


@given(parsers.parse("the transport timeout policy is composed with a {ms:d} millisecond coalescer window"))
def _given_coalesce_compose(_transport_state: dict[str, Any], ms: int) -> None:
    """Wire the timeout dispatcher for the composition scenario.

    Architecture decision: every caller in a coalesced batch that
    times out sees the typed :class:`TimeoutExceeded`. The coalescer's
    base contract ("never raises, returns []") is overridden via a
    thin wrapper that parks the typed exception and re-raises it to
    each caller after the coalescer's Future resolves with [].
    """
    provider: FakeProvider = _transport_state["provider"]
    budget: TimeoutBudget = _transport_state["budget"]

    def _timed_batch(texts: list[str]) -> list[list[float]]:
        return budget.with_timeout(lambda: provider.embed_batch(texts))

    class _CoalescedTimeoutDispatcher:
        def __init__(self) -> None:
            self.batched_attempts = 0
            self._inner = EmbedCoalescer(
                embed_batch_fn=self._scoped_batch,
                coalesce_window_ms=ms,
                max_batch_size=32,
            )
            self._error_lock = threading.Lock()
            self._current_error: TimeoutExceeded | None = None

        def _scoped_batch(self, texts: list[str]) -> list[list[float]]:
            self.batched_attempts += 1
            try:
                return _timed_batch(texts)
            except TimeoutExceeded as exc:
                with self._error_lock:
                    self._current_error = exc
                raise

        def embed_or_timeout(self, text: str) -> list[float]:
            vec = self._inner.embed(text)
            if not vec:
                with self._error_lock:
                    error = self._current_error
                if error is not None:
                    raise error
            return vec

        def shutdown(self) -> None:
            self._inner.shutdown()

    _transport_state["timeout_dispatcher"] = _CoalescedTimeoutDispatcher()


@when(parsers.parse("the caller dispatches one embed request with a {ms:d} millisecond per-call timeout override"))
def _when_dispatch_with_override(_transport_state: dict[str, Any], ms: int) -> None:
    _dispatch_through_budget(_transport_state, budget_override=ms)


@when(parsers.parse("the caller dispatches {n:d} embed requests each timing out"))
def _when_dispatch_n_timeouts(_transport_state: dict[str, Any], n: int) -> None:
    budget: TimeoutBudget = _transport_state["budget"]
    provider: FakeProvider = _transport_state["provider"]
    raised: list[TimeoutExceeded] = []
    for _ in range(n):
        try:
            budget.with_timeout(lambda: provider.embed_batch(["x"]))
        except TimeoutExceeded as exc:
            raised.append(exc)
    _transport_state["raised_per_caller"] = raised


def _coalesced_timeout_fan_out(state: dict[str, Any], n: int, dispatcher: Any) -> None:
    raised: list[BaseException] = []
    raised_lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            dispatcher.embed_or_timeout(f"text-{i}")
        except Exception as exc:
            # Capture every exception type for per-caller assertion;
            # threads can't raise BaseException-only forms here.
            with raised_lock:
                raised.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    state["raised_per_caller"] = raised


@then(parsers.parse("the caller receives a successful embed response within {ms:d} milliseconds"))
def _then_success_within(_transport_state: dict[str, Any], ms: int) -> None:
    assert _transport_state["raised"] is None, f"expected success; got {_transport_state['raised']!r}"
    result = _transport_state["result"]
    assert result and result[0], f"expected non-empty result; got {result!r}"
    elapsed = _transport_state["elapsed_ms"]
    assert elapsed is not None
    assert elapsed < ms, f"elapsed {elapsed:.1f}ms exceeded budget {ms}ms"


@then("the fixture-tracked socket counter shows opened equals closed")
def _then_balanced(_transport_state: dict[str, Any]) -> None:
    provider: FakeProvider = _transport_state["provider"]
    assert provider.opened == provider.closed, (
        f"socket counter unbalanced: opened={provider.opened}, closed={provider.closed}"
    )


@then(parsers.parse("the caller sees a TimeoutExceeded error within {ms:d} milliseconds"))
def _then_timeout_within(_transport_state: dict[str, Any], ms: int) -> None:
    raised = _transport_state["raised"]
    assert isinstance(raised, TimeoutExceeded), f"expected TimeoutExceeded; got {type(raised).__name__}: {raised!r}"
    elapsed = _transport_state["elapsed_ms"]
    assert elapsed is not None
    assert elapsed < ms, f"timeout fired late: elapsed={elapsed:.1f}ms, budget={ms}ms"


@then("the caller sees a TimeoutExceeded error")
def _then_timeout_seen(_transport_state: dict[str, Any]) -> None:
    raised = _transport_state["raised"]
    assert isinstance(raised, TimeoutExceeded), f"expected TimeoutExceeded; got {type(raised).__name__}: {raised!r}"


@then(parsers.parse("the TimeoutExceeded error reports the configured {ms:d} millisecond budget"))
def _then_reports_configured(_transport_state: dict[str, Any], ms: int) -> None:
    raised = _transport_state["raised"]
    assert isinstance(raised, TimeoutExceeded)
    assert raised.budget_ms == ms, f"TimeoutExceeded.budget_ms: expected {ms}, got {raised.budget_ms}"


@then(parsers.parse("the TimeoutExceeded error reports the override {ms:d} millisecond budget"))
def _then_reports_override(_transport_state: dict[str, Any], ms: int) -> None:
    raised = _transport_state["raised"]
    assert isinstance(raised, TimeoutExceeded)
    assert raised.budget_ms == ms, f"TimeoutExceeded.budget_ms: expected override {ms}, got {raised.budget_ms}"


@then("every caller sees a TimeoutExceeded error")
def _then_every_timeout(_transport_state: dict[str, Any]) -> None:
    raised = _transport_state["raised_per_caller"]
    assert raised, "no callers raised"
    for i, exc in enumerate(raised):
        assert isinstance(exc, TimeoutExceeded), (
            f"caller {i}: expected TimeoutExceeded; got {type(exc).__name__}: {exc!r}"
        )


@then(parsers.parse("the fixture-tracked socket counter peak open value never exceeds {n:d} per concurrent request"))
def _then_peak_open_bound(_transport_state: dict[str, Any], n: int) -> None:
    provider: FakeProvider = _transport_state["provider"]
    assert provider.peak_open <= n, f"peak_open exceeded {n}: got {provider.peak_open}"


@then(parsers.parse("the fake provider records exactly {n:d} batched embed call attempted"))
def _then_batched_attempts(_transport_state: dict[str, Any], n: int) -> None:
    dispatcher = _transport_state["timeout_dispatcher"]
    assert dispatcher is not None, "coalescer composition fixture not wired"
    assert dispatcher.batched_attempts == n, (
        f"expected exactly {n} batched attempt(s); got {dispatcher.batched_attempts}"
    )
