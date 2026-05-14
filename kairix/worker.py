"""Background worker for scheduled tasks.

Runs inside the kairix-worker Docker container. Handles:
- Incremental document indexing (every hour)
- Entity relationship seeding (once a day at 3am)
- Health check logging (every 6 hours)

Usage:
    python -m kairix.worker
    # Or via Docker: docker compose exec kairix-worker worker
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairix.paths import (
    document_root,
    maintenance_skip_noop_threshold,
    worker_pause_flag_path,
    worker_state_path,
)
from kairix.worker_state import WorkerPhase, WorkerState, read_state, write_state

if TYPE_CHECKING:
    from kairix.core.embed.use_cases import EmbedPipelineResult

logger = logging.getLogger(__name__)

# #224 phase 4 — pause-flag polling cadence.
# When the worker is in PAUSED phase, it sleeps this long between flag
# re-checks. Short enough that operators see resumption quickly (CLI tells
# them "may take up to 5s"), long enough not to thrash on a touch-file
# stat call. Exposed as a module constant so tests can run a couple of
# iterations through the pause-check without injecting the value.
PAUSE_POLL_INTERVAL_S = 5

# Task schedule (seconds between runs)
EMBED_INTERVAL = 3600  # 1 hour
ENTITY_SEED_INTERVAL = 86400  # 24 hours
HEALTH_CHECK_INTERVAL = 21600  # 6 hours
WIKILINKS_INTERVAL = 3600  # 1 hour — runs after embed; --changed mtime-filters

# Idle backoff (#224): when embed runs find no work to do, the next-embed
# wait extends exponentially. Cap at 4 hours so we don't go totally silent
# on a long-idle vault but also don't churn CPU/IO every hour for nothing.
EMBED_BACKOFF_NOOP_THRESHOLD = 2  # after N consecutive no-ops, start backing off
EMBED_BACKOFF_MAX_INTERVAL = 14400  # 4 hours — cap on backed-off embed interval

# #224 phase 2 — maintenance-skip threshold.
# When the embed no-op streak hits this count, the three maintenance scans
# (entity_seed, health_check, wikilinks_inject) become pointless work and
# the worker skips them too until embed next finds work. Resolved at module
# import time from KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD via paths.py
# (F4 — env reads stay centralised). Threshold tuned to default 10 so the
# embed-backoff exponential has time to slow polling down before we silence
# maintenance, but operators can lower it on tiny shared hosts.
MAINTENANCE_SKIP_NOOP_THRESHOLD = maintenance_skip_noop_threshold()


def _default_embed() -> EmbedPipelineResult:
    """Default embed implementation — runs the embed use case directly.

    Returns the structured ``EmbedPipelineResult`` so the worker can log
    structured outcomes (embed counts, recall score, alerts) without
    depending on CLI exit-code semantics. Critically, this DOES NOT call
    the CLI ``main()`` — that path raises ``SystemExit`` on recall-gate
    failures and would terminate the worker process. The use case raises
    only on truly unrecoverable conditions.
    """
    from kairix.core.embed.use_cases import run_incremental_embed_pipeline

    return run_incremental_embed_pipeline()


def _default_entity_seed() -> None:
    """Default entity seed implementation — lazy-imports and runs store crawl."""
    from kairix.knowledge.store.cli import main as store_main

    store_main(
        [
            "crawl",
            "--document-root",
            str(document_root()),
        ]
    )


def _default_wikilinks_inject() -> None:
    """Default wikilinks inject — runs ``kairix wikilinks inject --changed``.

    The CLI's ``main`` may raise ``SystemExit`` (e.g. when no entities
    are loaded yet, before the entity seed has run). The worker's
    ``run_wikilinks_inject`` catches that to keep the worker alive.
    """
    from kairix.knowledge.wikilinks.cli import main as wikilinks_main

    wikilinks_main(["inject", "--changed"])


def _default_health_check() -> list[Any]:
    """Default health check — lazy-imports and runs all deployment checks."""
    from kairix.platform.onboard.check import run_all_checks

    return run_all_checks()


@dataclass
class WorkerDeps:
    """Injectable dependencies for the worker loop and its task helpers.

    Replaces the F6-violating ``embed_fn=None`` / ``entity_seed_fn=None`` /
    ``health_check_fn=None`` / ``wikilinks_fn=None`` / ``sleep_fn=None``
    test-only kwargs with a typed dataclass. Production code calls
    ``main()`` without ``deps`` and the default factory wires the real
    task callables. Tests construct
    ``WorkerDeps(embed=fake, sleep=lambda _s: None)`` and pass it through.

    Each callable field is non-Optional with a ``default_factory`` (per
    CLAUDE.md F6 guidance: avoid the ``Optional[Callable] + post-init``
    pattern that "just landed a mypy bug") so mypy sees the production
    callable directly — no ``assert deps.x is not None`` ladder is
    needed inside the worker loop.
    """

    embed: Callable[[], Any] = field(default_factory=lambda: _default_embed)
    entity_seed: Callable[[], None] = field(default_factory=lambda: _default_entity_seed)
    health_check: Callable[[], list[Any]] = field(default_factory=lambda: _default_health_check)
    wikilinks: Callable[[], None] = field(default_factory=lambda: _default_wikilinks_inject)
    sleep: Callable[[float], None] = field(default_factory=lambda: time.sleep)
    # #224 phase 4-5 combined — observable state + pause flag.
    # ``state`` is the in-memory dataclass the loop mutates on phase changes.
    # ``state`` defaults to None so the boot path in main() can read prior
    # state off disk first (restart_count survives container restarts).
    # ``state_path`` is where it gets persisted via ``write_state_fn`` so
    # operators (and ``kairix worker status``) can read it.
    # ``read_state_fn`` is the read-side test seam mirroring ``write_state_fn``.
    # ``pause_flag_path`` is the touch-file the operator-facing
    # ``kairix worker pause/resume`` toggles; the loop polls it each
    # iteration. All are F6-clean (typed, default_factory).
    state: WorkerState = field(default_factory=WorkerState)
    state_path: Path = field(default_factory=worker_state_path)
    write_state_fn: Callable[[WorkerState, Path], None] = field(default_factory=lambda: write_state)
    read_state_fn: Callable[[Path], WorkerState | None] = field(default_factory=lambda: read_state)
    pause_flag_path: Path = field(default_factory=worker_pause_flag_path)


@dataclass(frozen=True)
class EmbedRunOutcome:
    """Structured outcome of one embed pass — used by ``main()`` to update
    the persisted ``WorkerState`` counters.

    Field semantics mirror ``EmbedPipelineResult`` but with safe-default
    integers so a legacy stub returning a sparse object still feeds the
    state counters cleanly.
    """

    did_work: bool
    embedded: int = 0
    failed: int = 0
    recall_passed: bool | None = None


def _log_embed_complete(embedded: Any, failed: Any, recall_score: Any) -> None:
    """Emit the standard 'embed complete' info line with recall as percentage or n/a."""
    recall_str = f"{recall_score:.0%}" if isinstance(recall_score, float) else "n/a"
    logger.info("worker: embed complete — embedded=%s failed=%s recall=%s", embedded, failed, recall_str)


def _log_embed_warnings(failed: Any, recall_passed: Any, recall_alert: Any, diagnostics: list[Any]) -> None:
    """Emit failure / recall-gate / diagnostic warnings from an embed result."""
    if isinstance(failed, int) and failed > 0:
        logger.warning("worker: %d chunks failed during embed", failed)
    if recall_passed is False:
        logger.warning(
            "worker: recall gate alert — %s",
            recall_alert or "search quality degraded; see kairix onboard check",
        )
    for diag in diagnostics:
        logger.warning("worker: %s", diag)


def _outcome_from_result(result: Any) -> EmbedRunOutcome:
    """Map an ``EmbedPipelineResult``-shaped object into a typed ``EmbedRunOutcome``."""
    embedded = getattr(result, "embedded", None)
    failed = getattr(result, "failed", None)
    recall_passed = getattr(result, "recall_passed", None)
    diagnostics = getattr(result, "diagnostics", None) or []
    _log_embed_complete(embedded, failed, getattr(result, "recall_score", None))
    _log_embed_warnings(failed, recall_passed, getattr(result, "recall_alert", None), diagnostics)
    did_work = (isinstance(embedded, int) and embedded > 0) or (isinstance(failed, int) and failed > 0)
    return EmbedRunOutcome(
        did_work=did_work,
        embedded=embedded if isinstance(embedded, int) else 0,
        failed=failed if isinstance(failed, int) else 0,
        recall_passed=recall_passed if isinstance(recall_passed, bool) else None,
    )


def run_embed_with_outcome(deps: WorkerDeps | None = None) -> EmbedRunOutcome:
    """Run incremental embed and return a structured outcome.

    Same try/except/logging discipline as ``run_embed`` (see its
    docstring for the "never crash the worker" rationale); this variant
    additionally surfaces the counters main() folds into ``WorkerState``.
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        logger.info("worker: starting incremental embed")
        result = deps.embed()
        if result is None:
            logger.info("worker: embed complete")
            return EmbedRunOutcome(did_work=False)
        return _outcome_from_result(result)
    except (Exception, SystemExit) as exc:
        logger.warning("worker: embed pipeline raised — %s", exc)
        return EmbedRunOutcome(did_work=False)


