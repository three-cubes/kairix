"""Integration: embed coalescer wired into ProviderEmbeddingService folds provider calls (#288).

Boundary chain:
  caller -> ProviderEmbeddingService.embed -> EmbedCache (miss) ->
            EmbedCoalescer -> FakeProvider.embed_batch (one batch)
  -> caller's Future resolves with per-text vector

The provider is a :class:`FakeProvider` from ``tests/fakes.py`` whose
``embed_batch`` records every batch invocation so the test can pin
"10 concurrent embed calls become 1 batch invocation". F1-clean (no
@patch on kairix internals), F5-clean (no private-name imports — the
adapter + coalescer + cache classes are part of the public surface
for #288/#285).
"""

from __future__ import annotations

import threading
import time

import pytest

from kairix.transport.cache import embed_cache as embed_cache_mod
from kairix.transport.cache.embed_cache import EmbedCache, reset_embed_cache
from kairix.transport.coalesce import EmbedCoalescer, reset_embed_coalescer
from kairix.transport.coalesce import embed_coalescer as embed_coalescer_mod
from kairix.transport.embed_service import ProviderEmbeddingService

pytestmark = pytest.mark.integration


class _DeterministicProvider:
    """FakeProvider variant whose ``embed_batch`` returns a deterministic
    per-text vector so callers can verify alignment.

    Matches the :class:`kairix.providers.Provider` Protocol surface used
    by ``ProviderEmbeddingService.embed`` (the only method exercised on
    the embed hot path).
    """

    def __init__(self) -> None:
        self.embed_calls: list[list[str]] = []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        # First component encodes len(text) so a misalignment surfaces.
        return [[float(len(t)), 0.5, 1.5] for t in texts]


@pytest.fixture
def _wire(monkeypatch: pytest.MonkeyPatch) -> tuple[_DeterministicProvider, EmbedCoalescer, EmbedCache]:
    """Install a fresh cache + coalescer singleton wired to a counting provider.

    F2-clean: every object is constructed directly and substituted into
    the module singleton via ``setattr`` on a public attribute. No env
    monkeypatching. F5-clean: no private-name imports — the coalescer
    + cache classes are part of the public test surface.
    """
    reset_embed_cache()
    reset_embed_coalescer()
    cache = EmbedCache(max_entries=10, max_age_s=60.0)
    monkeypatch.setattr(embed_cache_mod, "_EMBED_CACHE", cache)

    provider = _DeterministicProvider()
    coalescer = EmbedCoalescer(
        embed_batch_fn=provider.embed_batch,
        coalesce_window_ms=200,
        max_batch_size=64,
    )
    monkeypatch.setattr(embed_coalescer_mod, "_EMBED_COALESCER", coalescer)

    yield provider, coalescer, cache
    coalescer.shutdown()
    reset_embed_cache()
    reset_embed_coalescer()


# ---------------------------------------------------------------------------
# 10 concurrent embed calls → 1 provider batch call
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ten_concurrent_embed_calls_become_one_batch(
    _wire: tuple[_DeterministicProvider, EmbedCoalescer, EmbedCache],
) -> None:
    """The headline behaviour — 10 concurrent embeds → 1 provider batch.

    Sabotage: bypass the coalescer in ``ProviderEmbeddingService.embed``
    (drop the ``existing.embed(text)`` short-circuit when the singleton
    is installed) and each caller drives its own provider round-trip —
    ``embed_calls`` grows to 10, this assertion fires.
    """
    provider, _coalescer, _cache = _wire
    # Provider Protocol satisfied structurally; the adapter only needs
    # embed_batch. mypy doesn't know our _DeterministicProvider matches
    # because it lives inline in this test file.
    service = ProviderEmbeddingService(provider)  # type: ignore[arg-type] — _DeterministicProvider satisfies the Protocol structurally; mypy can't see it lives in this test file

    results: dict[str, list[float]] = {}
    results_lock = threading.Lock()
    errors: list[BaseException] = []

    def worker(text: str) -> None:
        try:
            vec = service.embed(text)
            with results_lock:
                results[text] = vec
        except Exception as exc:
            errors.append(exc)

    texts = [f"text-{i}" for i in range(10)]
    threads = [threading.Thread(target=worker, args=(t,)) for t in texts]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, f"workers raised: {errors!r}"
    assert len(provider.embed_calls) == 1, (
        f"expected exactly 1 provider batch call; got {len(provider.embed_calls)}: {provider.embed_calls!r}"
    )
    assert len(provider.embed_calls[0]) == 10

    # Each caller got back its own embedding (the fake assigns the
    # first vector component to ``len(text)`` so a misalignment surfaces).
    for text, vec in results.items():
        assert vec, f"worker {text!r} got an empty result"
        assert vec[0] == float(len(text)), f"worker {text!r} got vector aligned to the wrong text: {vec!r}"


