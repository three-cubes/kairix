"""Tests for the dispatch threadpool sizing in ``async_tool_handler``.

Issue #403 — MCP search reported 36s latency on production while CLI on the
same warm pipeline completed in 12s. Root cause confirmed via
``/tmp/instrument_mcp_concurrent.py``: ``asyncio.to_thread`` used the event
loop's default executor, which on CPython 3.12 is sized
``min(32, cpu_count + 4)``. On a 2-CPU production container that is six
worker threads. Six concurrent dogfood agents firing search + entity +
brief calls saturated the pool; the seventh call queued behind whichever
slot freed first, accumulating the observed 24s gap.

The fix routes every tool dispatch through a kairix-owned
``ThreadPoolExecutor`` whose ``max_workers`` defaults to 32 (env override
``KAIRIX_MCP_DISPATCH_WORKERS``). Tests exercise the seam by injecting a
known-size pool through ``AsyncToolHandlerDeps.dispatch_executor_fn`` and
asserting that:

* N concurrent calls to a slow handler land on N distinct executor threads
  when the pool is sized to N — the wrapper does NOT serialise to a smaller
  pool below it.

* When the pool is intentionally undersized (max_workers=2), the same N
  calls serialize into ``ceil(N / 2)`` waves — proves the seam is
  load-bearing (the test exists to catch the regression of someone wiring
  the wrapper back to ``asyncio.to_thread`` and the dev-machine default
  pool size masking the production failure).

Tested through public surface only — no private symbols, no @patch /
monkeypatch (F1-clean). All concurrency is driven from the canonical async
test harness (``asyncio.run`` + ``asyncio.gather``); the executor is
explicitly injected via the public Deps seam.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kairix.agents.mcp.errors import (
    DEFAULT_DISPATCH_WORKERS,
    AsyncToolHandlerDeps,
    async_tool_handler,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def obs_executor() -> Iterator[ThreadPoolExecutor]:
    """A throwaway observability executor so the call-log writes don't leak."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-obs")
    try:
        yield pool
    finally:
        pool.shutdown(wait=True)


@pytest.fixture
def silent_db_path(tmp_path: Path) -> Path:
    """Tmp DB path — calls log into a sandbox we don't inspect."""
    return tmp_path / "obs.sqlite"


def _make_wrapped_slow_handler(
    *,
    sleep_s: float,
    dispatch_executor: ThreadPoolExecutor,
    obs_executor: ThreadPoolExecutor,
    db_path: Path,
    started: dict[int, tuple[float, int]],
    barrier: threading.Barrier | None = None,
) -> object:
    """Construct a wrapped handler that records when + on which thread each call ran.

    Returns the async-wrapped callable. The handler sleeps ``sleep_s`` so a
    saturated pool's behaviour is observable in wall-clock terms.

    When ``barrier`` is supplied, the handler records its thread ident and
    then blocks on the barrier BEFORE sleeping. The barrier only releases
    once ``barrier.parties`` calls have all reached it, so every call is
    proven genuinely in-flight on its own worker thread at the same instant
    — no worker can complete and be reused before the others are dispatched.
    That removes the pool-reuse race that made the distinct-thread count
    non-deterministic under CI load (#557).
    """

    def slow_handler(call_id: int = 0) -> dict[str, int]:
        started[call_id] = (time.monotonic(), threading.get_ident())
        if barrier is not None:
            # Hold every worker thread until ALL N calls have arrived, so the
            # pool can't recycle a finished worker before the rest dispatch.
            barrier.wait(timeout=10)
        time.sleep(sleep_s)
        return {"id": call_id}

    return async_tool_handler(
        slow_handler,
        deps=AsyncToolHandlerDeps(
            db_path_fn=lambda: db_path,
            dispatch_executor_fn=lambda: dispatch_executor,
            obs_executor_fn=lambda: obs_executor,
        ),
    )


