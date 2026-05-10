"""Incremental embed pipeline — the use case the CLI and worker both call.

This module hosts the canonical "scan → embed → recall gate" flow as a
single function (``run_incremental_embed_pipeline``) that returns a
structured ``EmbedPipelineResult``. Two consumers:

  - ``kairix embed`` (CLI) maps the result to a process exit code.
  - ``kairix worker`` (background daemon) calls the function directly,
    inspects the result, logs alerts, and continues to the next interval.

Before this module existed, the worker called ``embed_main()`` from the
CLI. The CLI raises ``SystemExit`` on recall-gate failure; the worker's
``except Exception`` did not catch SystemExit, so any gate alert killed
the worker process. This made the recall gate's job (fire an alert) and
the worker's job (stay alive and run on a schedule) collide via process
semantics rather than data flow. See v2026.5.10 fix-notes.

Design notes:
  - The recall gate is an **alert**, not a fatal error. The use case
    runs the gate (unless ``skip_recall_check=True``) and reports the
    score in the result dataclass. Callers decide what to do.
  - Embed failures (chunks that errored at the Azure boundary) are
    counted in ``failed`` and are retryable on the next run.
  - Schema, scan, FTS rebuild are all in-flow. Splitting them behind
    extra abstractions adds no value here — they always run together.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kairix.core.embed import _pipeline_defaults as _defaults
from kairix.core.embed.deps import EmbedDependencies
from kairix.core.embed.embed import DEFAULT_BATCH_SIZE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbedPipelineResult:
    """Outcome of one ``run_incremental_embed_pipeline`` invocation.

    Attributes:
        embedded: Chunks newly embedded this run.
        failed: Chunks the Azure call failed for. Retried automatically
            on the next run unless ``--force`` is passed.
        skipped: Chunks where the document was already up-to-date.
        duration_s: Wall-clock seconds spent embedding.
        cost_usd: Estimated cost of this run.
        db_path: Absolute path to the SQLite database.
        timestamp: Unix epoch start of this run.
        recall_score: Fraction (0..1) of canary queries that hit. None
            if the recall gate was skipped.
        recall_passed: Whether the recall gate's degradation check
            passed. None if the gate was skipped. False means an alert
            was logged — NOT a fatal error.
        recall_alert: Human-readable alert message when
            ``recall_passed=False``; None otherwise.
        scan_new / scan_updated / scan_errors: Document scan counters.
        diagnostics: Best-effort messages from sub-steps that may have
            partially failed without aborting the pipeline (e.g. agent
            resolver unavailable, recall gate raised).
    """

    embedded: int
    failed: int
    skipped: int
    duration_s: float
    cost_usd: float
    db_path: str
    timestamp: int
    recall_score: float | None = None
    recall_passed: bool | None = None
    recall_alert: str | None = None
    scan_new: int = 0
    scan_updated: int = 0
    scan_errors: int = 0
    diagnostics: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Whether the embed pass itself succeeded (no chunks failed).

        Recall-gate failures are NOT counted here — those are alerts,
        and ``recall_passed`` exposes them separately.
        """
        return self.failed == 0


@dataclass(frozen=True)
class PipelineDeps:
    """Injectable dependencies for ``run_incremental_embed_pipeline``.

    Production callers leave every field None and the use case fills in
    real implementations on first access. Tests construct a
    ``PipelineDeps(...)`` with light-weight stand-ins to drive the
    orchestration end-to-end without touching real DB / Azure / disk.

    All fields are intentionally callables (not concrete instances) so
    the use case stays decoupled from import order and module-level
    state in the production helpers.
    """

    db_path_fn: Callable[[], str] | None = None
    open_db_fn: Callable[[Path], Any] | None = None
    schema_fn: Callable[[Any], None] | None = None
    validate_schema_fn: Callable[[Any], None] | None = None
    acquire_lock_fn: Callable[[], Any] | None = None
    release_lock_fn: Callable[[Any], None] | None = None
    save_run_log_fn: Callable[[dict[str, Any]], None] | None = None
    run_embed_fn: Callable[..., dict[str, Any]] | None = None
    run_recall_gate_fn: Callable[..., tuple[bool, dict[str, Any]]] | None = None
    scan_documents_fn: Callable[[Any, list[str]], tuple[int, int, int]] | None = None