# ---------------------------------------------------------------------------
# Cache hit short-circuits before the coalescer
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cache_hit_skips_the_coalescer(
    _wire: tuple[_DeterministicProvider, EmbedCoalescer, EmbedCache],
) -> None:
    """A second embed of the same text returns from cache; no batch.

    Sabotage: drop the ``cache.get(text)`` short-circuit before the
    coalescer routing in ``ProviderEmbeddingService.embed`` and the
    second call re-enters the coalescer — ``embed_calls`` grows to 2.
    """
    provider, coalescer, _cache = _wire
    service = ProviderEmbeddingService(provider)  # type: ignore[arg-type] — _DeterministicProvider satisfies the Protocol structurally; mypy can't see it lives in this test file
    first = service.embed("FEAT-288 status")
    second = service.embed("FEAT-288 status")
    assert first == second
    assert len(provider.embed_calls) == 1
    # The coalescer should have seen exactly 1 request (the cache miss).
    assert coalescer.stats().requests == 1


# ---------------------------------------------------------------------------
# Sequential calls still produce results (single-caller window path)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_sequential_calls_return_within_bounded_window(
    _wire: tuple[_DeterministicProvider, EmbedCoalescer, EmbedCache],
) -> None:
    """Single caller doesn't hang — dispatcher fires on window timeout.

    Sabotage: remove the ``wait(timeout=self._window_s)`` and a single
    caller blocks forever — pytest times out instead of asserting.
    """
    provider, _coalescer, _cache = _wire
    service = ProviderEmbeddingService(provider)  # type: ignore[arg-type] — _DeterministicProvider satisfies the Protocol structurally; mypy can't see it lives in this test file
    start = time.monotonic()
    out = service.embed("solo-text")
    elapsed_ms = (time.monotonic() - start) * 1000
    assert out, "single embed call returned empty"
    # Window=200ms; allow generous upper bound for CI flakes.
    assert 150 <= elapsed_ms < 2000, f"single embed latency out of band: {elapsed_ms:.1f}ms"
    assert len(provider.embed_calls) == 1
    assert provider.embed_calls[0] == ["solo-text"]


# ---------------------------------------------------------------------------
# Empty text short-circuits before either cache or coalescer
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_empty_text_bypasses_both_cache_and_coalescer(
    _wire: tuple[_DeterministicProvider, EmbedCoalescer, EmbedCache],
) -> None:
    """An empty embed call returns [] without touching anything.

    Sabotage: drop the ``if not text or not text.strip()`` guard at
    the top of ``ProviderEmbeddingService.embed`` and the empty string
    flows into the coalescer — ``embed_calls`` grows.
    """
    provider, coalescer, _cache = _wire
    service = ProviderEmbeddingService(provider)  # type: ignore[arg-type] — _DeterministicProvider satisfies the Protocol structurally; mypy can't see it lives in this test file
    assert service.embed("") == []
    assert service.embed("   ") == []
    # Wait past the window so any (sabotaged) dispatch would have fired.
    time.sleep(0.25)
    assert provider.embed_calls == []
    assert coalescer.stats().requests == 0


# ---------------------------------------------------------------------------
# Cache populated after coalesced dispatch
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_coalesced_results_populate_the_cache(
    _wire: tuple[_DeterministicProvider, EmbedCoalescer, EmbedCache],
) -> None:
    """After the coalesced batch resolves, the cache holds each text.

    Sabotage: drop the ``cache.put(text, result)`` after the coalesced
    path returns in ``ProviderEmbeddingService.embed`` and the next
    same-text call re-routes through the coalescer instead of hitting
    the cache.
    """
    provider, _coalescer, cache = _wire
    service = ProviderEmbeddingService(provider)  # type: ignore[arg-type] — _DeterministicProvider satisfies the Protocol structurally; mypy can't see it lives in this test file
    results: list[list[float]] = []
    lock = threading.Lock()

    def worker(text: str) -> None:
        vec = service.embed(text)
        with lock:
            results.append(vec)

    texts = ["alpha", "bravo", "charlie"]
    threads = [threading.Thread(target=worker, args=(t,)) for t in texts]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(provider.embed_calls) == 1
    assert cache.stats().size == 3

    # Second wave — all three should be cache hits, no new batch.
    for t in texts:
        service.embed(t)
    assert len(provider.embed_calls) == 1, "cache hits should not re-enter the coalescer"
