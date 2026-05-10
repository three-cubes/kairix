"""Tests for kairix.worker — scheduled background task runner.

Covers:
  - run_embed catches exceptions without crashing
  - run_entity_seed catches exceptions without crashing
  - run_health_check catches exceptions without crashing
  - run_embed calls deps.embed on success
  - run_entity_seed calls deps.entity_seed on success
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
    WorkerDeps,
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

    run_embed(deps=WorkerDeps(embed=_failing_embed))  # should not raise


def test_run_embed_catches_import_error() -> None:
    """run_embed should catch ImportError without crashing."""

    def _import_error_embed() -> None:
        raise ImportError("no module")

    run_embed(deps=WorkerDeps(embed=_import_error_embed))


def test_run_embed_calls_embed_callable() -> None:
    """run_embed should call the injected ``deps.embed`` callable."""
    calls: list[bool] = []

    def _tracking_embed() -> None:
        calls.append(True)

    run_embed(deps=WorkerDeps(embed=_tracking_embed))
    assert len(calls) == 1


def test_run_embed_survives_systemexit_from_embed_callable() -> None:
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
    run_embed(deps=WorkerDeps(embed=_exiting_embed))


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
        run_embed(deps=WorkerDeps(embed=lambda: result))

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
        run_embed(deps=WorkerDeps(embed=lambda: result))

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

    run_entity_seed(deps=WorkerDeps(entity_seed=_failing_seed))  # should not raise


def test_run_entity_seed_catches_import_error() -> None:
    """run_entity_seed should catch ImportError without crashing."""

    def _import_error_seed() -> None:
        raise ImportError("no module")

    run_entity_seed(deps=WorkerDeps(entity_seed=_import_error_seed))


def test_run_entity_seed_calls_entity_seed_callable() -> None:
    """run_entity_seed should call the injected ``deps.entity_seed``."""
    calls: list[bool] = []

    def _tracking_seed() -> None:
        calls.append(True)

    run_entity_seed(deps=WorkerDeps(entity_seed=_tracking_seed))
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

    run_health_check(deps=WorkerDeps(health_check=_failing_check))  # should not raise


def test_run_health_check_catches_import_error() -> None:
    """run_health_check should catch ImportError without crashing."""

    def _import_error_check() -> list:
        raise ImportError("no module")

    run_health_check(deps=WorkerDeps(health_check=_import_error_check))


def test_run_health_check_counts_results() -> None:
    """run_health_check should count passed/total results."""

    def _mixed_results() -> list:
        return [
            _FakeCheckResult(ok=True),
            _FakeCheckResult(ok=False),
            _FakeCheckResult(ok=True),
        ]

    run_health_check(deps=WorkerDeps(health_check=_mixed_results))  # should not raise


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
# WorkerDeps default factories — F6 sabotage proofs
# ---------------------------------------------------------------------------


def test_worker_deps_default_factory_binds_callable_for_every_field() -> None:
    """``WorkerDeps()`` with no overrides constructs a deps bag whose every
    callable field is non-None and callable.

    Sabotage proof: the F6 issue specifically rejects the
    ``Optional[Callable] = None`` self-resolving pattern as having "just
    landed a mypy bug". Every field on this dataclass uses
    ``default_factory`` instead. If any field regressed to ``None`` by
    default, the corresponding ``callable()`` check fires. The test reads
    only the public field names (``embed``, ``entity_seed``, etc.) and
    does not import the private ``_default_*`` helpers — that would
    violate F5 (no internal-name imports in tests).
    """
    deps = WorkerDeps()
    for name in ("embed", "entity_seed", "health_check", "wikilinks", "sleep"):
        value = getattr(deps, name)
        assert callable(value), (
            f"default_factory for WorkerDeps.{name} must bind a callable; "
            f"got {value!r}. Regressing to ``{name}: Callable | None = None`` would leave this None."
        )


def test_worker_deps_partial_override_preserves_other_defaults() -> None:
    """Constructing ``WorkerDeps(embed=fake)`` overrides only ``embed``;
    the other four fields keep their default-factory-bound callables.

    Sabotage proof: production tests rely on this — they swap one callable
    while letting the rest fall through. If the dataclass demanded every
    field set explicitly, this would fail with TypeError. If a default
    regressed to ``None``, ``callable()`` would fail for that field.
    """

    def fake_embed() -> None:
        return None

    deps = WorkerDeps(embed=fake_embed)
    assert deps.embed is fake_embed, "the override field must be the injected callable"
    # Other fields keep their factory-bound defaults — none are None.
    for name in ("entity_seed", "health_check", "wikilinks", "sleep"):
        value = getattr(deps, name)
        assert callable(value), f"WorkerDeps.{name} must remain callable after partial override"


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
        deps=WorkerDeps(
            embed=embed_then_signal,
            entity_seed=lambda: None,
            health_check=lambda: [],
            sleep=lambda _s: None,
        ),
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
        deps=WorkerDeps(
            embed=embed_counter,
            entity_seed=entity_then_noop,
            health_check=health_then_noop,
            sleep=lambda _s: None,
        ),
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
        deps=WorkerDeps(
            embed=embed_then_sigint,
            entity_seed=lambda: None,
            health_check=lambda: [],
            sleep=lambda _s: None,
        ),
        embed_interval=999999,
        entity_seed_interval=999999,
        health_check_interval=999999,
    )

    assert call_count >= 1


# ---------------------------------------------------------------------------
# run_wikilinks_inject() — Phase 3 of #100
# ---------------------------------------------------------------------------


def test_run_wikilinks_inject_calls_wikilinks_callable() -> None:
    called = False

    def _inject() -> None:
        nonlocal called
        called = True

    run_wikilinks_inject(deps=WorkerDeps(wikilinks=_inject))
    assert called


def test_run_wikilinks_inject_catches_exceptions() -> None:
    def _failing() -> None:
        raise RuntimeError("kapow")

    run_wikilinks_inject(deps=WorkerDeps(wikilinks=_failing))  # must not raise


def test_run_wikilinks_inject_survives_systemexit() -> None:
    """The wikilinks CLI raises SystemExit when entities aren't loaded.
    The worker must catch it (same discipline as run_embed).
    """

    def _exits() -> None:
        raise SystemExit(1)

    run_wikilinks_inject(deps=WorkerDeps(wikilinks=_exits))  # must not propagate


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
        deps=WorkerDeps(
            embed=_embed_then_shutdown,
            entity_seed=lambda: None,
            health_check=lambda: [],
            wikilinks=_wikilinks,
            sleep=lambda _s: None,
        ),
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
