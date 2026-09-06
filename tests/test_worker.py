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
import typing
from dataclasses import dataclass
from pathlib import Path

import pytest

from kairix.worker import (
    CONNECTOR_SYNC_INTERVAL,
    EMBED_INTERVAL,
    ENTITY_SEED_INTERVAL,
    HEALTH_CHECK_INTERVAL,
    WIKILINKS_INTERVAL,
    ConnectorSyncResult,
    ConnectorSyncRuntime,
    WorkerDeps,
    main,
    run_connector_sync,
    run_embed,
    run_entity_seed,
    run_health_check,
    run_wikilinks_inject,
)
from kairix.worker_state import WorkerPhase, WorkerState, write_state

pytestmark = pytest.mark.unit


def test_connector_sync_runtime_reuses_replaces_and_rejects_after_close() -> None:
    """The public runtime owns connectors by identity and exact config."""
    constructed: list[tuple[dict[str, str], object]] = []

    def _resolver(kind: str):
        assert kind == "obsidian"

        def _construct(config: dict[str, str]) -> object:
            instance = object()
            constructed.append((config, instance))
            return instance

        return _construct

    runtime = ConnectorSyncRuntime(connector_factory_resolver=_resolver)
    first_entry = {"kind": "obsidian", "name": "vault", "config": {"vault_root": "/vault-a"}}
    changed_entry = {"kind": "obsidian", "name": "vault", "config": {"vault_root": "/vault-b"}}

    first = runtime.connector_for(first_entry)
    same = runtime.connector_for(first_entry)
    changed = runtime.connector_for(changed_entry)

    assert same is first
    assert changed is not first
    assert [config for config, _instance in constructed] == [
        {"vault_root": "/vault-a"},
        {"vault_root": "/vault-b"},
    ]

    runtime.close()
    runtime.close()
    with pytest.raises(RuntimeError, match="connector runtime is closed"):
        runtime.connector_for(changed_entry)


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

    ``except Exception`` does not catch ``SystemExit``, so any embed
    callable that calls ``sys.exit()`` (e.g. the embed CLI on recall-gate
    failure) would tear down the worker process on every gate alert.
    run_embed catches ``(Exception, SystemExit)``; this test fires
    SystemExit and asserts run_embed returns cleanly.
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


def test_run_entity_seed_survives_systemexit_zero() -> None:
    """#270 regression: the store-crawl CLI calls ``sys.exit(0)`` on
    every successful crawl. ``SystemExit`` is NOT a subclass of
    ``Exception``, so a plain ``except Exception`` lets the
    "successful" exit escape — terminating the worker process with
    exit code 0. Docker's ``restart: unless-stopped`` then loops the
    container indefinitely. The fix catches ``(Exception, SystemExit)``.

    Sabotage proof: dropping ``SystemExit`` from the ``except`` tuple
    in ``run_entity_seed`` makes this test raise ``SystemExit`` and
    fail (pytest reports it as an error rather than a pass), proving
    the catch really is the gate.
    """

    def _exiting_seed() -> None:
        raise SystemExit(0)

    # Must not raise — SystemExit(0) is the success-path tear-down
    # from kairix.knowledge.store.cli.crawl. Worker must survive it.
    run_entity_seed(deps=WorkerDeps(entity_seed=_exiting_seed))


def test_run_entity_seed_survives_systemexit_one() -> None:
    """Sister test to the ``SystemExit(0)`` case — non-zero exits
    from the store-crawl CLI (e.g. ``crawl --document-root`` reporting
    errors) must also be non-fatal for the worker process.
    """

    def _exiting_seed() -> None:
        raise SystemExit(1)

    run_entity_seed(deps=WorkerDeps(entity_seed=_exiting_seed))  # must not propagate


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


def test_run_health_check_survives_systemexit() -> None:
    """Mirrors the #270 entity-seed regression: a maintenance helper
    that calls ``sys.exit`` must not bring down the worker. Any
    CheckResult-producing CLI that grew a ``sys.exit`` path would
    otherwise terminate the worker on its first health check.
    """

    def _exiting_check() -> list:
        raise SystemExit(1)

    run_health_check(deps=WorkerDeps(health_check=_exiting_check))  # must not propagate


# ---------------------------------------------------------------------------
# Module-level checks
# ---------------------------------------------------------------------------


def test_worker_has_required_imports() -> None:
    """Worker module should have Path and document_root available
    (regression test).

    ``os`` was removed when the ``KAIRIX_DOCUMENT_ROOT`` env read moved
    into ``kairix.paths.document_root`` (F4). The replacement assertion
    pins that the helper is reachable from the worker namespace so the
    seed-crawl branch keeps working.
    """
    from kairix import worker

    # These are imported at module level — verify they exist
    assert hasattr(worker, "Path")
    assert hasattr(worker, "document_root")


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
    for name in ("embed", "entity_seed", "health_check", "wikilinks", "sleep", "write_state_fn", "read_state_fn"):
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


def test_shutdown_handler_sets_running_false(tmp_path: Path) -> None:
    """The _shutdown signal handler should set running=False via nonlocal."""
    import os

    call_count = 0

    def embed_then_signal() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            os.kill(
                os.getpid(), signal.SIGTERM
            )  # NOSONAR — self-signal SIGTERM to exercise shutdown handler; no external reach.

    main(
        deps=WorkerDeps(
            embed=embed_then_signal,
            entity_seed=lambda: None,
            health_check=lambda: [],
            sleep=lambda _s: None,
            state_path=tmp_path / "worker-state.json",
            write_state_fn=lambda *_: None,
        ),
        embed_interval=999999,
        entity_seed_interval=999999,
        health_check_interval=999999,
    )

    assert call_count >= 1, "embed was never called"


def test_main_loop_runs_embed_on_interval(tmp_path: Path) -> None:
    """Main loop should run embed when interval has elapsed."""
    import os

    call_count = 0
    entity_called = False
    health_called = False

    def embed_counter() -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal to drive worker shutdown loop.

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
            state_path=tmp_path / "worker-state.json",
            write_state_fn=lambda *_: None,
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


def test_main_loop_survives_entity_seed_systemexit_zero(tmp_path: Path) -> None:
    """#270 regression — closed-loop reproduction.

    Scenario reconstructed from the production log: the entity-seed
    callable (``kairix.knowledge.store.cli.main(["crawl", ...])``)
    calls ``sys.exit(0)`` on every successful crawl. Pre-fix, the
    worker caught ``Exception`` but not ``SystemExit`` so the success
    exit propagated, ``main()`` returned, and the container exited 0.
    Docker's restart policy then looped the container.

    This test fires ``SystemExit(0)`` from the injected entity_seed
    callable on the first call, and asserts the embed fake is called
    at least twice (startup + one in-loop iteration). Pre-fix, the
    second embed call never happens because the loop tears down after
    entity_seed runs. Post-fix, the loop keeps running.

    Sabotage proof: revert ``run_entity_seed`` to catch only
    ``Exception`` and rerun — the test fails with an uncaught
    ``SystemExit`` because the loop never reaches its second embed.
    Restore the ``(Exception, SystemExit)`` tuple and the test passes.
    """
    import os

    embed_calls = {"n": 0}
    seed_calls = {"n": 0}

    def _embed_then_shutdown() -> None:
        embed_calls["n"] += 1
        # Two iterations: startup embed (call 1) then one in-loop embed
        # (call 2) which is the post-fix proof that the SystemExit from
        # entity_seed did NOT bring the loop down.
        if embed_calls["n"] >= 2:
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal so the worker loop exits after #270 proof.

    def _entity_seed_exits_zero() -> None:
        seed_calls["n"] += 1
        # Mirror kairix.knowledge.store.cli.main(["crawl", ...]) on the
        # happy path — the CLI calls sys.exit(0) on successful crawl.
        raise SystemExit(0)

    main(
        deps=WorkerDeps(
            embed=_embed_then_shutdown,
            entity_seed=_entity_seed_exits_zero,
            health_check=lambda: [],
            wikilinks=lambda: None,
            sleep=lambda _s: None,
            state_path=tmp_path / "worker-state.json",
            write_state_fn=lambda *_: None,
        ),
        # All intervals zero — entity_seed fires on the first loop pass,
        # the same one where it raised SystemExit pre-fix. Embed then
        # gets a second call on the second pass iff the loop survived.
        embed_interval=0,
        entity_seed_interval=0,
        health_check_interval=999999,
        wikilinks_interval=999999,
    )

    assert seed_calls["n"] >= 1, "entity_seed must have been called at least once"
    assert embed_calls["n"] >= 2, (
        f"main loop must have completed at least 2 iterations after entity_seed raised "
        f"SystemExit(0); got {embed_calls['n']} embed call(s). This is the #270 regression."
    )


