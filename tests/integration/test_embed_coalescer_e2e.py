"""Integration: embed coalescer wired into embed_text folds Azure calls (#288).

Boundary chain:
  caller -> embed_text -> EmbedCache (miss) -> EmbedCoalescer -> fake batch fn
                                                                  (closes over fake client)
  -> caller's Future resolves with per-text vector

The fake client lives inside the batch_fn closure so the same
production wiring runs (cache → coalescer → batch dispatcher → SDK
shape) without touching Azure. F1-clean (no @patch on kairix
internals), F5-clean (no private-name imports — the coalescer + cache
classes are part of the public surface for #288/#285).
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from kairix.core.embed import embed_cache as embed_cache_mod
from kairix.core.embed import embed_coalescer as embed_coalescer_mod
from kairix.core.embed import embed_text
from kairix.core.embed.embed_cache import EmbedCache, reset_embed_cache
from kairix.core.embed.embed_coalescer import EmbedCoalescer, reset_embed_coalescer

pytestmark = pytest.mark.integration

# F17: lift the shared test deployment label.
_TEST_DEPLOYMENT = "test-deployment"


# ---------------------------------------------------------------------------
# Counting fake Azure client + batch wiring
# ---------------------------------------------------------------------------


class _EmbedItem:
    """Stub the openai SDK's response[i] shape — only ``.embedding`` is read."""

    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _EmbedResponse:
    """Stub the openai SDK's create() return shape — only ``.data`` is read."""

    def __init__(self, embeddings: list[list[float]]) -> None:
        self.data = [_EmbedItem(e) for e in embeddings]


class _CountingEmbeddings:
    def __init__(self, owner: _CountingClient) -> None:
        self._owner = owner

    def create(self, *, model: str, input: list[str], dimensions: int) -> _EmbedResponse:
        self._owner.calls.append({"model": model, "input": list(input), "dimensions": dimensions})
        # Deterministic per-text vector so callers can verify alignment.
        return _EmbedResponse([[float(len(t)), 0.5, 1.5] for t in input])


class _CountingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.embeddings = _CountingEmbeddings(self)


def _make_batch_fn(client: _CountingClient, deployment: str = _TEST_DEPLOYMENT) -> Any:
    """Build a coalescer batch_fn that closes over a fake client.

    Mirrors the production batch dispatcher's shape (it calls
    ``client.embeddings.create(model=..., input=..., dimensions=...)``)
    so the integration test exercises the same SDK contract.
    """

    def batch(texts: list[str]) -> list[list[float]]:
        response = client.embeddings.create(model=deployment, input=texts, dimensions=3)
        return [list(item.embedding) for item in response.data]

    return batch


@pytest.fixture
def _wire(monkeypatch: pytest.MonkeyPatch) -> tuple[_CountingClient, EmbedCoalescer, EmbedCache]:
    """Install a fresh cache + coalescer singleton owning a counting client.

    F2-clean: every object is constructed directly and substituted into
    the module singleton via ``setattr`` on a public attribute. No env
    monkeypatching. F5-clean: no private-name imports — the coalescer
    + cache classes are part of the public test surface.
    """
    reset_embed_cache()
    reset_embed_coalescer()
    cache = EmbedCache(max_entries=10, max_age_s=60.0)
    monkeypatch.setattr(embed_cache_mod, "_EMBED_CACHE", cache)

    client = _CountingClient()
    coalescer = EmbedCoalescer(
        embed_batch_fn=_make_batch_fn(client),
        coalesce_window_ms=200,
        max_batch_size=64,
    )
    monkeypatch.setattr(embed_coalescer_mod, "_EMBED_COALESCER", coalescer)

    yield client, coalescer, cache
    coalescer.shutdown()
    reset_embed_cache()
    reset_embed_coalescer()


