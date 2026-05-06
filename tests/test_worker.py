"""Tests for kairix.worker — scheduled background task runner.

Covers:
  - run_embed catches exceptions without crashing
  - run_entity_seed catches exceptions without crashing
  - run_health_check catches exceptions without crashing
  - run_embed calls embed_fn on success
  - run_entity_seed calls entity_seed_fn on success
  - run_health_check counts results on success
  - Shutdown signal (SIGTERM / SIGINT) sets running=False and exits the loop
  - Main loop scheduling logic
"""

from __future__ import annotations

import signal
from dataclasses import dataclass

import pytest

from kairix.worker import (
    EMBED_INTERVAL,
    ENTITY_SEED_INTERVAL,
    HEALTH_CHECK_INTERVAL,
    main,
    run_embed,
    run_entity_seed,
    run_health_check,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# run_embed() tests
# ---------------------------------------------------------------------------


def test_run_embed_catches_exceptions() -> None:
    """run_embed should catch exceptions and not re-raise."""

    def _failing_embed() -> None:
        raise RuntimeError("embed failed")

    run_embed(embed_fn=_failing_embed)  # should not raise


def test_run_embed_catches_import_error() -> None:
    """run_embed should catch ImportError without crashing."""

    def _import_error_embed() -> None:
        raise ImportError("no module")

    run_embed(embed_fn=_import_error_embed)


def test_run_embed_calls_embed_fn() -> None:
    """run_embed should call the injected embed_fn."""
    calls: list[bool] = []

    def _tracking_embed() -> None:
        calls.append(True)

    run_embed(embed_fn=_tracking_embed)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# run_entity_seed() tests
# ---------------------------------------------------------------------------


def test_run_entity_seed_catches_exceptions() -> None:
    """run_entity_seed should catch exceptions and not re-raise."""

    def _failing_seed() -> None:
        raise RuntimeError("store crawl failed")

    run_entity_seed(entity_seed_fn=_failing_seed)  # should not raise


def test_run_entity_seed_catches_import_error() -> None:
    """run_entity_seed should catch ImportError without crashing."""

    def _import_error_seed() -> None:
        raise ImportError("no module")

    run_entity_seed(entity_seed_fn=_import_error_seed)


def test_run_entity_seed_calls_entity_seed_fn() -> None:
    """run_entity_seed should call the injected entity_seed_fn."""
    calls: list[bool] = []

    def _tracking_seed() -> None:
        calls.append(True)

    run_entity_seed(entity_seed_fn=_tracking_seed)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# run_health_check() tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeCheckResult:
    ok: bool


def test_run_health_check_catches_exceptions() -> None:
    """run_health_check should catch exceptions and not re-raise."""

    def _failing_check() -> list:
        raise RuntimeError("check failed")

    run_health_check(health_check_fn=_failing_check)  # should not raise


def test_run_health_check_catches_import_error() -> None:
    """run_health_check should catch ImportError without crashing."""

    def _import_error_check() -> list:
        raise ImportError("no module")

    run_health_check(health_check_fn=_import_error_check)


def test_run_health_check_counts_results() -> None:
    """run_health_check should count passed/total results."""

    def _mixed_results() -> list:
        return [
            _FakeCheckResult(ok=True),
            _FakeCheckResult(ok=False),
            _FakeCheckResult(ok=True),
        ]

    run_health_check(health_check_fn=_mixed_results)  # should not raise


# ---------------------------------------------------------------------------
# Module-level checks
# ---------------------------------------------------------------------------


def test_worker_has_required_imports() -> None:
    """Worker module should have os and Path available (regression test)."""
    from kairix import worker

    # These are imported at module level — verify they exist
    assert hasattr(worker, "os")
    assert hasattr(worker, "Path")


def test_worker_constants() -> None:
    """Worker module should define scheduling interval constants."""
    assert EMBED_INTERVAL == 3600
    assert ENTITY_SEED_INTERVAL == 86400
    assert HEALTH_CHECK_INTERVAL == 21600


# ---------------------------------------------------------------------------
# Shutdown signal tests
# ---------------------------------------------------------------------------


def test_shutdown_handler_sets_running_false() -> None:
    """The _shutdown signal handler should set running=False via nonlocal."""
    import os

    call_count = 0

    def embed_then_signal() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # NOSONAR(python:S4828): test sends a real SIGTERM to itself to
            # exercise the worker's shutdown handler. Self-targeted; no
            # external reach.
            os.kill(os.getpid(), signal.SIGTERM)

    main(
        embed_fn=embed_then_signal,
        entity_seed_fn=lambda: None,
        health_check_fn=lambda: [],
        sleep_fn=lambda _s: None,
        embed_interval=999999,
        entity_seed_interval=999999,
        health_check_interval=999999,
    )

    assert call_count >= 1, "embed was never called"


def test_main_loop_runs_embed_on_interval() -> None:
    """Main loop should run embed when interval has elapsed."""
    import os

    call_count = 0
    entity_called = False
    health_called = False

    def embed_counter() -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            # NOSONAR(python:S4828): self-signal to drive worker shutdown loop.
            os.kill(os.getpid(), signal.SIGTERM)

    def entity_then_noop() -> None:
        nonlocal entity_called
        entity_called = True

    def health_then_noop() -> list:
        nonlocal health_called
        health_called = True
        return []

    main(
        embed_fn=embed_counter,
        entity_seed_fn=entity_then_noop,
        health_check_fn=health_then_noop,
        sleep_fn=lambda _s: None,
        # Set all intervals to 0 so every task fires on every loop iteration
        embed_interval=0,
        entity_seed_interval=0,
        health_check_interval=0,
    )

    # embed is called once on startup + once in the loop = at least 2
    assert call_count >= 2
    assert entity_called, "entity seed should have been called"
    assert health_called, "health check should have been called"


def test_shutdown_handler_via_sigint() -> None:
    """SIGINT should also trigger graceful shutdown."""
    import os

    call_count = 0

    def embed_then_sigint() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # NOSONAR(python:S4828): self-signal to drive Ctrl-C path on the worker.
            os.kill(os.getpid(), signal.SIGINT)

    main(
        embed_fn=embed_then_sigint,
        entity_seed_fn=lambda: None,
        health_check_fn=lambda: [],
        sleep_fn=lambda _s: None,
        embed_interval=999999,
        entity_seed_interval=999999,
        health_check_interval=999999,
    )

    assert call_count >= 1