def test_shutdown_handler_via_sigint(tmp_path: Path) -> None:
    """SIGINT should also trigger graceful shutdown."""
    import os

    call_count = 0

    def embed_then_sigint() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            os.kill(os.getpid(), signal.SIGINT)  # NOSONAR — self-signal to drive Ctrl-C path on the worker.

    main(
        deps=WorkerDeps(
            embed=embed_then_sigint,
            entity_seed=lambda: None,
            health_check=lambda: [],
            sleep=lambda _s: None,
            state_path=tmp_path / "worker-state.json",
            write_state_fn=lambda *_: None,
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


def test_main_loop_calls_wikilinks_at_interval(tmp_path: Path) -> None:
    """When the wikilinks interval has elapsed, main() invokes it once per cycle."""
    call_counts = {"embed": 0, "wikilinks": 0}

    def _embed_then_shutdown() -> None:
        call_counts["embed"] += 1
        if call_counts["embed"] >= 2:
            import os

            os.kill(
                os.getpid(), signal.SIGTERM
            )  # NOSONAR — self-signal so the loop exits after we observe wikilinks running.

    def _wikilinks() -> None:
        call_counts["wikilinks"] += 1

    main(
        deps=WorkerDeps(
            embed=_embed_then_shutdown,
            entity_seed=lambda: None,
            health_check=lambda: [],
            wikilinks=_wikilinks,
            sleep=lambda _s: None,
            state_path=tmp_path / "worker-state.json",
            write_state_fn=lambda *_: None,
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


# ---------------------------------------------------------------------------
# #224 idle backoff — compute_embed_interval + run_embed return semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def testcompute_embed_interval_no_backoff_below_threshold() -> None:
    """First N consecutive no-ops keep the base interval — no churn on a
    just-quiet vault."""
    from kairix.worker import EMBED_BACKOFF_NOOP_THRESHOLD, compute_embed_interval

    base = 3600
    for streak in range(EMBED_BACKOFF_NOOP_THRESHOLD + 1):
        assert compute_embed_interval(base, streak) == base


@pytest.mark.unit
def testcompute_embed_interval_doubles_after_threshold() -> None:
    """First backoff hop is 2x base; second is 4x; growth is exponential.
    Verifies the exponential math up to the point where the 4-hour cap kicks
    in (a separate test covers the cap)."""
    from kairix.worker import EMBED_BACKOFF_NOOP_THRESHOLD, compute_embed_interval

    # Use a small base so we can demonstrate 2x and 4x growth without
    # hitting the 4-hour cap mid-test.
    base = 60
    assert compute_embed_interval(base, EMBED_BACKOFF_NOOP_THRESHOLD + 1) == base * 2
    assert compute_embed_interval(base, EMBED_BACKOFF_NOOP_THRESHOLD + 2) == base * 4
    assert compute_embed_interval(base, EMBED_BACKOFF_NOOP_THRESHOLD + 3) == base * 8


@pytest.mark.unit
def testcompute_embed_interval_caps_at_max() -> None:
    """Backoff caps at EMBED_BACKOFF_MAX_INTERVAL so a long-idle vault still
    runs embed every 4 hours minimum — keeps recall canaries fresh."""
    from kairix.worker import EMBED_BACKOFF_MAX_INTERVAL, compute_embed_interval

    # A huge streak must clamp to the max, not overflow.
    assert compute_embed_interval(3600, 100) == EMBED_BACKOFF_MAX_INTERVAL


@pytest.mark.unit
def test_run_embed_returns_true_on_real_work(caplog: pytest.LogCaptureFixture) -> None:
    """run_embed signals 'did real work' when the pipeline embedded chunks.
    Used by the main loop to reset the no-op streak."""

    class _Result:
        embedded = 7
        failed = 0
        recall_passed = True
        recall_alert = None
        diagnostics: typing.ClassVar[list[str]] = []
        recall_score = 0.95

    deps = WorkerDeps(embed=lambda: _Result(), entity_seed=lambda: None, health_check=lambda: [])
    assert run_embed(deps) is True


@pytest.mark.unit
def test_run_embed_returns_false_on_noop() -> None:
    """Sabotage-prove: zero embedded AND zero failed means no real work —
    return False so the main loop can advance the backoff streak."""

    class _Result:
        embedded = 0
        failed = 0
        recall_passed = True
        recall_alert = None
        diagnostics: typing.ClassVar[list[str]] = []
        recall_score = 0.95

    deps = WorkerDeps(embed=lambda: _Result(), entity_seed=lambda: None, health_check=lambda: [])
    assert run_embed(deps) is False


@pytest.mark.unit
def test_run_embed_returns_false_on_exception() -> None:
    """An exception in the embed step counts as no-op — broken pipelines
    don't deserve faster retries; backoff applies."""

    def _broken() -> None:
        raise RuntimeError("simulated embed failure")

    deps = WorkerDeps(embed=_broken, entity_seed=lambda: None, health_check=lambda: [])
    assert run_embed(deps) is False


@pytest.mark.unit
def test_run_embed_returns_false_on_legacy_none_result() -> None:
    """Legacy stubs returning None must be treated as no-op (back-compat)."""
    deps = WorkerDeps(embed=lambda: None, entity_seed=lambda: None, health_check=lambda: [])
    assert run_embed(deps) is False


# ---------------------------------------------------------------------------
# #224 phase 5 — observable phase transitions persisted to JSON
# ---------------------------------------------------------------------------


def _shutdown_after_first_embed() -> typing.Callable[[], None]:
    """Build an embed callable that signals shutdown after one call.

    Used by main-loop tests to bound execution to one cycle so they
    don't run the worker forever.
    """
    call_count = {"n": 0}

    def _embed() -> None:
        call_count["n"] += 1
        if call_count["n"] >= 1:
            import os

            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal so the worker loop exits after one cycle.

    return _embed


@pytest.mark.unit
def test_main_loop_writes_state_on_phase_transition(tmp_path: Path) -> None:
    """Every phase change calls ``deps.write_state_fn`` with the current state.

    Sabotage proof: if the main loop forgot to ``write_state_fn`` at any
    transition, the captured phase list would miss STARTING / INGEST /
    IDLE and the assertion would fail. We exercise startup +
    one in-loop cycle and assert all three phases appear in order.
    """
    state_path = tmp_path / "worker-state.json"
    captured_phases: list[WorkerPhase] = []

    def _capture(state: WorkerState, path: Path) -> None:
        captured_phases.append(state.current_phase)
        # Also persist so the next reader sees the file — keeps the
        # boot/resume path exercise-able if needed.
        write_state(state, path)

    deps = WorkerDeps(
        embed=_shutdown_after_first_embed(),
        entity_seed=lambda: None,
        health_check=lambda: [],
        wikilinks=lambda: None,
        sleep=lambda _s: None,
        state_path=state_path,
        write_state_fn=_capture,
    )

    main(
        deps=deps,
        embed_interval=999999,
        entity_seed_interval=999999,
        health_check_interval=999999,
        wikilinks_interval=999999,
    )

    # Boot writes STARTING, then INGEST (before embed), then IDLE (after embed).
    assert WorkerPhase.STARTING in captured_phases, f"missing STARTING in {captured_phases}"
    assert WorkerPhase.INGEST in captured_phases, f"missing INGEST in {captured_phases}"
    assert WorkerPhase.IDLE in captured_phases, f"missing IDLE in {captured_phases}"
    # Ordering: STARTING precedes the first INGEST.
    assert captured_phases.index(WorkerPhase.STARTING) < captured_phases.index(WorkerPhase.INGEST)


@pytest.mark.unit
def test_main_loop_writes_state_on_maintenance_transition(tmp_path: Path) -> None:
    """Entity-seed / health-check / wikilinks paths transition into
    MAINTENANCE and back to IDLE.

    Sabotage proof: if a maintenance task ran without ``_transition``
    bracketing, ``MAINTENANCE`` would never appear in the captured list.
    """
    state_path = tmp_path / "worker-state.json"
    captured_phases: list[WorkerPhase] = []

    call_counter = {"embed": 0}

    def _embed() -> None:
        call_counter["embed"] += 1
        if call_counter["embed"] >= 2:
            import os

            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal to exit the loop after observing maintenance.

    def _capture(state: WorkerState, path: Path) -> None:
        captured_phases.append(state.current_phase)

    deps = WorkerDeps(
        embed=_embed,
        entity_seed=lambda: None,
        health_check=lambda: [],
        wikilinks=lambda: None,
        sleep=lambda _s: None,
        state_path=state_path,
        write_state_fn=_capture,
    )

    main(
        deps=deps,
        embed_interval=0,
        entity_seed_interval=0,
        health_check_interval=0,
        wikilinks_interval=0,
    )

    assert WorkerPhase.MAINTENANCE in captured_phases, f"maintenance phase never written; got {captured_phases}"


@pytest.mark.unit
def test_main_loop_increments_restart_count_on_reboot(tmp_path: Path) -> None:
    """A pre-existing state file's ``restart_count`` is incremented on boot.

    Sabotage proof: if the boot path forgot to call ``read_state_fn`` /
    increment, every state write would carry restart_count=0 and this
    test would fail when asserting it's ≥ 6.
    """
    state_path = tmp_path / "worker-state.json"
    seed = WorkerState(restart_count=5, embedded_total=100)
    write_state(seed, state_path)

    captured_restart_counts: list[int] = []

    def _capture(state: WorkerState, path: Path) -> None:
        captured_restart_counts.append(state.restart_count)
        write_state(state, path)

    deps = WorkerDeps(
        embed=_shutdown_after_first_embed(),
        entity_seed=lambda: None,
        health_check=lambda: [],
        wikilinks=lambda: None,
        sleep=lambda _s: None,
        state_path=state_path,
        write_state_fn=_capture,
    )

    main(
        deps=deps,
        embed_interval=999999,
        entity_seed_interval=999999,
        health_check_interval=999999,
        wikilinks_interval=999999,
    )

    # Every write after boot should carry restart_count=6 (was 5 on disk).
    assert captured_restart_counts, "no state writes captured"
    assert captured_restart_counts[0] == 6, (
        f"first post-boot write should bump restart_count 5→6; got {captured_restart_counts[0]}"
    )


@pytest.mark.unit
def test_main_loop_starts_fresh_when_no_prior_state(tmp_path: Path) -> None:
    """No prior state file → restart_count stays 0; embedded_total starts at 0.

    Pairs with ``test_main_loop_increments_restart_count_on_reboot`` so
    both branches of the boot-read path are covered.
    """
    state_path = tmp_path / "worker-state.json"
    assert not state_path.exists()

    captured: list[WorkerState] = []

    def _capture(state: WorkerState, path: Path) -> None:
        # Snapshot a copy of the mutable state at each write.
        captured.append(
            WorkerState(
                current_phase=state.current_phase,
                restart_count=state.restart_count,
                embedded_total=state.embedded_total,
            )
        )

    deps = WorkerDeps(
        embed=_shutdown_after_first_embed(),
        entity_seed=lambda: None,
        health_check=lambda: [],
        wikilinks=lambda: None,
        sleep=lambda _s: None,
        state_path=state_path,
        write_state_fn=_capture,
    )

    main(
        deps=deps,
        embed_interval=999999,
        entity_seed_interval=999999,
        health_check_interval=999999,
        wikilinks_interval=999999,
    )

    assert captured, "no state writes captured"
    assert captured[0].restart_count == 0, "fresh boot should start at restart_count=0"


@pytest.mark.unit
def test_main_loop_increments_counters_from_embed_outcome(tmp_path: Path) -> None:
    """``embedded`` and ``failed`` counters on the embed result accumulate
    into the persisted ``WorkerState``.

    Sabotage proof: if main() forgot to fold ``outcome.embedded`` into
    ``state.embedded_total`` the asserted final value would still be 0.
    """
    state_path = tmp_path / "worker-state.json"

    class _Result:
        embedded = 4
        failed = 2
        recall_passed = True
        recall_alert = None
        diagnostics: typing.ClassVar[list[str]] = []
        recall_score = 0.9

    embed_call_count = {"n": 0}

    def _embed() -> _Result:
        embed_call_count["n"] += 1
        if embed_call_count["n"] >= 1:
            import os

            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — end the loop after one embed cycle.
        return _Result()

    final_states: list[WorkerState] = []

    def _capture(state: WorkerState, path: Path) -> None:
        final_states.append(
            WorkerState(
                current_phase=state.current_phase,
                embedded_total=state.embedded_total,
                failed_chunks_total=state.failed_chunks_total,
            )
        )

    deps = WorkerDeps(
        embed=_embed,
        entity_seed=lambda: None,
        health_check=lambda: [],
        wikilinks=lambda: None,
        sleep=lambda _s: None,
        state_path=state_path,
        write_state_fn=_capture,
    )

    main(
        deps=deps,
        embed_interval=999999,
        entity_seed_interval=999999,
        health_check_interval=999999,
        wikilinks_interval=999999,
    )

    last = final_states[-1]
    assert last.embedded_total == 4, f"expected 4 embedded, got {last.embedded_total}"
    assert last.failed_chunks_total == 2, f"expected 2 failed, got {last.failed_chunks_total}"


@pytest.mark.unit
def test_main_loop_increments_recall_alerts_when_gate_fails(tmp_path: Path) -> None:
    """``recall_passed=False`` from the embed outcome bumps
    ``recall_alerts_total``.

    Sabotage proof: drop the ``if outcome.recall_passed is False``
    branch in main() and this asserts 0 == 1.
    """
    state_path = tmp_path / "worker-state.json"

    class _Result:
        embedded = 1
        failed = 0
        recall_passed = False
        recall_alert = "degraded"
        diagnostics: typing.ClassVar[list[str]] = []
        recall_score = 0.1

    embed_count = {"n": 0}

    def _embed() -> _Result:
        embed_count["n"] += 1
        if embed_count["n"] >= 1:
            import os

            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — end loop after one embed.
        return _Result()

    captured: list[int] = []

    def _capture(state: WorkerState, path: Path) -> None:
        captured.append(state.recall_alerts_total)

    deps = WorkerDeps(
        embed=_embed,
        entity_seed=lambda: None,
        health_check=lambda: [],
        wikilinks=lambda: None,
        sleep=lambda _s: None,
        state_path=state_path,
        write_state_fn=_capture,
    )

    main(
        deps=deps,
        embed_interval=999999,
        entity_seed_interval=999999,
        health_check_interval=999999,
        wikilinks_interval=999999,
    )

    assert captured[-1] == 1, f"expected one recall alert, got {captured[-1]}"


@pytest.mark.unit
def test_worker_deps_default_factory_binds_state_and_pause_flag_path() -> None:
    """``WorkerDeps()`` with no overrides binds a fresh WorkerState, a Path for
    state_path, a write_state_fn callable, and a Path for pause_flag_path.

    Sabotage proof: if any of these regressed to ``None``, this would fail
    immediately. F6 explicitly rejects the ``Optional[Callable] = None``
    pattern; this test makes that regression visible.
    """
    deps = WorkerDeps()
    assert isinstance(deps.state, WorkerState), "state must be a WorkerState dataclass instance"
    assert isinstance(deps.state_path, Path), "state_path must be a Path"
    assert callable(deps.write_state_fn), "write_state_fn must be callable"
    assert isinstance(deps.pause_flag_path, Path), "pause_flag_path must be a Path"


@pytest.mark.unit
def test_main_loop_enters_paused_phase_when_flag_present(tmp_path: Path) -> None:
    """Pre-touch the pause flag; run main() with a sleep stub that signals
    SIGTERM on its second call. Assert state.current_phase ends up PAUSED.

    Sabotage proof: removing the ``_transition(deps, WorkerPhase.PAUSED)``
    call from worker.py leaves the state in STARTING and this fails.
    """
    import os

    flag = tmp_path / ".worker-paused"
    flag.touch()
    state_path = tmp_path / "worker-state.json"

    sleep_calls: list[float] = []

    def _sleep_then_shutdown(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            os.kill(
                os.getpid(), signal.SIGTERM
            )  # NOSONAR — self-signal SIGTERM to drive shutdown handler; no external reach.

    state = WorkerState()
    deps = WorkerDeps(
        embed=lambda: None,
        entity_seed=lambda: None,
        health_check=lambda: [],
        wikilinks=lambda: None,
        sleep=_sleep_then_shutdown,
        state=state,
        state_path=state_path,
        pause_flag_path=flag,
    )

    main(
        deps=deps,
        embed_interval=999999,
        entity_seed_interval=999999,
        health_check_interval=999999,
        wikilinks_interval=999999,
    )

    assert state.current_phase is WorkerPhase.PAUSED, (
        f"expected PAUSED after pause-flagged loop; got {state.current_phase}"
    )
    # The persisted state file must reflect the same phase.
    persisted = state_path.read_text(encoding="utf-8")
    assert WorkerPhase.PAUSED.value in persisted, "PAUSED phase must be persisted to state file"


@pytest.mark.unit
def test_main_loop_resumes_to_idle_when_flag_removed(tmp_path: Path) -> None:
    """Pre-touch the pause flag, then in the sleep callback remove it after
    one iteration and signal shutdown on the next iteration's task work.

    Sabotage proof: dropping the resume-side ``_transition(deps, IDLE)``
    branch leaves the state in PAUSED forever; this test fails.
    """
    import os

    flag = tmp_path / ".worker-paused"
    flag.touch()
    state_path = tmp_path / "worker-state.json"

    sleep_count = 0

    def _remove_flag_after_one_pause_iter(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            # First pause-poll: remove the flag. Next iter the worker
            # transitions back to IDLE and tries to run tasks.
            flag.unlink(missing_ok=True)

    embed_calls = 0

    def _embed_then_shutdown() -> None:
        nonlocal embed_calls
        embed_calls += 1
        # The startup embed runs before the loop even sees the flag, so
        # we only count post-resume embed calls by requiring the second
        # one (which only happens after the flag is removed).
        if embed_calls >= 2:
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal to drive worker shutdown.

    state = WorkerState()
    deps = WorkerDeps(
        embed=_embed_then_shutdown,
        entity_seed=lambda: None,
        health_check=lambda: [],
        wikilinks=lambda: None,
        sleep=_remove_flag_after_one_pause_iter,
        state=state,
        state_path=state_path,
        pause_flag_path=flag,
    )

    main(
        deps=deps,
        embed_interval=0,  # post-resume embed fires immediately
        entity_seed_interval=999999,
        health_check_interval=999999,
        wikilinks_interval=999999,
    )

    assert state.current_phase is WorkerPhase.IDLE, f"expected IDLE after flag removal; got {state.current_phase}"
    # Confirm we actually went through PAUSED and back — the flag-removal
    # logic only fired on the first sleep, which means the loop saw the
    # flag, entered PAUSED, then resumed.
    assert sleep_count >= 1, "expected at least one paused-loop iteration before resume"


@pytest.mark.unit
def test_main_loop_does_not_run_embed_while_paused(tmp_path: Path) -> None:
    """While the flag is present, embed/seed/health/wikilinks must NOT be
    called inside the main loop. The startup embed (run once before the
    loop) is the only embed call we expect.

    Sabotage proof: removing the ``continue`` from the paused branch lets
    the rest of the loop body run and embed is called extra times.
    """
    import os

    flag = tmp_path / ".worker-paused"
    flag.touch()
    state_path = tmp_path / "worker-state.json"

    sleep_count = 0

    def _sleep_then_shutdown(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 3:
            os.kill(
                os.getpid(), signal.SIGTERM
            )  # NOSONAR — self-signal to terminate the loop after we've observed multiple pause iterations.

    embed_calls = 0
    seed_calls = 0
    health_calls = 0
    wikilinks_calls = 0

    def _embed() -> None:
        nonlocal embed_calls
        embed_calls += 1

    def _seed() -> None:
        nonlocal seed_calls
        seed_calls += 1

    def _health() -> list:
        nonlocal health_calls
        health_calls += 1
        return []

    def _wikilinks() -> None:
        nonlocal wikilinks_calls
        wikilinks_calls += 1

    deps = WorkerDeps(
        embed=_embed,
        entity_seed=_seed,
        health_check=_health,
        wikilinks=_wikilinks,
        sleep=_sleep_then_shutdown,
        state=WorkerState(),
        state_path=state_path,
        pause_flag_path=flag,
    )

    main(
        deps=deps,
        embed_interval=0,
        entity_seed_interval=0,
        health_check_interval=0,
        wikilinks_interval=0,
    )

    # Startup embed runs once before the loop; loop-internal embeds must NOT.
    assert embed_calls == 1, f"expected exactly 1 startup embed while paused; got {embed_calls}"
    assert seed_calls == 0, f"entity seed must not run while paused; got {seed_calls} calls"
    assert health_calls == 0, f"health check must not run while paused; got {health_calls} calls"
    assert wikilinks_calls == 0, f"wikilinks must not run while paused; got {wikilinks_calls} calls"
    assert sleep_count >= 1, "loop must have entered the pause poll at least once"


# ---------------------------------------------------------------------------
# #224 phase 2 — skip-on-noop maintenance gating
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_skips_maintenance_when_noop_streak_above_threshold(tmp_path: Path) -> None:
    """When the no-op streak is at/above MAINTENANCE_SKIP_NOOP_THRESHOLD,
    none of entity_seed / health_check / wikilinks_inject should fire from
    the main loop.

    Sabotage proof: removed the ``maintenance_active and`` guard from the
    three maintenance ``if`` blocks in ``main()`` and the test fails
    because each fake gets called once per loop iteration. Restored the
    guards and the test passes again.
    """
    import os

    from kairix.worker import MAINTENANCE_SKIP_NOOP_THRESHOLD

    state_path = tmp_path / "worker-state.json"

    embed_calls = {"n": 0}
    seed_calls = {"n": 0}
    health_calls = {"n": 0}
    wikilinks_calls = {"n": 0}

    def _embed_noop_then_shutdown() -> None:
        embed_calls["n"] += 1
        # Two iterations: startup embed (call 1) doesn't signal so the
        # while loop body still runs; the in-loop embed (call 2) fires
        # SIGTERM after the maintenance branches have had their chance.
        # If we shut down on call 1 the loop never executes — the
        # gate would look like it worked even if the gate code was deleted.
        if embed_calls["n"] >= 2:
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal so the worker loop exits.
        # Returns None — counts as a no-op, so streak increments rather
        # than resetting.

    def _seed() -> None:
        seed_calls["n"] += 1

    def _health() -> list:
        health_calls["n"] += 1
        return []

    def _wikilinks() -> None:
        wikilinks_calls["n"] += 1

    # Pre-set the streak above the threshold. The startup embed is a
    # no-op so the streak bumps by 1 — still well above the threshold.
    seed_streak = MAINTENANCE_SKIP_NOOP_THRESHOLD + 1
    deps = WorkerDeps(
        embed=_embed_noop_then_shutdown,
        entity_seed=_seed,
        health_check=_health,
        wikilinks=_wikilinks,
        sleep=lambda _s: None,
        state=WorkerState(consecutive_embed_noops=seed_streak),
        state_path=state_path,
        write_state_fn=lambda *_: None,
    )

    main(
        deps=deps,
        embed_interval=0,
        entity_seed_interval=0,
        health_check_interval=0,
        wikilinks_interval=0,
    )

    assert seed_calls["n"] == 0, (
        f"entity_seed must not run when streak ({seed_streak}+) >= threshold ({MAINTENANCE_SKIP_NOOP_THRESHOLD}); "
        f"got {seed_calls['n']} call(s)"
    )
    assert health_calls["n"] == 0, f"health_check must not run above threshold; got {health_calls['n']} call(s)"
    assert wikilinks_calls["n"] == 0, (
        f"wikilinks_inject must not run above threshold; got {wikilinks_calls['n']} call(s)"
    )


@pytest.mark.unit
def test_main_runs_maintenance_when_noop_streak_below_threshold(tmp_path: Path) -> None:
    """When the no-op streak is below the threshold, all three
    maintenance scans fire on their normal schedule.

    Sabotage proof: forced ``maintenance_active = False`` unconditionally
    in main() and the assertion that all three fakes were called fired,
    confirming the maintenance branches really do gate on the flag.
    """
    import os

    state_path = tmp_path / "worker-state.json"

    embed_calls = {"n": 0}
    seed_calls = {"n": 0}
    health_calls = {"n": 0}
    wikilinks_calls = {"n": 0}

    def _embed_then_shutdown() -> None:
        embed_calls["n"] += 1
        # Two iterations: startup embed + one in-loop embed, then exit.
        # That gives the maintenance branches one chance to fire.
        if embed_calls["n"] >= 2:
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal so the worker loop exits.

    def _seed() -> None:
        seed_calls["n"] += 1

    def _health() -> list:
        health_calls["n"] += 1
        return []

    def _wikilinks() -> None:
        wikilinks_calls["n"] += 1

    deps = WorkerDeps(
        embed=_embed_then_shutdown,
        entity_seed=_seed,
        health_check=_health,
        wikilinks=_wikilinks,
        sleep=lambda _s: None,
        state=WorkerState(consecutive_embed_noops=0),
        state_path=state_path,
        write_state_fn=lambda *_: None,
    )

    main(
        deps=deps,
        embed_interval=0,
        entity_seed_interval=0,
        health_check_interval=0,
        wikilinks_interval=0,
    )

    assert seed_calls["n"] >= 1, "entity_seed should run when streak is below threshold"
    assert health_calls["n"] >= 1, "health_check should run when streak is below threshold"
    assert wikilinks_calls["n"] >= 1, "wikilinks_inject should run when streak is below threshold"


@pytest.mark.unit
def test_maintenance_resumes_after_embed_finds_work(tmp_path: Path) -> None:
    """Start above the threshold; the very next embed call returns a
    result with embedded>0 so the streak resets to 0 and maintenance
    resumes on the same iteration.

    Sabotage proof: replaced the ``maintenance_active and`` guard on the
    three maintenance blocks with plain ``True`` and the test still
    passed (because work-finding embed reset the streak). Then forced
    ``maintenance_active = False`` unconditionally and the resume
    assertions failed — confirming the streak-reset path really does
    re-arm the gate. Restored both and the test passes for the right
    reason.
    """
    import os

    from kairix.worker import MAINTENANCE_SKIP_NOOP_THRESHOLD

    state_path = tmp_path / "worker-state.json"

    embed_calls = {"n": 0}
    seed_calls = {"n": 0}
    health_calls = {"n": 0}
    wikilinks_calls = {"n": 0}

    class _DidWorkResult:
        """An embed result that reports real work done — drives
        ``EmbedRunOutcome.did_work=True`` so the loop resets the streak.
        """

        embedded = 3
        failed = 0
        recall_passed = True
        recall_alert = None
        diagnostics: typing.ClassVar[list[str]] = []
        recall_score = 0.95

    def _embed_returns_work_then_shutdown() -> object:
        embed_calls["n"] += 1
        if embed_calls["n"] >= 2:
            os.kill(
                os.getpid(), signal.SIGTERM
            )  # NOSONAR — self-signal so worker loop exits after streak-reset embed runs maintenance.
        # Every call returns a "real work" result. Startup embed bumps
        # the streak from 11 down to 0 (because did_work=True). The
        # second in-loop embed keeps it at 0 — maintenance fires.
        return _DidWorkResult()

    def _seed() -> None:
        seed_calls["n"] += 1

    def _health() -> list:
        health_calls["n"] += 1
        return []

    def _wikilinks() -> None:
        wikilinks_calls["n"] += 1

    seed_streak = MAINTENANCE_SKIP_NOOP_THRESHOLD + 1
    deps = WorkerDeps(
        embed=_embed_returns_work_then_shutdown,
        entity_seed=_seed,
        health_check=_health,
        wikilinks=_wikilinks,
        sleep=lambda _s: None,
        state=WorkerState(consecutive_embed_noops=seed_streak),
        state_path=state_path,
        write_state_fn=lambda *_: None,
    )

    main(
        deps=deps,
        embed_interval=0,
        entity_seed_interval=0,
        health_check_interval=0,
        wikilinks_interval=0,
    )

    # Streak resets on the very first (startup) embed because did_work=True.
    # The in-loop iteration then sees streak=0 < threshold → maintenance fires.
    assert seed_calls["n"] >= 1, (
        f"entity_seed should resume after embed reset the streak; got {seed_calls['n']} call(s)"
    )
    assert health_calls["n"] >= 1, (
        f"health_check should resume after embed reset the streak; got {health_calls['n']} call(s)"
    )
    assert wikilinks_calls["n"] >= 1, (
        f"wikilinks_inject should resume after embed reset the streak; got {wikilinks_calls['n']} call(s)"
    )


# ---------------------------------------------------------------------------
# SC-6 — connector-framework sync seam (Wave 1 wires; Wave 2 implements)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_connector_sync_interval_default_is_15_minutes() -> None:
    """Wave-1 default cadence for the connector sync tick is 900s (15 min).

    Sabotage proof: editing ``CONNECTOR_SYNC_INTERVAL`` in worker.py to
    e.g. 60 would fail this assertion immediately. Locks the documented
    Wave-1 default so an unintentional change surfaces in review.
    """
    assert CONNECTOR_SYNC_INTERVAL == 900


@pytest.mark.unit
def test_worker_deps_default_factory_binds_connector_sync_fn() -> None:
    """``WorkerDeps()`` with no overrides binds a callable
    ``connector_sync_fn`` (F6 default-factory discipline).

    Sabotage proof: regressing the field to
    ``connector_sync_fn: Callable | None = None`` would make this fail
    on the ``callable(...)`` check — the F6 issue specifically rejects
    the ``Optional[Callable] = None`` self-resolving pattern.
    """
    deps = WorkerDeps()
    assert callable(deps.connector_sync_fn), (
        "WorkerDeps.connector_sync_fn must default to a callable; "
        f"got {deps.connector_sync_fn!r}. F6: do not regress to "
        "``Callable | None = None``."
    )


@pytest.mark.unit
def test_connector_sync_result_default_zero_counters() -> None:
    """``ConnectorSyncResult()`` defaults every counter to 0.

    Sabotage proof: changing any field default to a non-zero literal
    would fail this — the Wave-1 placeholder shape is intentionally
    a no-op result so the worker can log it without lying about work
    that hasn't happened.
    """
    result = ConnectorSyncResult()
    assert result.synced == 0
    assert result.failed == 0
    assert result.dead_letter_added == 0


@pytest.mark.unit
def test_run_connector_sync_default_no_config_returns_zero(caplog: pytest.LogCaptureFixture) -> None:
    """PR-6: ``run_connector_sync`` with an injected zero-counter
    ``connector_sync_fn`` logs the structured-completion line.

    The original IM-3 variant relied on the default
    ``_default_connector_sync`` short-circuiting to zero. After PR-6
    the default branches on the ``obsidian_connector_primary`` flag and
    the legacy path reaches the on-disk DocumentScanner. Driving the
    default-factory branches is the job of the PR-6 integration tests
    in ``tests/integration/test_feature_flag_obsidian_connector_primary.py``;
    this unit test stays focused on the worker's logging contract.

    Sabotage proof: drop the ``%d failed=...`` substitution from the
    structured-log line in ``run_connector_sync`` and the
    ``synced=0`` substring check below fails. Restored, the test passes.
    """
    import logging

    def _zero_sync() -> ConnectorSyncResult:
        return ConnectorSyncResult(synced=0, failed=0, dead_letter_added=0)

    with caplog.at_level(logging.INFO, logger="kairix.worker"):
        run_connector_sync(WorkerDeps(connector_sync_fn=_zero_sync))
    completion = [r.getMessage() for r in caplog.records if "connector sync complete" in r.getMessage()]
    assert completion, "expected a 'connector sync complete' log line"
    assert "synced=0" in completion[0]


@pytest.mark.unit
def test_run_connector_sync_calls_injected_callable_and_logs_counters(caplog: pytest.LogCaptureFixture) -> None:
    """Injecting a custom ``connector_sync_fn`` returning a populated
    ``ConnectorSyncResult`` drives the structured-log line.

    Sabotage proof: drop the ``deps.connector_sync_fn()`` call from
    ``run_connector_sync`` and ``call_count`` stays 0 — the assertion
    fails. Restored, the call is observed and the result counters are
    logged.
    """
    import logging

    call_count = {"n": 0}

    def _fake_sync() -> ConnectorSyncResult:
        call_count["n"] += 1
        return ConnectorSyncResult(synced=7, failed=1, dead_letter_added=2)

    with caplog.at_level(logging.INFO, logger="kairix.worker"):
        run_connector_sync(WorkerDeps(connector_sync_fn=_fake_sync))

    assert call_count["n"] == 1, "connector_sync_fn must be invoked exactly once per tick"
    completion_lines = [r.getMessage() for r in caplog.records if "connector sync complete" in r.getMessage()]
    assert completion_lines, "expected a 'connector sync complete' log line"
    msg = completion_lines[0]
    assert "synced=7" in msg
    assert "failed=1" in msg
    assert "dead_letter_added=2" in msg


@pytest.mark.unit
def test_run_connector_sync_catches_unexpected_exception() -> None:
    """A non-NotImplementedError raised by the sync callable must NOT
    crash the worker — same (Exception, SystemExit) discipline as the
    other ``run_*`` helpers.

    Sabotage proof: narrow the except clause to only
    ``NotImplementedError`` and this test surfaces the unhandled
    ``RuntimeError``. Keep the (Exception, SystemExit) tuple and it
    passes.
    """

    def _boom() -> ConnectorSyncResult:
        raise RuntimeError("connector exploded")

    run_connector_sync(WorkerDeps(connector_sync_fn=_boom))  # must not raise


@pytest.mark.unit
def test_main_loop_calls_connector_sync_at_interval(tmp_path: Path) -> None:
    """When ``connector_sync_interval`` has elapsed, ``main()`` drives the
    injected ``connector_sync_fn`` from the dispatch loop.

    Sabotage proof: temporarily removed the
    ``("connector_sync", schedule.connector_sync, last_connector_sync, run_connector_sync)``
    tuple from ``_maybe_run_maintenance_cycle``; the test failed with
    ``call_counts['connector_sync'] == 0``. Restored the tuple and it
    passes. Proves the dispatch slot is actually wired into the loop.
    """
    import os

    call_counts = {"embed": 0, "connector_sync": 0}

    def _embed_then_shutdown() -> None:
        call_counts["embed"] += 1
        if call_counts["embed"] >= 2:
            # Self-signal SIGTERM so the worker loop exits after observing one sync tick.
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal to terminate loop in test.

    def _connector_sync() -> ConnectorSyncResult:
        call_counts["connector_sync"] += 1
        return ConnectorSyncResult(synced=0, failed=0, dead_letter_added=0)

    main(
        deps=WorkerDeps(
            embed=_embed_then_shutdown,
            entity_seed=lambda: None,
            health_check=lambda: [],
            wikilinks=lambda: None,
            connector_sync_fn=_connector_sync,
            sleep=lambda _s: None,
            state_path=tmp_path / "worker-state.json",
            write_state_fn=lambda *_: None,
        ),
        embed_interval=0,
        entity_seed_interval=999999,
        health_check_interval=999999,
        wikilinks_interval=999999,
        connector_sync_interval=0,
    )

    assert call_counts["embed"] >= 1
    assert call_counts["connector_sync"] >= 1, (
        f"expected connector_sync to fire at least once on interval=0; got {call_counts['connector_sync']}"
    )


@pytest.mark.unit
def test_main_loop_runs_connector_sync_above_maintenance_threshold(tmp_path: Path) -> None:
    """connector_sync MUST run regardless of the embed-noop streak (#312).

    A quiet local vault doesn't imply quiet upstream sources;
    SharePoint / Slack / GitHub can produce fresh content while the
    local vault sits idle. The original SC-6 coupling assumed
    coupled idleness and blocked external-source sync for the entire
    duration of any local idle period on the production VM.

    Sabotage proof: restore the ``if not maintenance_active`` early
    return in ``_maybe_run_maintenance_cycle`` and connector_sync_calls
    drops to 0 — this assertion fails.
    """
    import os

    from kairix.worker import MAINTENANCE_SKIP_NOOP_THRESHOLD

    state_path = tmp_path / "worker-state.json"
    embed_calls = {"n": 0}
    connector_sync_calls = {"n": 0}

    def _embed_noop_then_shutdown() -> None:
        embed_calls["n"] += 1
        if embed_calls["n"] >= 2:
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal so the worker loop exits.

    def _connector_sync() -> ConnectorSyncResult:
        connector_sync_calls["n"] += 1
        return ConnectorSyncResult()

    seed_streak = MAINTENANCE_SKIP_NOOP_THRESHOLD + 1
    deps = WorkerDeps(
        embed=_embed_noop_then_shutdown,
        entity_seed=lambda: None,
        health_check=lambda: [],
        wikilinks=lambda: None,
        connector_sync_fn=_connector_sync,
        sleep=lambda _s: None,
        state=WorkerState(consecutive_embed_noops=seed_streak),
        state_path=state_path,
        write_state_fn=lambda *_: None,
    )

    main(
        deps=deps,
        embed_interval=0,
        entity_seed_interval=0,
        health_check_interval=0,
        wikilinks_interval=0,
        connector_sync_interval=0,
    )

    assert connector_sync_calls["n"] >= 1, (
        f"connector_sync MUST still run even when streak ({seed_streak}+) >= threshold "
        f"({MAINTENANCE_SKIP_NOOP_THRESHOLD}) — external sources can produce fresh "
        f"content while the local vault is idle; got {connector_sync_calls['n']} call(s)"
    )


@pytest.mark.unit
def test_main_loop_skips_local_maintenance_above_noop_threshold(tmp_path: Path) -> None:
    """Locks the OTHER half of #312: entity_seed / health_check /
    wikilinks_inject ARE still gated by the embed-noop threshold.
    Connector_sync is the only task that crosses the gate.

    Sabotage proof: drop the ``maintenance_active`` guard around the
    local_tasks loop in _maybe_run_maintenance_cycle and these
    counters jump above 0 with the high streak.
    """
    import os

    from kairix.worker import MAINTENANCE_SKIP_NOOP_THRESHOLD

    state_path = tmp_path / "worker-state.json"
    embed_calls = {"n": 0}
    entity_calls = {"n": 0}
    health_calls = {"n": 0}
    wikilinks_calls = {"n": 0}

    def _embed_noop_then_shutdown() -> None:
        embed_calls["n"] += 1
        if embed_calls["n"] >= 2:
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal to exit worker loop.

    def _entity_seed() -> None:
        entity_calls["n"] += 1

    def _health() -> list[str]:
        health_calls["n"] += 1
        return []

    def _wikilinks() -> None:
        wikilinks_calls["n"] += 1

    seed_streak = MAINTENANCE_SKIP_NOOP_THRESHOLD + 1
    deps = WorkerDeps(
        embed=_embed_noop_then_shutdown,
        entity_seed=_entity_seed,
        health_check=_health,
        wikilinks=_wikilinks,
        connector_sync_fn=lambda: ConnectorSyncResult(),
        sleep=lambda _s: None,
        state=WorkerState(consecutive_embed_noops=seed_streak),
        state_path=state_path,
        write_state_fn=lambda *_: None,
    )

    main(
        deps=deps,
        embed_interval=0,
        entity_seed_interval=0,
        health_check_interval=0,
        wikilinks_interval=0,
        connector_sync_interval=0,
    )

    assert entity_calls["n"] == 0, "entity_seed must skip above the noop threshold"
    assert health_calls["n"] == 0, "health_check must skip above the noop threshold"
    assert wikilinks_calls["n"] == 0, "wikilinks must skip above the noop threshold"


@pytest.mark.unit
def test_main_loop_does_not_run_connector_sync_while_paused(tmp_path: Path) -> None:
    """While the operator pause flag is present, connector_sync MUST NOT
    fire from the dispatch loop — it inherits the same pause-gating as
    the other maintenance tasks.

    Sabotage proof: remove the ``continue`` from the paused branch in
    ``main`` and connector_sync_calls jumps above 0. With the continue
    in place, only the (pre-loop) startup embed runs and connector_sync
    stays at 0.
    """
    import os

    flag = tmp_path / ".worker-paused"
    flag.touch()
    state_path = tmp_path / "worker-state.json"

    sleep_count = 0

    def _sleep_then_shutdown(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 3:
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal so the loop exits after pause iterations.

    connector_sync_calls = {"n": 0}

    def _connector_sync() -> ConnectorSyncResult:
        connector_sync_calls["n"] += 1
        return ConnectorSyncResult()

    deps = WorkerDeps(
        embed=lambda: None,
        entity_seed=lambda: None,
        health_check=lambda: [],
        wikilinks=lambda: None,
        connector_sync_fn=_connector_sync,
        sleep=_sleep_then_shutdown,
        state=WorkerState(),
        state_path=state_path,
        pause_flag_path=flag,
    )

    main(
        deps=deps,
        embed_interval=0,
        entity_seed_interval=0,
        health_check_interval=0,
        wikilinks_interval=0,
        connector_sync_interval=0,
    )

    assert connector_sync_calls["n"] == 0, (
        f"connector_sync must not run while paused; got {connector_sync_calls['n']} call(s)"
    )
    assert sleep_count >= 1, "loop must have entered the pause poll at least once"


@pytest.mark.unit
def test_main_loop_default_connector_sync_does_not_crash_worker(tmp_path: Path) -> None:
    """End-to-end guarantee: the worker loop survives an unexpected
    raise from ``connector_sync_fn`` — embed still runs to completion
    in a subsequent iteration.

    PR-6: the default ``_default_connector_sync`` no longer raises
    ``NotImplementedError`` (it branches on the
    ``obsidian_connector_primary`` flag). To preserve the original
    test's intent — "an exceptional connector sync does not bring
    down the worker" — we now inject a ``connector_sync_fn`` that
    raises and assert the embed loop still progresses.

    Sabotage proof: remove the ``except (Exception, SystemExit)``
    catch from ``run_connector_sync`` and the worker crashes on the
    first maintenance tick — the second embed call never happens.
    """
    import os

    embed_calls = {"n": 0}

    def _embed_then_shutdown() -> None:
        embed_calls["n"] += 1
        if embed_calls["n"] >= 2:
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal so the loop exits after the second embed.

    def _raising_connector_sync() -> ConnectorSyncResult:
        raise RuntimeError("simulated connector_sync failure")

    deps = WorkerDeps(
        embed=_embed_then_shutdown,
        entity_seed=lambda: None,
        health_check=lambda: [],
        wikilinks=lambda: None,
        connector_sync_fn=_raising_connector_sync,
        sleep=lambda _s: None,
        state=WorkerState(),
        state_path=tmp_path / "worker-state.json",
        write_state_fn=lambda *_: None,
    )

    main(
        deps=deps,
        embed_interval=0,
        entity_seed_interval=999999,
        health_check_interval=999999,
        wikilinks_interval=999999,
        connector_sync_interval=0,
    )

    assert embed_calls["n"] >= 2, (
        f"main loop must survive an exceptional connector_sync_fn; got {embed_calls['n']} embed call(s)."
    )


# ---------------------------------------------------------------------------
# run_wal_checkpoint() tests (R3, #389)
# ---------------------------------------------------------------------------


def test_run_wal_checkpoint_invokes_fn_and_logs_result() -> None:
    """run_wal_checkpoint delegates to deps.wal_checkpoint_fn."""
    from kairix.worker import run_wal_checkpoint

    calls: list[bool] = []

    def _fake_checkpoint() -> dict[str, int]:
        calls.append(True)
        return {"busy": 0, "log_pages": 0, "checkpointed": 142}

    run_wal_checkpoint(deps=WorkerDeps(wal_checkpoint_fn=_fake_checkpoint))
    assert calls == [True], "wal_checkpoint_fn must be invoked once"


def test_run_wal_checkpoint_catches_exceptions() -> None:
    """A raised checkpoint must not crash the worker loop."""
    import sqlite3

    from kairix.worker import run_wal_checkpoint

    def _raising_checkpoint() -> dict[str, int]:
        raise sqlite3.OperationalError("database is locked")

    # Must not raise — the (Exception, SystemExit) catch keeps the worker alive.
    run_wal_checkpoint(deps=WorkerDeps(wal_checkpoint_fn=_raising_checkpoint))


def test_run_wal_checkpoint_handles_non_dict_return() -> None:
    """A misbehaving fn returning a non-dict value must not crash the log call."""
    from kairix.worker import run_wal_checkpoint

    def _returns_none() -> None:
        return None

    run_wal_checkpoint(deps=WorkerDeps(wal_checkpoint_fn=_returns_none))  # type: ignore[arg-type] — intentional shape mismatch: pins the runtime-tolerance for misbehaving checkpoint impls.


# ---------------------------------------------------------------------------
# run_deadletter_sweep() tests (PR-5)
# ---------------------------------------------------------------------------


def test_run_deadletter_sweep_invokes_fn_and_logs() -> None:
    """run_deadletter_sweep delegates to deps.deadletter_sweep_fn."""
    from kairix.core.connectors.deadletter_drain import DrainSummary
    from kairix.worker import run_deadletter_sweep

    calls: list[bool] = []

    def _fake_sweep() -> tuple[DrainSummary, ...]:
        calls.append(True)
        return (DrainSummary("removed-sharepoint", drained=2, corrupt_zip=1, unsupported_mime=1, left=0),)

    run_deadletter_sweep(deps=WorkerDeps(deadletter_sweep_fn=_fake_sweep))
    assert calls == [True], "deadletter_sweep_fn must be invoked once"


def test_run_deadletter_sweep_catches_exceptions() -> None:
    """A raised sweep must not crash the worker loop."""
    import sqlite3

    from kairix.worker import run_deadletter_sweep

    def _raising_sweep() -> tuple:
        raise sqlite3.OperationalError("database is locked")

    # Must not raise — the (Exception, SystemExit) catch keeps the worker alive.
    run_deadletter_sweep(deps=WorkerDeps(deadletter_sweep_fn=_raising_sweep))


def test_run_deadletter_sweep_handles_none_return() -> None:
    """A misbehaving fn returning None must not crash the aggregate log."""
    from kairix.worker import run_deadletter_sweep

    def _returns_none() -> None:
        return None

    # A () -> None callable is assignable to the Callable[[], Any] slot; this
    # pins the run_deadletter_sweep aggregate-log tolerance for an empty/None return.
    run_deadletter_sweep(deps=WorkerDeps(deadletter_sweep_fn=_returns_none))


@pytest.mark.unit
def test_main_loop_calls_deadletter_sweep_at_interval(tmp_path: Path) -> None:
    """When ``deadletter_sweep_interval`` has elapsed, ``main()`` drives the
    injected ``deadletter_sweep_fn`` from the dispatch loop — the periodic
    orphaned-source sweep is actually wired into the worker tick.

    Sabotage proof: remove the ``deadletter_sweep`` dispatch block in
    ``_maybe_run_maintenance_cycle`` → ``sweep_calls['n'] == 0`` and this
    assertion fails. Restored, it passes — proving the slot is live.
    """
    import os

    from kairix.core.connectors.deadletter_drain import DrainSummary

    embed_calls = {"n": 0}
    sweep_calls = {"n": 0}

    def _embed_then_shutdown() -> None:
        embed_calls["n"] += 1
        if embed_calls["n"] >= 2:
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal to terminate loop in test.

    def _sweep() -> tuple[DrainSummary, ...]:
        sweep_calls["n"] += 1
        return ()

    main(
        deps=WorkerDeps(
            embed=_embed_then_shutdown,
            entity_seed=lambda: None,
            health_check=lambda: [],
            wikilinks=lambda: None,
            connector_sync_fn=lambda: ConnectorSyncResult(),
            deadletter_sweep_fn=_sweep,
            sleep=lambda _s: None,
            state_path=tmp_path / "worker-state.json",
            write_state_fn=lambda *_: None,
        ),
        embed_interval=0,
        entity_seed_interval=999999,
        health_check_interval=999999,
        wikilinks_interval=999999,
        connector_sync_interval=999999,
        deadletter_sweep_interval=0,
    )

    assert sweep_calls["n"] >= 1, (
        f"expected deadletter_sweep to fire at least once on interval=0; got {sweep_calls['n']}"
    )


@pytest.mark.unit
def test_main_loop_runs_deadletter_sweep_above_maintenance_threshold(tmp_path: Path) -> None:
    """The sweep MUST run regardless of the embed-noop streak.

    A quiet local vault does not imply a drained orphaned-source
    dead-letter backlog — the sweep is in the always-run bucket alongside
    connector_sync / neo4j_drain.

    Sabotage proof: move the sweep dispatch inside the
    ``if maintenance_active:`` block → with the high streak ``sweep_calls``
    drops to 0 and this fails.
    """
    import os

    from kairix.core.connectors.deadletter_drain import DrainSummary
    from kairix.worker import MAINTENANCE_SKIP_NOOP_THRESHOLD

    embed_calls = {"n": 0}
    sweep_calls = {"n": 0}

    def _embed_noop_then_shutdown() -> None:
        embed_calls["n"] += 1
        if embed_calls["n"] >= 2:
            os.kill(os.getpid(), signal.SIGTERM)  # NOSONAR — self-signal so the loop exits.

    def _sweep() -> tuple[DrainSummary, ...]:
        sweep_calls["n"] += 1
        return ()

    seed_streak = MAINTENANCE_SKIP_NOOP_THRESHOLD + 1
    main(
        deps=WorkerDeps(
            embed=_embed_noop_then_shutdown,
            entity_seed=lambda: None,
            health_check=lambda: [],
            wikilinks=lambda: None,
            connector_sync_fn=lambda: ConnectorSyncResult(),
            deadletter_sweep_fn=_sweep,
            sleep=lambda _s: None,
            state=WorkerState(consecutive_embed_noops=seed_streak),
            state_path=tmp_path / "worker-state.json",
            write_state_fn=lambda *_: None,
        ),
        embed_interval=0,
        entity_seed_interval=0,
        health_check_interval=0,
        wikilinks_interval=0,
        connector_sync_interval=999999,
        deadletter_sweep_interval=0,
    )

    assert sweep_calls["n"] >= 1, (
        f"deadletter sweep MUST still run even when streak ({seed_streak}+) >= threshold "
        f"({MAINTENANCE_SKIP_NOOP_THRESHOLD}); got {sweep_calls['n']} call(s)"
    )


# ---------------------------------------------------------------------------
# connector_enabled() — same-module behaviour-pin (mutation parity).
#
# The full predicate matrix lives in
# tests/unit/test_worker_connector_sync.py. This same-module home guarantees
# the flagless-branch outcome stays in the mutation-parity impacted window
# (mutation_parity prioritises test_<mod>.py), so the
# ``... else True`` -> ``... else False`` mutant on the flagless arm is
# reliably KILLED even when worker.py's wide importer set is truncated by the
# impacted-test cap.
# ---------------------------------------------------------------------------


def test_connector_enabled_flagless_kind_is_true_even_when_flag_off() -> None:
    """A flagless connector kind (no ``connector_<kind>`` in REGISTRY) always
    runs — the predicate returns True without consulting ``read_flag``.

    Sabotage: change ``... else True`` to ``... else False`` on the flagless
    arm of connector_enabled → this test fails because a flagless kind would
    report disabled.
    """
    from kairix.worker import connector_enabled

    # ``obsidian`` is flagless — no ``connector_obsidian`` REGISTRY entry — so
    # the OFF read_flag is never consulted and the kind stays enabled.
    assert connector_enabled("obsidian", read_flag=lambda _name: False) is True


def test_connector_enabled_registered_kind_follows_flag() -> None:
    """A registered connector kind (``sharepoint`` → ``connector_sharepoint``
    is in REGISTRY) is enabled iff its flag resolves True.

    Sabotage: drop the ``name in registry`` guard (always read_flag) or
    invert it → one of these two assertions fails.
    """
    from kairix.worker import connector_enabled

    assert connector_enabled("sharepoint", read_flag=lambda _name: False) is False
    assert connector_enabled("sharepoint", read_flag=lambda _name: True) is True