# ---------------------------------------------------------------------------
# 10 concurrent embed_text calls → 1 Azure batch call
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ten_concurrent_embed_text_calls_become_one_batch(
    _wire: tuple[_CountingClient, EmbedCoalescer, EmbedCache],
) -> None:
    """The headline behaviour — 10 concurrent embed_text → 1 Azure batch.

    Sabotage: bypass the coalescer in ``embed_text`` (drop the
    ``_route_through_coalescer`` call) and each caller drives its own
    Azure roundtrip — client.calls grows to 10, this assertion fires.
    """
    client, _coalescer, _cache = _wire
    results: dict[str, list[float]] = {}
    results_lock = threading.Lock()
    errors: list[BaseException] = []

    def worker(text: str) -> None:
        try:
            vec = embed_text(text, deployment=_TEST_DEPLOYMENT)
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
    assert len(client.calls) == 1, f"expected exactly 1 Azure batch call; got {len(client.calls)}: {client.calls!r}"
    assert len(client.calls[0]["input"]) == 10

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
    _wire: tuple[_CountingClient, EmbedCoalescer, EmbedCache],
) -> None:
    """A second embed_text of the same text returns from cache; no batch.

    Sabotage: drop the ``cache.get(text)`` short-circuit before
    ``_route_through_coalescer`` in embed_text and the second call
    re-enters the coalescer — client.calls grows to 2.
    """
    client, coalescer, _cache = _wire
    first = embed_text("FEAT-288 status", deployment=_TEST_DEPLOYMENT)
    second = embed_text("FEAT-288 status", deployment=_TEST_DEPLOYMENT)
    assert first == second
    assert len(client.calls) == 1
    # The coalescer should have seen exactly 1 request (the cache miss).
    assert coalescer.stats().requests == 1


# ---------------------------------------------------------------------------
# Sequential calls still produce results (single-caller window path)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_sequential_calls_return_within_bounded_window(
    _wire: tuple[_CountingClient, EmbedCoalescer, EmbedCache],
) -> None:
    """Single caller doesn't hang — dispatcher fires on window timeout.

    Sabotage: remove the ``wait(timeout=self._window_s)`` and a single
    caller blocks forever — pytest times out instead of asserting.
    """
    client, _coalescer, _cache = _wire
    start = time.monotonic()
    out = embed_text("solo-text", deployment=_TEST_DEPLOYMENT)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert out, "single embed_text call returned empty"
    # Window=200ms; allow generous upper bound for CI flakes.
    assert 150 <= elapsed_ms < 2000, f"single embed_text latency out of band: {elapsed_ms:.1f}ms"
    assert len(client.calls) == 1
    assert client.calls[0]["input"] == ["solo-text"]


# ---------------------------------------------------------------------------
# Empty text short-circuits before either cache or coalescer
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_empty_text_bypasses_both_cache_and_coalescer(
    _wire: tuple[_CountingClient, EmbedCoalescer, EmbedCache],
) -> None:
    """An empty embed_text call returns [] without touching anything.

    Sabotage: drop the ``if not text or not text.strip()`` guard at
    the top of embed_text and the empty string flows into the
    coalescer — client.calls grows.
    """
    client, coalescer, _cache = _wire
    assert embed_text("", deployment=_TEST_DEPLOYMENT) == []
    assert embed_text("   ", deployment=_TEST_DEPLOYMENT) == []
    # Wait past the window so any (sabotaged) dispatch would have fired.
    time.sleep(0.25)
    assert client.calls == []
    assert coalescer.stats().requests == 0


# ---------------------------------------------------------------------------
# Cache populated after coalesced dispatch
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_coalesced_results_populate_the_cache(
    _wire: tuple[_CountingClient, EmbedCoalescer, EmbedCache],
) -> None:
    """After the coalesced batch resolves, the cache holds each text.

    Sabotage: drop the ``cache.put(text, coalesced)`` after the
    coalesced path returns in embed_text and the next same-text call
    re-routes through the coalescer instead of hitting the cache.
    """
    client, _coalescer, cache = _wire
    results: list[list[float]] = []
    lock = threading.Lock()

    def worker(text: str) -> None:
        vec = embed_text(text, deployment=_TEST_DEPLOYMENT)
        with lock:
            results.append(vec)

    texts = ["alpha", "bravo", "charlie"]
    threads = [threading.Thread(target=worker, args=(t,)) for t in texts]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(client.calls) == 1
    assert cache.stats().size == 3

    # Second wave — all three should be cache hits, no new batch.
    for t in texts:
        embed_text(t, deployment=_TEST_DEPLOYMENT)
    assert len(client.calls) == 1, "cache hits should not re-enter the coalescer"