def run_embed(deps: WorkerDeps | None = None) -> bool:
    """Run incremental embed — indexes new and changed documents.

    Returns ``True`` when the embed run did real work (embedded > 0 or
    failed > 0), ``False`` when it was a no-op. The main loop uses this
    signal to apply idle-backoff per #224.

    The worker treats every outcome of the embed pipeline as
    non-fatal: failed chunks, recall-gate alerts, and unexpected
    exceptions are all logged and the worker continues to the next
    interval. This decoupling is deliberate — the worker's job is to
    KEEP RUNNING on a schedule; the embed use case's job is to do the
    work and report what happened.

    The pre-v2026.5.10 worker called the embed CLI which used
    ``sys.exit()`` to signal recall-gate failures. ``SystemExit`` is
    not caught by ``except Exception``, so any gate alert killed the
    worker process. That coupling is removed here: the use case
    returns a ``EmbedPipelineResult`` dataclass; we inspect it and log.

    ``deps.embed`` is the injection seam: tests pass a callable returning
    either the result dataclass or None (legacy). Production passes
    ``_default_embed`` which runs the use case.
    """
    return run_embed_with_outcome(deps).did_work


def compute_embed_interval(base: int, noop_streak: int) -> int:
    """Apply exponential idle-backoff after a streak of no-op embed runs.

    No backoff until ``EMBED_BACKOFF_NOOP_THRESHOLD`` consecutive no-ops.
    After that, each additional no-op doubles the interval, capped at
    ``EMBED_BACKOFF_MAX_INTERVAL`` (4 hours). The exponent is
    ``noop_streak - threshold + 1`` so the FIRST backoff hop is 2x, not 1x.

    Implements #224's "Add backoff/jitter when scans find no new or
    changed work" acceptance criterion.
    """
    if noop_streak <= EMBED_BACKOFF_NOOP_THRESHOLD:
        return base
    exponent = noop_streak - EMBED_BACKOFF_NOOP_THRESHOLD
    return int(min(base * (2**exponent), EMBED_BACKOFF_MAX_INTERVAL))


