"""Step definitions for embed_coalescer.feature (#288).

Drives the real :class:`kairix.core.embed.embed_coalescer.EmbedCoalescer`
with a counting fake batch function so each scenario can pin call
count + batch sizes. F1-clean (no @patch on internals), F2-clean (no
env monkeypatch), F5-clean (no private-name imports — the coalescer
class is part of the public surface for #288).
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from pytest_bdd import given, then, when

from kairix.core.embed.embed_coalescer import EmbedCoalescer

pytestmark = pytest.mark.bdd

# F17: lift repeated phrase fragments to module-level constants.
_PHRASE_BACKEND_NOT_CALLED = "the embed batch backend was not called"


class _CountingBatchFn:
    """Records every batched call so the scenario can pin counts."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._lock = threading.Lock()

    def __call__(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self.calls.append(list(texts))
        return [[float(len(t)), 0.5] for t in texts]


@pytest.fixture
def _coalescer_state() -> Any:
    """Per-scenario coalescer state.

    Window=200 ms keeps the 10-thread scenario from accidentally
    splitting into multiple batches on slow CI; max_batch_size=64 lets
    every thread land in one batch. Each scenario gets its own
    coalescer so state is per-scenario.
    """
    fake = _CountingBatchFn()
    coalescer = EmbedCoalescer(embed_batch_fn=fake, coalesce_window_ms=200, max_batch_size=64)
    state: dict[str, Any] = {
        "fake": fake,
        "coalescer": coalescer,
        "elapsed_ms": None,
    }
    yield state
    coalescer.shutdown()


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("an embed coalescer with a counting batch backend")
def _given_coalescer(_coalescer_state: dict[str, Any]) -> None:
    """No-op — the coalescer is wired by the fixture above."""


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("ten threads each call the coalescer with their own text in the same window")
def _when_ten_concurrent(_coalescer_state: dict[str, Any]) -> None:
    """Spin up 10 threads that all submit within the coalesce window."""
    coalescer: EmbedCoalescer = _coalescer_state["coalescer"]
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
    _coalescer_state["results"] = results
    _coalescer_state["errors"] = errors


@when("a single caller asks the coalescer to embed one text")
def _when_single_caller(_coalescer_state: dict[str, Any]) -> None:
    """One thread — no batch ever fills, dispatcher must fire on timeout."""
    coalescer: EmbedCoalescer = _coalescer_state["coalescer"]
    start = time.monotonic()
    out = coalescer.embed("solo")
    _coalescer_state["elapsed_ms"] = (time.monotonic() - start) * 1000
    _coalescer_state["solo_result"] = out


@when("some caller asks the coalescer to embed an empty text")
def _when_empty(_coalescer_state: dict[str, Any]) -> None:
    """Empty text never reaches the queue."""
    coalescer: EmbedCoalescer = _coalescer_state["coalescer"]
    _coalescer_state["empty_result"] = coalescer.embed("")


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the embed batch backend was called exactly once")
def _then_one_batch(_coalescer_state: dict[str, Any]) -> None:
    """Sabotage: remove the wait/window mechanism and each thread
    fires its own batch — call count explodes past 1.
    """
    fake: _CountingBatchFn = _coalescer_state["fake"]
    assert len(fake.calls) == 1, f"expected exactly 1 batched call; got {len(fake.calls)}: {fake.calls!r}"


@then("the single batch contained ten texts")
def _then_ten_texts(_coalescer_state: dict[str, Any]) -> None:
    """Sabotage: cap the batch to 1 (e.g. drop the append to _pending and
    dispatch immediately) and the batch shrinks below 10.
    """
    fake: _CountingBatchFn = _coalescer_state["fake"]
    assert len(fake.calls[0]) == 10, f"expected the single batch to contain 10 texts; got {len(fake.calls[0])}"


@then("the call returns within the bounded coalesce window")
def _then_bounded_window(_coalescer_state: dict[str, Any]) -> None:
    """Sabotage: drop the wait(timeout=window_s) and the single-caller
    case blocks forever — pytest times out instead of returning.
    """
    elapsed_ms = _coalescer_state["elapsed_ms"]
    # Window is 200ms; assert it returned in a sensible band. The
    # lower bound catches a sabotage that disables coalescing entirely
    # (window=0 path), the upper bound catches a hang.
    assert elapsed_ms is not None
    assert 150 <= elapsed_ms < 1500, f"single-caller latency out of band: {elapsed_ms:.1f}ms (window=200ms)"
    # The single caller should still get a non-empty result.
    assert _coalescer_state["solo_result"], "single caller got empty result"


@then(_PHRASE_BACKEND_NOT_CALLED)
def _then_not_called(_coalescer_state: dict[str, Any]) -> None:
    """Sabotage: drop the empty-text guard in embed() and the empty
    string lands in the buffer — the dispatcher fires a batch and
    fake.calls goes non-empty.
    """
    fake: _CountingBatchFn = _coalescer_state["fake"]
    # Wait past the window so a sabotaged-in batch dispatch would have
    # already fired by now.
    time.sleep(0.25)
    assert fake.calls == [], f"empty input should not have reached the backend; got {fake.calls!r}"
    # And the empty caller got [].
    assert _coalescer_state["empty_result"] == []


@then("the coalescer reports zero requests")
def _then_zero_requests(_coalescer_state: dict[str, Any]) -> None:
    """Sabotage: count empty-text submissions against requests and the
    operator dashboard double-counts what was supposed to be a
    short-circuited no-op.
    """
    coalescer: EmbedCoalescer = _coalescer_state["coalescer"]
    stats = coalescer.stats()
    assert stats.requests == 0, f"empty calls should not count as requests; got {stats.requests}"
