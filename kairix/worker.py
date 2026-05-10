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
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kairix.core.embed.use_cases import EmbedPipelineResult

logger = logging.getLogger(__name__)

# Task schedule (seconds between runs)
EMBED_INTERVAL = 3600  # 1 hour
ENTITY_SEED_INTERVAL = 86400  # 24 hours
HEALTH_CHECK_INTERVAL = 21600  # 6 hours


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


def _default_health_check() -> list[Any]:
    """Default health check — lazy-imports and runs all deployment checks."""
    from kairix.platform.onboard.check import run_all_checks

    return run_all_checks()


def run_embed(embed_fn: Callable[[], Any] | None = None) -> None:
    """Run incremental embed — indexes new and changed documents.

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

    ``embed_fn`` injection seam: tests pass a callable returning either
    the result dataclass or None (legacy). Production passes
    ``_default_embed`` which runs the use case.
    """
    if embed_fn is None:
        embed_fn = _default_embed
    try:
        logger.info("worker: starting incremental embed")
        result = embed_fn()
        if result is None:
            logger.info("worker: embed complete")
            return
        # The defensive getattr() chain accommodates legacy test stubs
        # that return ad-hoc objects without the full result dataclass.
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
    # Catch (Exception, SystemExit) — a SystemExit from the embed step
    # (e.g. legacy CLI calling sys.exit(1) on recall-gate failure) must
    # NOT terminate the worker. Graceful shutdown is signal-driven via
    # SIGTERM/SIGINT in main(), which sets ``running = False`` rather
    # than raising. KeyboardInterrupt is intentionally NOT caught — if
    # signals fail and a Ctrl+C reaches us mid-call, propagation is the
    # right behaviour for a developer running locally.
    except (Exception, SystemExit) as exc:
        logger.warning("worker: embed pipeline raised — %s", exc)


def run_entity_seed(entity_seed_fn: Callable[[], None] | None = None) -> None:
    """Run entity relationship seeding from document store structure.

    Args:
        entity_seed_fn: Callable that performs the entity seed. Defaults to
                        the production store crawl CLI entry point.
    """
    if entity_seed_fn is None:
        entity_seed_fn = _default_entity_seed
    try:
        logger.info("worker: starting entity seed")
        entity_seed_fn()
        logger.info("worker: entity seed complete")
    except Exception as exc:
        logger.warning("worker: entity seed failed — %s", exc)


def run_health_check(health_check_fn: Callable[[], list[Any]] | None = None) -> None:
    """Log a health check.

    Args:
        health_check_fn: Callable that returns a list of check results.
                         Defaults to the production run_all_checks.
    """
    if health_check_fn is None:
        health_check_fn = _default_health_check
    try:
        results = health_check_fn()
        passed = sum(1 for r in results if r.ok)
        total = len(results)
        logger.info("worker: health check %d/%d passed", passed, total)
    except Exception as exc:
        logger.warning("worker: health check failed — %s", exc)


def main(
    *,
    embed_fn: Callable[[], None] | None = None,
    entity_seed_fn: Callable[[], None] | None = None,
    health_check_fn: Callable[[], list[Any]] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    embed_interval: int | None = None,
    entity_seed_interval: int | None = None,
    health_check_interval: int | None = None,
) -> None:
    """Run the worker loop.

    All dependencies are injectable for testing. Production defaults are
    used when arguments are None.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    _embed_interval = embed_interval if embed_interval is not None else EMBED_INTERVAL
    _entity_interval = entity_seed_interval if entity_seed_interval is not None else ENTITY_SEED_INTERVAL
    _health_interval = health_check_interval if health_check_interval is not None else HEALTH_CHECK_INTERVAL
    _sleep = sleep_fn if sleep_fn is not None else time.sleep

    logger.info(
        "kairix worker starting — embed every %ds, entity seed every %ds",
        _embed_interval,
        _entity_interval,
    )

    # Track when each task last ran
    last_embed = 0.0
    last_entity = 0.0
    last_health = 0.0

    # Graceful shutdown
    running = True

    def _shutdown(signum: int, frame: object) -> None:
        nonlocal running
        logger.info("worker: shutdown signal received")
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Run embed immediately on startup
    run_embed(embed_fn)
    last_embed = time.monotonic()

    while running:
        now = time.monotonic()

        if now - last_embed >= _embed_interval:
            run_embed(embed_fn)
            last_embed = now

        if now - last_entity >= _entity_interval:
            run_entity_seed(entity_seed_fn)
            last_entity = now

        if now - last_health >= _health_interval:
            run_health_check(health_check_fn)
            last_health = now

        # Sleep 60 seconds between checks
        for _ in range(60):
            if not running:
                break
            _sleep(1)

    logger.info("kairix worker stopped")


if __name__ == "__main__":
    main()