def run_entity_seed(deps: WorkerDeps | None = None) -> None:
    """Run entity relationship seeding from document store structure.

    Args:
        deps: Injectable worker dependencies. Tests construct
              ``WorkerDeps(entity_seed=fake)``; production omits the
              kwarg and the default factory wires the real store crawl
              CLI entry point.
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        logger.info("worker: starting entity seed")
        deps.entity_seed()
        logger.info("worker: entity seed complete")
    except Exception as exc:
        logger.warning("worker: entity seed failed — %s", exc)


def run_wikilinks_inject(deps: WorkerDeps | None = None) -> None:
    """Inject ``[[wikilinks]]`` on first mention into agent-written documents.

    Closes #100 — the host cron's nightly ``kairix wikilinks inject
    --changed`` was lost in the Docker migration. The worker now runs
    it on the same cadence as embed (hourly) so new agent-written notes
    get linked to known entities.

    Treats every outcome as non-fatal: the wikilinks CLI may
    ``sys.exit(1)`` when entities aren't loaded yet (pre-first-seed
    bootstrapping), and that must NOT terminate the worker. Same
    ``(Exception, SystemExit)`` discipline as ``run_embed``.

    ``deps.wikilinks`` is the injection seam tests use; production
    falls through to ``_default_wikilinks_inject``.
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        logger.info("worker: starting wikilinks inject")
        deps.wikilinks()
        logger.info("worker: wikilinks inject complete")
    except (Exception, SystemExit) as exc:
        logger.warning("worker: wikilinks inject raised — %s", exc)


def run_health_check(deps: WorkerDeps | None = None) -> None:
    """Log a health check.

    Args:
        deps: Injectable worker dependencies. Tests construct
              ``WorkerDeps(health_check=fake)``; production omits the
              kwarg and the default factory wires ``run_all_checks``.
    """
    deps = deps if deps is not None else WorkerDeps()
    try:
        results = deps.health_check()
        passed = sum(1 for r in results if r.ok)
        total = len(results)
        logger.info("worker: health check %d/%d passed", passed, total)
    except Exception as exc:
        logger.warning("worker: health check failed — %s", exc)


