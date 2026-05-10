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
    WIKILINKS_INTERVAL,
    main,
    run_embed,
    run_entity_seed,
    run_health_check,
    run_wikilinks_inject,
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


def test_run_embed_survives_systemexit_from_embed_fn() -> None:
    """A ``SystemExit`` from the embed step MUST NOT propagate.

    This pins the v2026.5.10 fix: pre-fix, the worker called the embed
    CLI which used ``sys.exit(1)`` on recall-gate failure. SystemExit
    is not caught by ``except Exception``, so the worker process died
    on every gate alert. The fix catches ``(Exception, SystemExit)``;
    this test fires SystemExit and asserts run_embed returns cleanly.
    """

    def _exiting_embed() -> None:
        raise SystemExit(1)

    # Must not raise — SystemExit is caught and logged, worker continues.
    run_embed(embed_fn=_exiting_embed)


def test_run_embed_logs_recall_alert_from_pipeline_result(caplog: pytest.LogCaptureFixture) -> None:
    """When the embed function returns an EmbedPipelineResult with
    ``recall_passed=False``, the worker logs the alert and continues.
    """
    from kairix.core.embed.use_cases import EmbedPipelineResult

    result = EmbedPipelineResult(
        embedded=10,
        failed=0,
        skipped=0,
        duration_s=1.0,
        cost_usd=0.0,
        db_path="/tmp/test",
        timestamp=0,
        recall_score=0.20,
        recall_passed=False,
        recall_alert="Recall degraded: 20% (was 80%, delta -60%).",
    )

    with caplog.at_level("WARNING"):
        run_embed(embed_fn=lambda: result)

    assert any("recall gate alert" in rec.message for rec in caplog.records), (
        f"expected 'recall gate alert' in logs; got: {[r.message for r in caplog.records]}"
    )


def test_run_embed_logs_failed_chunk_count_from_pipeline_result(caplog: pytest.LogCaptureFixture) -> None:
    """A non-zero ``failed`` count is surfaced as a warning, not silent."""
    from kairix.core.embed.use_cases import EmbedPipelineResult

    result = EmbedPipelineResult(
        embedded=5,
        failed=3,
        skipped=0,
        duration_s=1.0,
        cost_usd=0.0,
        db_path="/tmp/test",
        timestamp=0,
    )

    with caplog.at_level("WARNING"):
        run_embed(embed_fn=lambda: result)

    assert any("3 chunks failed" in rec.message for rec in caplog.records), (
        f"expected '3 chunks failed' in logs; got: {[r.message for r in caplog.records]}"
    )


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
            # NOSONAR: test sends a real SIGTERM to itself to
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
            # NOSONAR: self-signal to drive worker shutdown loop.
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
            # NOSONAR: self-signal to drive Ctrl-C path on the worker.
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


# ---------------------------------------------------------------------------
# run_wikilinks_inject() — Phase 3 of #100
# ---------------------------------------------------------------------------


def test_run_wikilinks_inject_calls_wikilinks_fn() -> None:
    called = False

    def _inject() -> None:
        nonlocal called
        called = True

    run_wikilinks_inject(wikilinks_fn=_inject)
    assert called


def test_run_wikilinks_inject_catches_exceptions() -> None:
    def _failing() -> None:
        raise RuntimeError("kapow")

    run_wikilinks_inject(wikilinks_fn=_failing)  # must not raise


def test_run_wikilinks_inject_survives_systemexit() -> None:
    """The wikilinks CLI raises SystemExit when entities aren't loaded.
    The worker must catch it (same discipline as run_embed).
    """

    def _exits() -> None:
        raise SystemExit(1)

    run_wikilinks_inject(wikilinks_fn=_exits)  # must not propagate


def test_main_loop_calls_wikilinks_at_interval() -> None:
    """When the wikilinks interval has elapsed, main() invokes it once per cycle."""
    call_counts = {"embed": 0, "wikilinks": 0}

    def _embed_then_shutdown() -> None:
        call_counts["embed"] += 1
        if call_counts["embed"] >= 2:
            import os

            # NOSONAR: self-signal so the loop exits after we observe wikilinks running.
            os.kill(os.getpid(), signal.SIGTERM)

    def _wikilinks() -> None:
        call_counts["wikilinks"] += 1

    main(
        embed_fn=_embed_then_shutdown,
        entity_seed_fn=lambda: None,
        health_check_fn=lambda: [],
        wikilinks_fn=_wikilinks,
        sleep_fn=lambda _s: None,
        embed_interval=0,
        entity_seed_interval=999999,
        health_check_interval=999999,
        wikilinks_interval=0,
    )

    assert call_counts["embed"] >= 1
    assert call_counts["wikilinks"] >= 1


def test_wikilinks_interval_constant_matches_embed() -> None:
    """Per #100 the inject runs on the same hourly cadence as embed."""
    assert WIKILINKS_INTERVAL == EMBED_INTERVAL