def test_concurrent_calls_use_distinct_dispatch_threads_when_pool_is_large(
    obs_executor: ThreadPoolExecutor, silent_db_path: Path
) -> None:
    """Eight concurrent calls land on eight distinct threads when the pool is sized to eight.

    This is the post-fix steady-state contract: when the operator-tuned
    dispatch pool is at least as large as the concurrent-agent count,
    every in-flight call runs in parallel with no queueing.

    A ``threading.Barrier(n_calls)`` gates the slow handler: each call
    records its worker thread, then blocks on the barrier. The barrier
    only releases once all eight calls have arrived, so all eight worker
    threads are provably occupied simultaneously — the pool cannot recycle
    a finished worker before the rest dispatch. That makes the exact
    ``== n_calls`` distinct-thread count deterministic and closes the
    pool-reuse race that produced flaky ``7 == 8`` failures under CI load
    (#557).

    Sabotage proof: revert ``async_tool_handler`` to use
    ``asyncio.to_thread(safe, ...)`` instead of
    ``loop.run_in_executor(dispatch_executor, ...)`` — the injected
    eight-worker pool is bypassed, the event loop's default executor
    (``min(32, cpu_count + 4)``) is used, and the eight calls all block
    on the barrier inside fewer worker slots → the barrier never releases
    (deadlock/timeout) on a constrained runner. CONFIRMED locally by
    shrinking the injected dispatch pool below ``n_calls`` (see #557 report).
    """
    n_calls = 8
    started: dict[int, tuple[float, int]] = {}
    barrier = threading.Barrier(n_calls)

    with ThreadPoolExecutor(max_workers=n_calls, thread_name_prefix="test-dispatch") as dispatch:
        wrapped = _make_wrapped_slow_handler(
            sleep_s=0.2,
            dispatch_executor=dispatch,
            obs_executor=obs_executor,
            db_path=silent_db_path,
            started=started,
            barrier=barrier,
        )

        async def fire_all() -> list[dict[str, int]]:
            return await asyncio.gather(*(wrapped(call_id=i) for i in range(n_calls)))

        results = asyncio.run(fire_all())

    assert len(results) == n_calls
    assert {r["id"] for r in results} == set(range(n_calls))
    threads_used = {tid for _, tid in started.values()}
    # The barrier holds every worker until all N calls have arrived, so exactly
    # N distinct dispatch threads are occupied at once — no pool-reuse race.
    assert len(threads_used) == n_calls, (
        f"expected exactly {n_calls} distinct dispatch threads, got {len(threads_used)} "
        "(pool exhaustion / queueing would show fewer; barrier guarantees full parallelism)"
    )


def test_undersized_dispatch_pool_serialises_calls_into_waves(
    obs_executor: ThreadPoolExecutor, silent_db_path: Path
) -> None:
    """Eight concurrent calls on a two-worker pool serialise into four sequential waves.

    Captures the pre-fix production failure shape: when the dispatch pool
    is smaller than the in-flight call count, only ``pool_size`` calls run
    at once and the rest queue. This is the regression that bit production
    in #403 (pool=6, agents=6+ -> seventh call waited a full search
    duration).

    The sabotage proof is the opposite of the previous test: if the
    wrapper ever stops honouring the injected ``dispatch_executor_fn``,
    this test sees eight distinct threads instead of two. The test
    documents the contract via failure mode.
    """
    n_calls = 8
    pool_size = 2
    sleep_s = 0.2
    started: dict[int, tuple[float, int]] = {}

    with ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="test-dispatch") as dispatch:
        wrapped = _make_wrapped_slow_handler(
            sleep_s=sleep_s,
            dispatch_executor=dispatch,
            obs_executor=obs_executor,
            db_path=silent_db_path,
            started=started,
        )

        async def fire_all() -> list[dict[str, int]]:
            return await asyncio.gather(*(wrapped(call_id=i) for i in range(n_calls)))

        t0 = time.monotonic()
        asyncio.run(fire_all())
        elapsed = time.monotonic() - t0

    threads_used = {tid for _, tid in started.values()}
    assert len(threads_used) == pool_size, (
        f"expected exactly {pool_size} threads (pool exhausted); "
        f"saw {len(threads_used)} (was the executor seam bypassed?)"
    )

    # Eight calls / two workers = four waves. Each wave is ``sleep_s``.
    expected_waves = n_calls // pool_size
    lower_bound = expected_waves * sleep_s * 0.9  # 0.9 = generous floor for thread overhead
    assert elapsed >= lower_bound, (
        f"undersized pool should serialise into {expected_waves} waves "
        f"(~{expected_waves * sleep_s:.2f}s); got {elapsed:.2f}s"
    )