@dataclass
class _Schedule:
    """Worker task interval config — bundles the four `int` cadences.

    Scalar config (not a test-injection seam); main() builds this once
    from kwargs + module defaults so the inner loop helpers can pass a
    single value around rather than four discrete `_embed_interval` ints.
    """

    embed: int
    entity: int
    health: int
    wikilinks: int


def _resolve_schedule(
    embed_interval: int | None,
    entity_seed_interval: int | None,
    health_check_interval: int | None,
    wikilinks_interval: int | None,
) -> _Schedule:
    """Fold kwargs + module defaults into a single ``_Schedule``."""
    return _Schedule(
        embed=embed_interval if embed_interval is not None else EMBED_INTERVAL,
        entity=entity_seed_interval if entity_seed_interval is not None else ENTITY_SEED_INTERVAL,
        health=health_check_interval if health_check_interval is not None else HEALTH_CHECK_INTERVAL,
        wikilinks=wikilinks_interval if wikilinks_interval is not None else WIKILINKS_INTERVAL,
    )


def _boot_state(deps: WorkerDeps) -> WorkerState:
    """Load prior state from disk (increment restart_count) or start fresh.

    #224 phase 5: if a prior run left a state file, we INCREMENT its
    ``restart_count`` and reuse historical counters so operators see
    lifetime totals across restarts.
    """
    prior = deps.read_state_fn(deps.state_path)
    if prior is not None:
        prior.restart_count += 1
        logger.info("worker: resumed from prior state — restart_count=%d", prior.restart_count)
        return prior
    logger.info("worker: no prior state on disk — starting fresh")
    return deps.state


def _apply_embed_outcome(state: WorkerState, outcome: EmbedRunOutcome, consecutive_noops: int) -> int:
    """Fold an embed outcome into worker state; return the updated no-op streak."""
    new_streak = 0 if outcome.did_work else consecutive_noops + 1
    state.consecutive_embed_noops = new_streak
    state.embedded_total += outcome.embedded
    state.failed_chunks_total += outcome.failed
    state.last_embed_run_at = time.time()
    state.last_embed_did_work = outcome.did_work
    if outcome.recall_passed is False:
        state.recall_alerts_total += 1
    return new_streak


def _check_paused(deps: WorkerDeps, transition: Callable[[WorkerPhase], None], previously_paused: bool) -> bool:
    """Handle the operator-pause flag. Returns the new ``previously_paused`` value.

    When the flag is present we sleep and return True; otherwise we restore
    IDLE phase if we were paused and return False.
    """
    if deps.pause_flag_path.exists():
        if not previously_paused:
            transition(WorkerPhase.PAUSED)
            logger.info("worker: paused — flag file present at %s", deps.pause_flag_path)
        deps.sleep(PAUSE_POLL_INTERVAL_S)
        return True
    if previously_paused:
        transition(WorkerPhase.IDLE)
        logger.info("worker: resumed — flag file removed")
    return False


def _log_maintenance_toggle(maintenance_active: bool, previously_skipping: bool, streak: int) -> bool:
    """Log skip-enter / skip-exit transitions; return the new ``previously_skipping`` flag."""
    if not maintenance_active and not previously_skipping:
        logger.info(
            "worker: skipping maintenance scans — %d consecutive no-op embeds (threshold %d)",
            streak,
            MAINTENANCE_SKIP_NOOP_THRESHOLD,
        )
        return True
    if maintenance_active and previously_skipping:
        logger.info("worker: maintenance scans resumed — embed found work")
        return False
    return previously_skipping


def _run_embed_cycle(
    deps: WorkerDeps,
    state: WorkerState,
    transition: Callable[[WorkerPhase], None],
    streak: int,
) -> int:
    """Run one embed pass, persist state, log idle-backoff if applicable. Returns new streak."""
    transition(WorkerPhase.INGEST)
    outcome = run_embed_with_outcome(deps)
    new_streak = _apply_embed_outcome(state, outcome, streak)
    transition(WorkerPhase.IDLE)
    return new_streak


def _run_maintenance_task(
    deps: WorkerDeps,
    transition: Callable[[WorkerPhase], None],
    task: Callable[[WorkerDeps], None],
) -> None:
    """Run one maintenance task with MAINTENANCE→IDLE phase transitions."""
    transition(WorkerPhase.MAINTENANCE)
    task(deps)
    transition(WorkerPhase.IDLE)


