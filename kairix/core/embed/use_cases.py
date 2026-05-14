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

from kairix.core.embed.deps import EmbedDependencies
from kairix.core.embed.embed import DEFAULT_BATCH_SIZE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Production defaults — lazy-import wrappers used by ``PipelineDeps``'
# ``default_factory`` slots. Each helper is a thin pass-through to the
# real implementation; lazy ``from kairix.<pkg> import ...`` calls keep
# import-time cost off any unit test that injects stand-ins.
#
# These helpers are tested in aggregate by
# ``tests/integration/test_recall_gate_pipeline.py`` and the production
# embed flow on the VM. They are on the F7 per-file-coverage baseline
# as production-wiring code.
# ---------------------------------------------------------------------------


def _default_db_path() -> str:
    from kairix.core.db import get_db_path

    return str(get_db_path())


def _default_open_db(path: Path) -> Any:
    from kairix.core.db import open_db

    return open_db(path)


def _default_create_schema(db: Any) -> None:
    from kairix.core.db.schema import create_schema

    create_schema(db)


def _default_validate_schema(db: Any) -> None:
    from kairix.core.db.schema import validate_schema

    validate_schema(db)


def _default_acquire_lock() -> Any:
    from kairix.core.embed.cli import acquire_lock

    return acquire_lock()


def _default_release_lock(lock_fh: Any) -> None:
    from kairix.core.embed.cli import release_lock

    release_lock(lock_fh)


def _default_save_run_log(entry: dict[str, Any]) -> None:
    from kairix.core.embed.schema import save_run_log

    save_run_log(entry)


def _default_run_embed(**kwargs: Any) -> dict[str, Any]:
    from kairix.core.embed.embed import run_embed

    return run_embed(**kwargs)


def _default_run_recall_gate(**kwargs: Any) -> tuple[bool, dict[str, Any]]:
    from kairix.core.embed.recall_check import run_recall_gate

    return run_recall_gate(**kwargs)


def _default_scan_documents(db: Any, diagnostics: list[str]) -> tuple[int, int, int]:
    """Scan the document root for new/changed files and rebuild FTS.

    Lives in the use-case module so ``PipelineDeps``' default factory
    can wire it directly. Integration-tested via the full embed flow
    against a real DB; unit testing it would require a fake document
    corpus and is not load-bearing for the worker fix.
    """
    from kairix.core.db.scanner import CollectionConfig, DocumentScanner
    from kairix.core.search.config_loader import _resolve_config_path, load_collections
    from kairix.core.search.registry import build_agent_owner_resolver, parse_agent_registry
    from kairix.paths import document_root, reference_library_root

    droot = document_root()

    agent_resolver = None
    try:
        config_path = _resolve_config_path()
        if config_path is not None:
            import yaml as _yaml

            with config_path.open(encoding="utf-8") as _f:
                _raw_yaml = _yaml.safe_load(_f) or {}
            _registry = parse_agent_registry(_raw_yaml)
            if _registry.list_agents():
                agent_resolver = build_agent_owner_resolver(_registry)
    # Agent-resolver construction is best-effort; we'd rather scan with
    # agent_owner=NULL than skip the scan entirely.
    except Exception as exc:
        diagnostics.append(f"agent_resolver_unavailable: {exc}")

    scanner = DocumentScanner(db, document_root=droot, agent_owner_resolver=agent_resolver)

    collections_cfg = load_collections()
    if collections_cfg and collections_cfg.shared:
        scan_collections = [CollectionConfig(name=c.name, path=c.path, glob=c.glob) for c in collections_cfg.shared]
        logger.info("Using %d configured collections", len(scan_collections))
    else:
        scan_collections = [CollectionConfig(name="default", path=".")]

    reflib_root = reference_library_root()
    if reflib_root.is_dir():
        scan_collections.append(CollectionConfig(name="reference-library", path=str(reflib_root), glob="**/*.md"))

    scan_report = scanner.scan(scan_collections)
    if scan_report.new > 0 or scan_report.updated > 0:
        logger.info(
            "Scanned documents: %d new, %d updated, %d unchanged",
            scan_report.new,
            scan_report.updated,
            scan_report.unchanged,
        )
        from kairix.core.db.fts import rebuild_fts

        fts_count = rebuild_fts(db)
        logger.info("FTS index rebuilt: %d documents", fts_count)

    return scan_report.new, scan_report.updated, scan_report.errors


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

    Production callers leave every field unset and the dataclass'
    ``default_factory`` wires the real implementations. Tests construct
    a ``PipelineDeps(...)`` with light-weight stand-ins to drive the
    orchestration end-to-end without touching real DB / Azure / disk.

    All fields are intentionally callables (not concrete instances) so
    the use case stays decoupled from import order and module-level
    state in the production helpers.

    All callable fields use ``field(default_factory=lambda: _default_X)``
    rather than ``Callable[...] | None = None`` (per CLAUDE.md F6
    guidance: avoid the ``Optional[Callable] + post-init`` pattern) so
    mypy sees the production callable directly and
    ``run_incremental_embed_pipeline`` invokes ``pdeps.x_fn(...)``
    without a None-fallback ladder.
    """

    db_path_fn: Callable[[], str] = field(default_factory=lambda: _default_db_path)
    open_db_fn: Callable[[Path], Any] = field(default_factory=lambda: _default_open_db)
    schema_fn: Callable[[Any], None] = field(default_factory=lambda: _default_create_schema)
    validate_schema_fn: Callable[[Any], None] = field(default_factory=lambda: _default_validate_schema)
    acquire_lock_fn: Callable[[], Any] = field(default_factory=lambda: _default_acquire_lock)
    release_lock_fn: Callable[[Any], None] = field(default_factory=lambda: _default_release_lock)
    save_run_log_fn: Callable[[dict[str, Any]], None] = field(default_factory=lambda: _default_save_run_log)
    run_embed_fn: Callable[..., dict[str, Any]] = field(default_factory=lambda: _default_run_embed)
    run_recall_gate_fn: Callable[..., tuple[bool, dict[str, Any]]] = field(
        default_factory=lambda: _default_run_recall_gate
    )
    scan_documents_fn: Callable[[Any, list[str]], tuple[int, int, int]] = field(
        default_factory=lambda: _default_scan_documents
    )


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

    db_path_fn = pdeps.db_path_fn
    open_db_fn = pdeps.open_db_fn
    schema_fn = pdeps.schema_fn
    validate_fn = pdeps.validate_schema_fn
    acquire_fn = pdeps.acquire_lock_fn
    release_fn = pdeps.release_lock_fn
    save_log_fn = pdeps.save_run_log_fn
    embed_fn = pdeps.run_embed_fn
    recall_fn = pdeps.run_recall_gate_fn
    scan_fn = pdeps.scan_documents_fn

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