def run_incremental_embed_pipeline(
    *,
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    skip_recall_check: bool = False,
    rebuild_canaries: bool = False,
    deps: EmbedDependencies | None = None,
    pipeline_deps: PipelineDeps | None = None,
) -> EmbedPipelineResult:
    """Run the full incremental embed pipeline and return a structured result.

    The pipeline:

      1. Acquire the embed lock (so we don't run two embeds concurrently).
      2. Open the SQLite DB; ensure schema exists.
      3. Scan the document root for new / changed files.
      4. Rebuild the FTS index when the scan saw any new or updated doc.
      5. Run ``run_embed`` over the pending chunks.
      6. (Optional) Run the recall gate. The gate's outcome is captured
         in the result dataclass; failures are alerts, not exceptions.

    Raises only on truly unrecoverable conditions (DB unreachable,
    schema migration failure). All other failure modes — Azure errors,
    recall regression, scan errors — are reported in the result.

    ``deps`` injects embed-stage dependencies (Azure config, batch I/O).
    ``pipeline_deps`` injects orchestration dependencies (DB, lock,
    scan, recall) — used by tests to drive the full flow without
    touching production disk or Azure. Production callers leave both
    None and lazy production defaults are wired on demand.
    """
    pdeps = pipeline_deps or PipelineDeps()

    db_path_fn = pdeps.db_path_fn or _defaults.default_db_path
    open_db_fn = pdeps.open_db_fn or _defaults.default_open_db
    schema_fn = pdeps.schema_fn or _defaults.default_create_schema
    validate_fn = pdeps.validate_schema_fn or _defaults.default_validate_schema
    acquire_fn = pdeps.acquire_lock_fn or _defaults.default_acquire_lock
    release_fn = pdeps.release_lock_fn or _defaults.default_release_lock
    save_log_fn = pdeps.save_run_log_fn or _defaults.default_save_run_log
    embed_fn = pdeps.run_embed_fn or _defaults.default_run_embed
    recall_fn = pdeps.run_recall_gate_fn or _defaults.default_run_recall_gate
    scan_fn = pdeps.scan_documents_fn or _defaults.default_scan_documents

    diagnostics: list[str] = []

    logger.info(
        "embed pipeline starting — force=%s limit=%s batch_size=%s",
        force,
        limit,
        batch_size,
    )

    lock_fh = acquire_fn()
    db_path = db_path_fn()
    start = time.time()
    embed_result: dict[str, Any]

    try:
        db = open_db_fn(Path(db_path))
        try:
            schema_fn(db)
            validate_fn(db)

            scan_new, scan_updated, scan_errors = scan_fn(db, diagnostics)

            embed_result = embed_fn(
                db=db,
                force=force,
                batch_size=batch_size,
                limit=limit,
                deps=deps,
            )
            embed_result["command"] = "embed"
            embed_result["db_path"] = str(db_path)
            embed_result["timestamp"] = int(start)
            save_log_fn(embed_result)
        finally:
            db.close()
    finally:
        release_fn(lock_fh)

    recall_score: float | None = None
    recall_passed: bool | None = None
    recall_alert: str | None = None

    if not skip_recall_check:
        captured_alert: list[str] = []

        def _capture(msg: str) -> None:
            captured_alert.append(msg)

        try:
            recall_passed, recall_result = recall_fn(
                alert_callback=_capture,
                rebuild_canaries=rebuild_canaries,
            )
            recall_score = float(recall_result.get("score", 0.0))
            if captured_alert:
                recall_alert = captured_alert[0]
        # The recall gate is best-effort; swallowing its errors keeps the
        # caller's primary signal (embed result) intact. The diagnostic
        # is captured so operators can still see what went wrong.
        except Exception as exc:
            logger.warning("recall gate failed to run: %s", exc)
            diagnostics.append(f"recall_gate_error: {exc}")

    return EmbedPipelineResult(
        embedded=int(embed_result.get("embedded", 0)),
        failed=int(embed_result.get("failed", 0)),
        skipped=int(embed_result.get("skipped", 0)),
        duration_s=float(embed_result.get("duration_s", 0)),
        cost_usd=float(embed_result.get("estimated_cost_usd", 0.0)),
        db_path=str(db_path),
        timestamp=int(start),
        recall_score=recall_score,
        recall_passed=recall_passed,
        recall_alert=recall_alert,
        scan_new=scan_new,
        scan_updated=scan_updated,
        scan_errors=scan_errors,
        diagnostics=diagnostics,
    )
