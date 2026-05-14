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
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairix.paths import worker_state_path
from kairix.worker_state import WorkerPhase, WorkerState, read_state, write_state

if TYPE_CHECKING:
    from kairix.core.embed.use_cases import EmbedPipelineResult

logger = logging.getLogger(__name__)

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
            os.environ.get("KAIRIX_DOCUMENT_ROOT", str(Path.home() / "Documents")),
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
    # #224 phase 5 — observable state. ``state`` defaults to None and is
    # initialised inside main() so the boot path can read any prior state
    # off disk first. ``state_path`` defaults to the production location
    # via ``worker_state_path()``; tests pass a tmp_path. ``write_state_fn``
    # is the test seam — F6-clean (typed Callable with default_factory, not
    # ``write_state_fn: Callable | None = None``).
    state: WorkerState | None = None
    state_path: Path = field(default_factory=worker_state_path)
    write_state_fn: Callable[[WorkerState, Path], None] = field(default_factory=lambda: write_state)
    read_state_fn: Callable[[Path], WorkerState | None] = field(default_factory=lambda: read_state)


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
        embedded = getattr(result, "embedded", None)
        failed = getattr(result, "failed", None)
        recall_score = getattr(result, "recall_score", None)
        recall_passed = getattr(result, "recall_passed", None)
        recall_alert = getattr(result, "recall_alert", None)
        diagnostics = getattr(result, "diagnostics", None) or []
        logger.info(
            "worker: embed complete — embedded=%s failed=%s recall=%s",
            embedded,
            failed,
            f"{recall_score:.0%}" if isinstance(recall_score, float) else "n/a",
        )
        if isinstance(failed, int) and failed > 0:
            logger.warning("worker: %d chunks failed during embed", failed)
        if recall_passed is False:
            logger.warning(
                "worker: recall gate alert — %s",
                recall_alert or "search quality degraded; see kairix onboard check",
            )
        for diag in diagnostics:
            logger.warning("worker: %s", diag)
        did_work = (isinstance(embedded, int) and embedded > 0) or (isinstance(failed, int) and failed > 0)
        return EmbedRunOutcome(
            did_work=did_work,
            embedded=embedded if isinstance(embedded, int) else 0,
            failed=failed if isinstance(failed, int) else 0,
            recall_passed=recall_passed if isinstance(recall_passed, bool) else None,
        )
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

    _embed_interval = embed_interval if embed_interval is not None else EMBED_INTERVAL
    _entity_interval = entity_seed_interval if entity_seed_interval is not None else ENTITY_SEED_INTERVAL
    _health_interval = health_check_interval if health_check_interval is not None else HEALTH_CHECK_INTERVAL
    _wikilinks_interval = wikilinks_interval if wikilinks_interval is not None else WIKILINKS_INTERVAL

    logger.info(
        "kairix worker starting — embed every %ds, entity seed every %ds, wikilinks every %ds",
        _embed_interval,
        _entity_interval,
        _wikilinks_interval,
    )

    # #224 phase 5 — boot the persisted state.
    #
    # If a prior run left a state file, we INCREMENT its ``restart_count``
    # and reuse the historical counters (embedded_total, recall_alerts_total
    # etc.) so operators see lifetime totals across restarts. If no prior
    # state, we start fresh.
    #
    # ``deps.state`` is the test seam: tests pass a hand-constructed
    # WorkerState and skip the read_state_fn entirely.
    state = deps.state
    if state is None:
        prior = deps.read_state_fn(deps.state_path)
        if prior is not None:
            state = prior
            state.restart_count = state.restart_count + 1
            logger.info("worker: resumed from prior state — restart_count=%d", state.restart_count)
        else:
            state = WorkerState()
            logger.info("worker: no prior state on disk — starting fresh")
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
        # ``state`` is closed-over from main(); never None by this point —
        # main() reassigns it before defining this closure.
        if state is None:  # pragma: no cover — main() guarantees state is bound before _transition runs
            return
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
    _transition(WorkerPhase.INGEST)
    outcome = run_embed_with_outcome(deps)
    consecutive_embed_noops = 0 if outcome.did_work else consecutive_embed_noops + 1
    state.consecutive_embed_noops = consecutive_embed_noops
    state.embedded_total += outcome.embedded
    state.failed_chunks_total += outcome.failed
    state.last_embed_run_at = time.time()
    state.last_embed_did_work = outcome.did_work
    if outcome.recall_passed is False:
        state.recall_alerts_total += 1
    last_embed = time.monotonic()
    _transition(WorkerPhase.IDLE)

    while running:
        now = time.monotonic()
        effective_embed_interval = compute_embed_interval(_embed_interval, consecutive_embed_noops)

        if now - last_embed >= effective_embed_interval:
            if effective_embed_interval != _embed_interval:
                logger.info(
                    "worker: idle backoff active — embed interval extended to %ds after %d no-op cycle(s)",
                    effective_embed_interval,
                    consecutive_embed_noops,
                )
            _transition(WorkerPhase.INGEST)
            outcome = run_embed_with_outcome(deps)
            consecutive_embed_noops = 0 if outcome.did_work else consecutive_embed_noops + 1
            state.consecutive_embed_noops = consecutive_embed_noops
            state.embedded_total += outcome.embedded
            state.failed_chunks_total += outcome.failed
            state.last_embed_run_at = time.time()
            state.last_embed_did_work = outcome.did_work
            if outcome.recall_passed is False:
                state.recall_alerts_total += 1
            last_embed = now
            _transition(WorkerPhase.IDLE)

        if now - last_entity >= _entity_interval:
            _transition(WorkerPhase.MAINTENANCE)
            run_entity_seed(deps)
            last_entity = now
            _transition(WorkerPhase.IDLE)

        if now - last_health >= _health_interval:
            _transition(WorkerPhase.MAINTENANCE)
            run_health_check(deps)
            last_health = now
            _transition(WorkerPhase.IDLE)

        if now - last_wikilinks >= _wikilinks_interval:
            _transition(WorkerPhase.MAINTENANCE)
            run_wikilinks_inject(deps)
            last_wikilinks = now
            _transition(WorkerPhase.IDLE)

        # Sleep 60 seconds between checks
        for _ in range(60):
            if not running:
                break
            deps.sleep(1)

    logger.info("kairix worker stopped")


if __name__ == "__main__":
    main()