def main(
    *,
    deps: WorkerDeps | None = None,
    embed_interval: int | None = None,
    entity_seed_interval: int | None = None,
    health_check_interval: int | None = None,
    wikilinks_interval: int | None = None,
) -> None:
    """Run the worker loop.

    All callable dependencies are bundled into ``WorkerDeps``;
    interval ints stay as plain kwargs because they're scalar
    config (not test-substitution seams). Production omits ``deps``
    and the default factory wires the real task callables.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    deps = deps if deps is not None else WorkerDeps()
    schedule = _resolve_schedule(embed_interval, entity_seed_interval, health_check_interval, wikilinks_interval)

    logger.info(
        "kairix worker starting — embed every %ds, entity seed every %ds, wikilinks every %ds",
        schedule.embed,
        schedule.entity,
        schedule.wikilinks,
    )

    state = _boot_state(deps)
    # Persist initial state (STARTING) so ``kairix worker status`` is
    # answerable immediately after boot, before the first embed completes.
    state.current_phase = WorkerPhase.STARTING
    state.last_phase_change_at = time.time()
    deps.write_state_fn(state, deps.state_path)

    def _transition(phase: WorkerPhase) -> None:
        """Update state's phase + timestamp and persist atomically.

        Each call is a single write — the persistence layer's temp-file +
        rename keeps concurrent ``kairix worker status`` readers safe.
        """
        state.current_phase = phase
        state.last_phase_change_at = time.time()
        deps.write_state_fn(state, deps.state_path)

    # Track when each task last ran
    last_embed = 0.0
    last_entity = 0.0
    last_health = 0.0
    last_wikilinks = 0.0

    # #224 idle backoff: extend the embed interval after consecutive
    # no-op runs to avoid steady CPU/I/O pressure on idle vaults.
    consecutive_embed_noops = state.consecutive_embed_noops

    # Graceful shutdown
    running = True

    def _shutdown(signum: int, frame: object) -> None:
        nonlocal running
        logger.info("worker: shutdown signal received")
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Run embed immediately on startup
    consecutive_embed_noops = _run_embed_cycle(deps, state, _transition, consecutive_embed_noops)
    last_embed = time.monotonic()

    # #224 phase 4: one-shot log on pause/resume so we don't spam every 5s.
    previously_paused = False
    # #224 phase 2: same one-shot-log pattern for maintenance-skip episodes.
    previously_skipping_maint = False

    while running:
        previously_paused = _check_paused(deps, _transition, previously_paused)
        if previously_paused:
            continue

        now = time.monotonic()
        effective_embed_interval = compute_embed_interval(schedule.embed, consecutive_embed_noops)

        if now - last_embed >= effective_embed_interval:
            if effective_embed_interval != schedule.embed:
                logger.info(
                    "worker: idle backoff active — embed interval extended to %ds after %d no-op cycle(s)",
                    effective_embed_interval,
                    consecutive_embed_noops,
                )
            consecutive_embed_noops = _run_embed_cycle(deps, state, _transition, consecutive_embed_noops)
            last_embed = now

        # #224 phase 2 — skip-on-noop maintenance gating. After
        # MAINTENANCE_SKIP_NOOP_THRESHOLD consecutive no-op embed cycles
        # the three maintenance scans become pointless work. Embed continues
        # on its (already exponentially-backed-off) cadence, so a single
        # fresh document still resumes everything.
        maintenance_active = consecutive_embed_noops < MAINTENANCE_SKIP_NOOP_THRESHOLD
        previously_skipping_maint = _log_maintenance_toggle(
            maintenance_active, previously_skipping_maint, consecutive_embed_noops
        )

        if maintenance_active and now - last_entity >= schedule.entity:
            _run_maintenance_task(deps, _transition, run_entity_seed)
            last_entity = now

        if maintenance_active and now - last_health >= schedule.health:
            _run_maintenance_task(deps, _transition, run_health_check)
            last_health = now

        if maintenance_active and now - last_wikilinks >= schedule.wikilinks:
            _run_maintenance_task(deps, _transition, run_wikilinks_inject)
            last_wikilinks = now

        # Sleep 60 seconds between checks
        for _ in range(60):
            if not running:
                break
            deps.sleep(1)

    logger.info("kairix worker stopped")


if __name__ == "__main__":
    main()