def test_dispatch_pool_isolated_from_obs_pool(obs_executor: ThreadPoolExecutor, silent_db_path: Path) -> None:
    """A blocked observability pool does NOT block subsequent tool dispatches.

    Production trace 2026-06-04 also showed ``_record_mcp_call`` blocking
    the event loop in the wrapper's ``finally`` — the fix moved the write
    onto a dedicated single-thread executor. This test proves the two
    pools are independent: the observability write is held open on a
    gate the test only releases AFTER both dispatches have returned, so
    both calls must complete while the obs write is still pending.

    Deterministic outcome assertion (no wall-clock ceiling, F82): the
    test records whether the obs write was still pending when the calls
    returned. If the wrapper waited on the obs write, the write would run
    to the end of its (bounded) gate wait before the call returned and
    ``obs_write_finished`` would already be set.

    Sabotage proof: change ``_submit_call_log`` to wait on the submitted
    future (``.submit(...).result()``) instead of fire-and-forget — the
    obs write then completes before each dispatch returns and the
    ``pending_when_returned`` assertion fails.
    """
    started: dict[int, tuple[float, int]] = {}
    release_obs = threading.Event()
    obs_write_finished = threading.Event()

    def gated_obs_write(*_a: object, **_k: object) -> None:
        # Simulate a stalled observability backend (lock contention, fsync
        # stall, ...). The bounded wait is a hang guard for the sabotaged
        # (synchronous) shape, which would otherwise deadlock the loop.
        release_obs.wait(timeout=2.0)
        obs_write_finished.set()

    # An obs executor whose submitted task blocks on the gate. It is only
    # released after both dispatches return — fire-and-forget submission
    # must not block the caller.
    gated_obs_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-gated-obs")

    try:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-dispatch") as dispatch:

            def quick_handler(call_id: int = 0) -> dict[str, int]:
                started[call_id] = (time.monotonic(), threading.get_ident())
                return {"id": call_id}

            # Custom obs pool that performs gated_obs_write on every submit.
            class _GatedObs:
                def submit(self, *_a: object, **_k: object) -> object:
                    return gated_obs_pool.submit(gated_obs_write)

            wrapped = async_tool_handler(
                quick_handler,
                deps=AsyncToolHandlerDeps(
                    db_path_fn=lambda: silent_db_path,
                    dispatch_executor_fn=lambda: dispatch,
                    obs_executor_fn=lambda: _GatedObs(),  # type: ignore[arg-type]  # duck-typed test seam — only .submit is exercised
                ),
            )

            async def fire_two() -> list[dict[str, int]]:
                first = await wrapped(call_id=0)
                second = await wrapped(call_id=1)
                return [first, second]

            results = asyncio.run(fire_two())
            pending_when_returned = not obs_write_finished.is_set()
            release_obs.set()

        assert [r["id"] for r in results] == [0, 1], f"both dispatches must return their result; got {results!r}"
        assert sorted(started) == [0, 1], f"both handlers must have run; started={sorted(started)}"
        assert pending_when_returned, (
            "dispatch returned only after the observability write finished — "
            "the obs write is blocking the dispatch path (must be fire-and-forget)"
        )
    finally:
        release_obs.set()
        gated_obs_pool.shutdown(wait=True)


def test_production_default_dispatch_pool_absorbs_six_dogfood_agents() -> None:
    """The production-default dispatch pool absorbs the documented dogfood agent count.

    Six dogfood agents share the production MCP server (per the
    project_dogfood memory). If the default pool ever shrinks below the
    documented load, this test fires before the regression reaches
    production.

    The check is a value assertion against the module-level constant:
    pool-size selection is intentionally a known-at-import-time number,
    not derived from runtime CPU count (the production failure was
    exactly that runtime derivation — cpu_count=2 yielded six workers,
    not enough for six agents + tools-during-agent-turn).

    Sabotage proof: lower ``DEFAULT_DISPATCH_WORKERS`` to 4 — this test
    fails with a clear message naming the documented load.
    """
    minimum_required = 6  # six dogfood agents documented in project_dogfood memory
    assert DEFAULT_DISPATCH_WORKERS >= minimum_required, (
        f"DEFAULT_DISPATCH_WORKERS={DEFAULT_DISPATCH_WORKERS} < {minimum_required} "
        f"dogfood agents documented in project_dogfood memory; production will queue. "
        f"fix: raise DEFAULT_DISPATCH_WORKERS in kairix/agents/mcp/errors.py. "
        f"next: re-run this test."
    )
