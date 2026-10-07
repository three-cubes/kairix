"""
kairix worker — operator CLI for the background worker (#224 phases 4 + 5).

Subcommands:
  run        Start the worker loop (default if no subcommand given).
  status     Print the worker's last-known state from the persisted JSON file.
             Exit 0 if present, 1 if missing — so a shell monitor (or Docker
             healthcheck) can detect a never-started worker. (#224 phase 5)
  pause      Touch the pause flag file. The running worker enters PAUSED phase
             at the next loop iteration (within ``PAUSE_POLL_INTERVAL_S``) and
             stops doing task work until the flag is removed. (#224 phase 4)
  resume     Remove the pause flag file. The running worker transitions back
             to IDLE at the next loop iteration. Idempotent. (#224 phase 4)
  preflight  Audit the persistence invariants (FTS rows match documents,
             vectors match content, no orphans). Exit 0 if healthy, 1 if any
             error-severity gap. Run before every deploy / cutover; the
             worker boot path runs the same check and logs the report at
             startup. ``--auto-heal`` invokes ``rebuild_fts`` for the
             documents-without-fts gap.

Pause/resume are deliberately decoupled from the worker process: they only
toggle a touch-file in the kairix data dir. A stuck/unresponsive worker can
still be paused (so it stops piling on a shared host), and an operator pause
survives worker restarts.

Tests inject ``state_path`` / ``flag_path`` directly so they don't need to
monkeypatch env vars or touch the user's real data dir. The F30 subprocess
seam is the ``--state-path`` / ``--flag-path`` argparse args (mirrors the
``--document-root`` pattern from ``kairix store crawl``), so an outcome
test can drive the binary against a tmp path without touching the process
environment (F2-clean).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from kairix.paths import worker_pause_flag_path, worker_state_path
from kairix.worker_state import WorkerState, read_state

if TYPE_CHECKING:
    from kairix.core.connectors.topology_applier import ConfigDriftReport

# argparse store-true action — extracted because the literal appears
# on every boolean flag and the no-duplicate-string rule trips at 3+.
_STORE_TRUE = "store_true"

# F17 — the ``maintenance`` subcommand name appears in the dispatch
# table, parser, and JSON envelope; pulling it to a constant keeps
# rename surgery a single edit.
_MAINTENANCE_SUBCOMMAND = "maintenance"


def _format_age(seconds_ago: float) -> str:
    if seconds_ago <= 0:
        return "never"
    if seconds_ago < 60:
        return f"{int(seconds_ago)}s ago"
    if seconds_ago < 3600:
        return f"{int(seconds_ago / 60)} min ago"
    return f"{seconds_ago / 3600:.1f} h ago"


def format_status(state: WorkerState, now: float | None = None) -> str:
    """Pure renderer: turn ``WorkerState`` into a multi-line status string.

    ``now`` is injectable so a unit test can pin the clock and assert
    deterministic age renderings without monkeypatching ``time.time``.
    """
    now = now if now is not None else time.time()
    last_embed = _format_age(now - state.last_embed_run_at) if state.last_embed_run_at > 0 else "never"
    uptime = _format_age(now - state.started_at) if state.started_at > 0 else "unknown"
    # SYNC-OBS — the connector-sync heartbeat. ``Last connector sync`` is
    # the quiet-vs-dead signal: a recent age here with ``yielded last tick:
    # False`` means the source IS being polled but had no new docs (healthy
    # quiet), whereas ``never`` / a stale age means the sync loop isn't
    # running (dead). ``Syncs attempted`` proves forward progress across ticks.
    last_sync = _format_age(now - state.last_connector_sync_at) if state.last_connector_sync_at > 0 else "never"
    lines = [
        f"Phase: {state.current_phase.value.upper()}",
        f"Last embed: {last_embed} (did work: {state.last_embed_did_work})",
        f"Embedded total: {state.embedded_total}",
        f"Failed chunks total: {state.failed_chunks_total}",
        f"Recall alerts: {state.recall_alerts_total}",
        f"Consecutive no-ops: {state.consecutive_embed_noops}",
        f"Restart count: {state.restart_count}",
        f"Last connector sync: {last_sync} (yielded last tick: {state.last_connector_tick_yielded})",
        f"Syncs attempted: {state.syncs_attempted}",
        f"Connectors polled last tick: {state.last_connector_connectors_polled}",
        f"Synced last tick: {state.last_connector_synced}",
        f"Uptime: {uptime}",
    ]
    return "\n".join(lines)


def _resolve_existing_db_path(db_path: Path | None) -> Path | None:
    """Pick the DB the drift scan should read, or None when there is nothing to read.

    Honours an explicit ``db_path`` (operator ``--db-path`` / in-process seam)
    and otherwise resolves the production default. Either way the path is
    returned ONLY if it already exists — ``kairix worker status`` stays
    read-only, so it never creates an empty index just to scan for config drift
    on a box that has not ingested yet.
    """
    from kairix.core.db import get_db_path

    candidate = db_path if db_path is not None else get_db_path()
    return candidate if candidate.exists() else None


def _config_drift_report(
    db_path: Path | None,
    config_mapping: dict[str, Any] | None,
) -> ConfigDriftReport | None:
    """Best-effort config-drift snapshot for ``kairix worker status`` (issue #726).

    Compares the ``topology_*`` store rows against the current parsed config
    topology and returns a ``ConfigDriftReport`` naming the sources the config
    no longer declares (removed-but-not-pruned, since ``apply_topology`` is
    upsert-only). Read-only: no row is deleted or transitioned. Returns None
    when there is no DB to read, or when any failure (unreadable config, legacy
    schema, locked DB) makes drift undecidable — status must never crash on it.
    """
    resolved_db = _resolve_existing_db_path(db_path)
    if resolved_db is None:
        return None
    from kairix.config.topology import parse_topology
    from kairix.core.connectors.topology_applier import detect_config_drift
    from kairix.core.db import open_db

    mapping = config_mapping if config_mapping is not None else _load_merged_config_mapping_default()
    try:
        parsed = parse_topology(mapping)
        db = open_db(resolved_db)
    except Exception:  # pragma: no cover - defensive best-effort observability boundary
        return None
    try:
        return detect_config_drift(db, parsed)
    finally:
        db.close()


def _load_merged_config_mapping_default() -> dict[str, Any]:
    """Boundary read for the MERGED operator config used by drift detection.

    Delegates to the same layered reader the worker's connector-sync tick uses
    (base + overlay via :func:`kairix.config_layers.load_merged_mapping`) so
    ``kairix worker status`` sees exactly the ``topology:`` block the running
    worker routes against.
    """
    from kairix.config_layers import load_merged_mapping

    return load_merged_mapping()


def status(
    *,
    state_path: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    as_json: bool = False,
    db_path: Path | None = None,
    config_mapping: dict[str, Any] | None = None,
) -> int:
    """``kairix worker status`` — exit 0 if state file present, 1 if missing.

    I/O sinks are injectable so unit tests capture stdout/stderr without
    monkeypatching ``sys``. ``as_json=True`` renders the structured state
    envelope (the same dict ``WorkerState.to_dict`` produces) for machine
    consumers and for the F30 subprocess outcome test.

    ``db_path`` / ``config_mapping`` are the drift-detection seams (issue #726):
    status also compares the ``topology_*`` store rows against the current
    config and surfaces a WARN line naming any source the config no longer
    declares (removed-but-not-pruned). Both default to the production
    resolution; the scan is read-only and best-effort, so a missing DB /
    unreadable config simply omits the WARN.
    """
    state_path = state_path if state_path is not None else worker_state_path()
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    state = read_state(state_path)
    if state is None:
        err.write(f"kairix worker: no state file at {state_path} — worker not running or never started\n")
        return 1
    drift = _config_drift_report(db_path, config_mapping)
    if as_json:
        envelope = state.to_dict()
        if drift is not None and drift.has_drift:
            envelope["config_drift"] = {
                "count": drift.total,
                "connectors": list(drift.connectors),
                "cc_pairs": list(drift.cc_pairs),
                "collections": list(drift.collections),
            }
        out.write(json.dumps(envelope, indent=2) + "\n")
    else:
        out.write(format_status(state) + "\n")
        warn = drift.warn_line() if drift is not None else None
        if warn:
            out.write(warn + "\n")
    return 0


def _resolve_flag_path(flag_path: Path | None) -> Path:
    """Pick the path the pause/resume commands should toggle."""
    return flag_path if flag_path is not None else worker_pause_flag_path()


def pause(*, flag_path: Path | None = None) -> int:
    """Create the pause flag file. Idempotent."""
    path = _resolve_flag_path(flag_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    print("Worker paused. Run 'kairix worker resume' to continue.")
    return 0


def resume(*, flag_path: Path | None = None) -> int:
    """Remove the pause flag file. Idempotent (missing_ok=True)."""
    path = _resolve_flag_path(flag_path)
    path.unlink(missing_ok=True)
    print("Worker resume requested. May take up to 5s for the worker to pick up the change.")
    return 0


def _open_db_for_preflight(db_path: Path | None) -> sqlite3.Connection:
    """Open the SQLite connection preflight should audit.

    Production callers pass ``db_path=None`` and we resolve the default
    via :func:`kairix.core.db.open_db`; tests pass an explicit tmp
    path so preflight runs against a sandboxed DB.
    """
    from kairix.core.db import open_db

    return open_db(db_path) if db_path is not None else open_db()


def _format_gap_line(gap: object) -> str:
    """Render one integrity gap as a short operator-readable line."""
    # Local import to keep worker_cli importable without the integrity
    # module on the path (e.g. during partial-tree static analysis).
    from kairix.core.db.integrity import IntegrityGap

    if not isinstance(gap, IntegrityGap):
        return str(gap)
    sample_str = ", ".join(gap.sample[:3])
    sample_part = f" sample=[{sample_str}]" if sample_str else ""
    return f"[{gap.severity.upper()}] {gap.invariant}: count={gap.count}{sample_part} — {gap.remediation}"


def _auto_heal_gaps(db: sqlite3.Connection, gaps: tuple[object, ...], out: TextIO) -> None:
    """Attempt remediation for the gaps we know how to fix automatically.

    Auto-healable today:
      * ``documents-without-fts`` — runs :func:`rebuild_fts`.
      * ``content-vectors-without-documents`` — runs a
        :class:`~kairix.core.maintenance.MaintenanceScheduler` tick to
        prune the orphan vectors into ``content_vectors_pruned``
        (KFEAT-021 Phase 1 extends the auto-heal surface).

    Other gaps surface for operator action via the per-gap remediation
    string. The helper writes a one-line summary per heal attempt to
    ``out`` so the operator sees what ran.
    """
    from kairix.core.db.fts import rebuild_fts
    from kairix.core.db.integrity import INVARIANT_DOCUMENTS_WITHOUT_FTS, IntegrityGap
    from kairix.core.maintenance import MaintenanceScheduler, MaintenanceSchedulerDeps

    for gap in gaps:
        if not isinstance(gap, IntegrityGap):
            continue
        if gap.invariant == INVARIANT_DOCUMENTS_WITHOUT_FTS:
            out.write(f"auto-heal: rebuilding FTS5 index for {gap.count} documents...\n")
            count = rebuild_fts(db)
            out.write(f"auto-heal: rebuild_fts indexed {count} documents\n")
        elif gap.invariant == "content-vectors-without-documents":
            out.write(f"auto-heal: pruning {gap.count} orphan content_vectors row(s)...\n")
            # Heal-in-place against the same connection — skip the
            # default usearch rebuilder (which would re-open a separate
            # connection to the platform-default DB path) and the FTS
            # healer (the preflight loop already handled FTS5).
            heal_deps = MaintenanceSchedulerDeps(
                usearch_rebuilder=lambda: True,
                fts_healer=lambda _db: 0,
            )
            scheduler = MaintenanceScheduler(db, scheduler_deps=heal_deps)
            result = scheduler.tick(db)
            out.write(
                f"auto-heal: pruned={result.orphans_pruned} "
                f"pruned_table_size={result.pruned_table_size} "
                f"usearch_rebuilt={result.usearch_rebuilt} "
                f"elapsed_ms={result.elapsed_ms}\n"
            )


def _render_preflight_human(report: Any, out: TextIO) -> None:
    """Render the human-readable preflight report (no exit-code logic)."""
    if report.healthy and not report.gaps:
        out.write("Preflight integrity check: PASSED (no gaps detected)\n")
        return
    status_word = "PASSED" if report.healthy else "FAILED"
    out.write(f"Preflight integrity check: {status_word} ({len(report.gaps)} gap(s))\n")
    for gap in report.gaps:
        out.write(_format_gap_line(gap) + "\n")


def preflight(
    *,
    db_path: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    as_json: bool = False,
    auto_heal: bool = False,
) -> int:
    """``kairix worker preflight`` — audit persistence invariants.

    Exit 0 if healthy, 1 if any error-severity gap. Warn / info gaps
    do not flip the exit code but are surfaced for visibility.

    ``--auto-heal`` runs :func:`kairix.core.db.fts.rebuild_fts` for the
    ``documents-without-fts`` gap, then re-runs the audit and reports
    the post-heal state. Mirrors the existing ``kairix embed rebuild-fts``
    surface but folded into the preflight workflow so operators have a
    single "fix what's fixable" entry point.

    The ``db_path`` / ``out`` / ``err`` kwargs are the in-process test
    seam; the CLI binds them from argparse args.
    """
    from kairix.core.db.integrity import check_integrity, report_to_dict
    from kairix.core.maintenance import count_current_orphans, count_pruned_rows

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    db = _open_db_for_preflight(db_path)
    maintenance_block: dict[str, Any]
    try:
        report = check_integrity(db)
        if auto_heal and report.gaps:
            _auto_heal_gaps(db, report.gaps, out)
            db.commit()
            # Re-check post-heal so the operator sees the new state.
            report = check_integrity(db)
        # KFEAT-021 — embed the maintenance counters in the envelope so
        # operators can pivot off the same JSON shape ``kairix worker
        # status`` produces. Reads are best-effort: a missing
        # content_vectors_pruned table (legacy schema) returns 0.
        maintenance_block = _build_preflight_maintenance_block(
            db,
            orphan_count_fn=count_current_orphans,
            pruned_size_fn=count_pruned_rows,
        )
    finally:
        db.close()

    if as_json:
        envelope = report_to_dict(report)
        envelope["maintenance"] = maintenance_block
        out.write(json.dumps(envelope, indent=2) + "\n")
    else:
        _render_preflight_human(report, out)
    return 0 if report.healthy else 1


def _build_preflight_maintenance_block(
    db: sqlite3.Connection,
    *,
    orphan_count_fn: Any,
    pruned_size_fn: Any,
) -> dict[str, Any]:
    """Build the ``maintenance`` block embedded in the preflight envelope.

    Reads the maintenance flag's effective value, the persisted
    ``WorkerState`` last-tick fields, and the live orphan / pruned
    counts. All four are best-effort — failures degrade to "unknown"
    rather than crashing the preflight.
    """
    from kairix.core.features import flag as _flag
    from kairix.core.maintenance import render_iso
    from kairix.paths import worker_state_path
    from kairix.worker_state import read_state

    try:
        enabled = bool(_flag("maintenance_loop"))
    except KeyError:
        enabled = False

    state = read_state(worker_state_path())
    last_tick = float(getattr(state, "last_maintenance_tick_at", 0.0)) if state is not None else 0.0
    last_pruned = int(getattr(state, "last_maintenance_orphans_pruned", 0)) if state is not None else 0

    return {
        "enabled": enabled,
        "last_tick_at": render_iso(last_tick),
        "orphans_pruned_last_tick": last_pruned,
        "current_orphan_count": int(orphan_count_fn(db)),
        "pruned_table_size": int(pruned_size_fn(db)),
    }


def maintenance(
    *,
    db_path: Path | None = None,
    retention_days: int | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    as_json: bool = False,
) -> int:
    """``kairix worker maintenance`` — one-shot MaintenanceScheduler.tick.

    Operator surface for ad-hoc cleanup (cron / k8s CronJob target for
    operators who don't want the worker-loop tick gated by the
    ``maintenance_loop`` feature flag). Always runs — the flag gates
    the worker-loop tick, not this on-demand verb, so operators can
    repair drift even with the flag still OFF.

    Outputs the structured envelope from
    :class:`~kairix.core.maintenance.MaintenanceTickResult`. Exits 0
    iff the tick completed without raising (a successful no-op tick
    where nothing needed pruning is exit 0).

    KFEAT-021 Phase 1 acceptance criterion #4.

    ``db_path`` / ``retention_days`` are the in-process injection
    seams; the ``--db-path`` / ``--retention-days`` CLI flags are the
    subprocess (F30) seams. Precedence: in-process kwarg > CLI flag >
    paths.py default.
    """
    from kairix.core.db.schema import create_schema
    from kairix.core.maintenance import (
        MaintenanceScheduler,
        MaintenanceSchedulerDeps,
        tick_to_dict,
    )
    from kairix.paths import maintenance_retention_days

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    effective_retention = retention_days if retention_days is not None else maintenance_retention_days()

    db = _open_db_for_preflight(db_path)
    try:
        create_schema(db)
        # In-place Deps: heal against the same connection rather than
        # the platform-default DB path. This keeps the verb scoped to
        # whatever ``--db-path`` selected — operators running against
        # a tmp DB or a copy-on-write snapshot don't accidentally poke
        # the production index.
        deps = MaintenanceSchedulerDeps(
            usearch_rebuilder=lambda: _maintenance_verb_usearch_rebuilder(db),
            fts_healer=_maintenance_verb_fts_healer,
        )
        scheduler = MaintenanceScheduler(
            db,
            retention_days=effective_retention,
            scheduler_deps=deps,
        )
        try:
            result = scheduler.tick(db)
        except Exception as exc:  # pragma: no cover - production boundary
            err.write(f"kairix worker maintenance: tick raised — {exc}\n")
            return 1
    finally:
        db.close()

    if as_json:
        out.write(json.dumps(tick_to_dict(result), indent=2) + "\n")
    else:
        out.write(
            f"maintenance tick: pruned={result.orphans_pruned} "
            f"pruned_table_size={result.pruned_table_size} "
            f"usearch_rebuilt={result.usearch_rebuilt} "
            f"fts_orphans_healed={result.fts_orphans_healed} "
            f"current_orphan_count={result.current_orphan_count} "
            f"elapsed_ms={result.elapsed_ms}\n"
        )
    return 0


def _maintenance_verb_usearch_rebuilder(db: sqlite3.Connection) -> bool:
    """In-place usearch rebuilder for the ``kairix worker maintenance`` verb.

    Operates against the verb's already-open connection rather than
    re-opening the platform-default DB — keeps the verb scoped to
    whatever ``--db-path`` selected. Best-effort: returns True on
    no-op (no usearch on disk) and False only when an exception
    propagates from the underlying usearch boundary.
    """
    try:
        from kairix.core.embed.embed import open_default_usearch_index

        idx = open_default_usearch_index()
        if idx is None:
            return True
        try:
            indexed = len(idx)
        except (AttributeError, RuntimeError):
            indexed = -1
        row = db.execute("SELECT COUNT(*) FROM content_vectors").fetchone()
        live = int(row[0]) if row else 0
        # Surface the parity check through the standard logger family
        # so operators see the same line from both verbs and the loop.
        import logging as _logging

        _logging.getLogger("kairix.maintenance").info(
            "maintenance verb: usearch parity check — live=%d indexed=%d",
            live,
            indexed,
        )
        return True
    except sqlite3.OperationalError:
        return True
    except Exception:  # pragma: no cover - production boundary
        return False


def _maintenance_verb_fts_healer(db: sqlite3.Connection) -> int:
    """In-place FTS healer for the ``kairix worker maintenance`` verb.

    Defensive: returns 0 when the integrity helpers fail (e.g. legacy
    schema without ``documents_fts``) so the verb's tick still reports
    a clean envelope.
    """
    try:
        from kairix.core.db.fts import rebuild_fts
        from kairix.core.db.integrity import (
            _check_documents_without_fts,
            _check_fts_without_documents,
        )

        if _check_documents_without_fts(db) or _check_fts_without_documents(db):
            return int(rebuild_fts(db))
        return 0
    except Exception:  # pragma: no cover - production boundary
        return 0


def reextract(
    *,
    source_name: str,
    db_path: Path | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    out: TextIO | None = None,
    err: TextIO | None = None,
    as_json: bool = False,
) -> int:
    """``kairix worker reextract --source-name <src>`` — operator recovery.

    Re-extracts every dead-lettered item for ``source_name`` through the
    currently-registered extractor. Recovers from past extract failures
    (e.g. missing extras fixed in a later release) without requiring
    the source to re-emit the items.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    db = _open_db_for_preflight(db_path)
    try:
        from kairix.worker import run_reextract_dead_letter

        try:
            result = run_reextract_dead_letter(
                source_name=source_name,
                db=db,
                limit=limit,
                dry_run=dry_run,
            )
        except Exception as exc:  # pragma: no cover — production boundary
            err.write(f"kairix worker reextract: raised — {exc}\n")
            return 1
    finally:
        db.close()

    if as_json:
        out.write(
            json.dumps(
                {
                    "source_name": source_name,
                    "recovered": result.recovered,
                    "still_failing": result.still_failing,
                    "skipped_no_bronze": result.skipped_no_bronze,
                    "skipped_no_connector": result.skipped_no_connector,
                    "skipped_source_unavailable": result.skipped_source_unavailable,
                    "dry_run": dry_run,
                },
                indent=2,
            )
            + "\n"
        )
    else:
        prefix = "[dry-run] " if dry_run else ""
        out.write(
            f"{prefix}reextract source={source_name} "
            f"recovered={result.recovered} still_failing={result.still_failing} "
            f"skipped_no_bronze={result.skipped_no_bronze} "
            f"skipped_no_connector={result.skipped_no_connector} "
            f"skipped_source_unavailable={result.skipped_source_unavailable}\n"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argparse for ``kairix worker [run|status|pause|resume]``."""
    parser = argparse.ArgumentParser(
        prog="kairix worker",
        description="Background worker — observable state + operator pause/resume.",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="Start the worker loop (default).")
    status_p = sub.add_parser("status", help="Print the worker's last-known phase and counters.")
    status_p.add_argument(
        "--state-path",
        default=None,
        help=(
            "Override the worker state JSON path for this invocation. When "
            "omitted, defaults to ``kairix.paths.worker_state_path()`` (the "
            "production data dir). Mirrors the ``--document-root`` pattern "
            "from ``kairix store crawl`` so F30 subprocess outcome tests "
            "can drive a tmp state file without touching the process "
            "environment (F2-clean)."
        ),
    )
    status_p.add_argument(
        "--json",
        dest="as_json",
        action=_STORE_TRUE,
        help="Emit the full WorkerState dict as JSON on stdout (machine-readable).",
    )
    status_p.add_argument(
        "--db-path",
        default=None,
        help=(
            "Scan this SQLite index for config drift instead of the default "
            "resolution chain. When omitted, status reads the default index "
            "only if it already exists (the drift scan is read-only). Mirrors "
            "the ``--db-path`` seam on ``preflight`` — F30-clean subprocess "
            "injection without KAIRIX_* env vars (F2-clean)."
        ),
    )
    pause_p = sub.add_parser("pause", help="Pause the running worker by creating a flag file.")
    pause_p.add_argument(
        "--flag-path",
        default=None,
        help=(
            "Override the pause-flag path for this invocation. When omitted, "
            "defaults to ``kairix.paths.worker_pause_flag_path()``. Subprocess "
            "seam for F30 outcome tests; matches ``--state-path`` on status."
        ),
    )
    resume_p = sub.add_parser("resume", help="Resume the running worker by removing the flag file.")
    resume_p.add_argument(
        "--flag-path",
        default=None,
        help=(
            "Override the pause-flag path for this invocation. When omitted, "
            "defaults to ``kairix.paths.worker_pause_flag_path()``. Subprocess "
            "seam for F30 outcome tests; matches ``--state-path`` on status."
        ),
    )
    preflight_p = sub.add_parser(
        "preflight",
        help="Audit persistence invariants; exit 1 if any error-severity gap.",
    )
    preflight_p.add_argument(
        "--db-path",
        default=None,
        help=(
            "Audit this SQLite index instead of the default resolution chain "
            "(``KAIRIX_DB_PATH`` env / kairix.config.yaml / platform default). "
            "F30 subprocess seam — keeps tmp-DB injection out of "
            "monkeypatch.setenv (F2-clean)."
        ),
    )
    preflight_p.add_argument(
        "--json",
        dest="as_json",
        action=_STORE_TRUE,
        help="Emit the full IntegrityReport as JSON on stdout (machine-readable).",
    )
    preflight_p.add_argument(
        "--auto-heal",
        action=_STORE_TRUE,
        help=(
            "Run rebuild_fts for documents-without-fts gaps AND a "
            "MaintenanceScheduler.tick for content-vectors-without-documents "
            "gaps (KFEAT-021 Phase 1), then re-audit. Other gaps require "
            "operator action via the per-gap remediation."
        ),
    )
    maintenance_p = sub.add_parser(
        _MAINTENANCE_SUBCOMMAND,
        help=(
            "Run a MaintenanceScheduler.tick once on demand "
            "(KFEAT-021 Phase 1) — prunes orphan content_vectors, "
            "rebuilds usearch, heals FTS5 drift."
        ),
    )
    maintenance_p.add_argument(
        "--db-path",
        default=None,
        help=(
            "Prune from this SQLite index instead of the default resolution "
            "chain. F30 subprocess seam — keeps tmp-DB injection out of "
            "monkeypatch.setenv (F2-clean)."
        ),
    )
    maintenance_p.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help=(
            "Override the soft-delete retention window (default 7 days, "
            "via KAIRIX_MAINTENANCE_RETENTION_DAYS). Rows older than the "
            "window are hard-deleted from content_vectors_pruned."
        ),
    )
    maintenance_p.add_argument(
        "--json",
        dest="as_json",
        action=_STORE_TRUE,
        help="Emit the MaintenanceTickResult as JSON on stdout (machine-readable).",
    )
    reextract_p = sub.add_parser(
        "reextract",
        help=(
            "Re-extract dead-lettered items for a source using the currently-"
            "registered extractor. Recovers from past extract failures (e.g. "
            "missing extras fixed in a later release) without requiring the "
            "source to re-emit the items."
        ),
    )
    reextract_p.add_argument(
        "--source-name",
        required=True,
        help="connector source name (e.g. sharepoint, obsidian)",
    )
    reextract_p.add_argument(
        "--db-path",
        default=None,
        help="Re-extract against this SQLite index instead of the default resolution chain.",
    )
    reextract_p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of items processed (default: all dead-lettered items for the source).",
    )
    reextract_p.add_argument(
        "--dry-run",
        action=_STORE_TRUE,
        help="Walk the same logic but commit nothing — useful for sizing the recovery first.",
    )
    reextract_p.add_argument(
        "--json",
        dest="as_json",
        action=_STORE_TRUE,
        help="Emit the ReextractResult as JSON on stdout (machine-readable).",
    )
    return parser


def _resolve_state_path(arg: str | None, injected: Path | None) -> Path | None:
    """In-process ``state_path=`` kwarg wins; otherwise use ``--state-path``."""
    if injected is not None:
        return injected
    return Path(arg) if arg else None


def _resolve_flag_path_arg(arg: str | None, injected: Path | None) -> Path | None:
    """In-process ``flag_path=`` kwarg wins; otherwise use ``--flag-path``."""
    if injected is not None:
        return injected
    return Path(arg) if arg else None


def _resolve_db_path_arg(arg: str | None, injected: Path | None) -> Path | None:
    """In-process ``db_path=`` kwarg wins; otherwise use ``--db-path``."""
    if injected is not None:
        return injected
    return Path(arg) if arg else None


def main(
    argv: list[str] | None = None,
    *,
    state_path: Path | None = None,
    flag_path: Path | None = None,
    db_path: Path | None = None,
) -> int | None:
    """CLI entry point. Routes to the right subcommand.

    ``state_path`` / ``flag_path`` / ``db_path`` are the in-process
    injection seams used by unit tests. The ``--state-path`` /
    ``--flag-path`` / ``--db-path`` CLI flags are the subprocess (F30)
    seams. Precedence: in-process kwarg wins over CLI flag wins over
    ``kairix.paths`` defaults.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "status":
        resolved_state = _resolve_state_path(getattr(args, "state_path", None), state_path)
        resolved_db = _resolve_db_path_arg(getattr(args, "db_path", None), db_path)
        # ``as_json`` is always defined by the status subparser (--json,
        # store_true) so read it directly — no dead getattr default.
        return status(
            state_path=resolved_state,
            as_json=args.as_json,
            db_path=resolved_db,
        )
    if args.cmd == "pause":
        resolved_flag = _resolve_flag_path_arg(getattr(args, "flag_path", None), flag_path)
        return pause(flag_path=resolved_flag)
    if args.cmd == "resume":
        resolved_flag = _resolve_flag_path_arg(getattr(args, "flag_path", None), flag_path)
        return resume(flag_path=resolved_flag)
    if args.cmd == "preflight":
        resolved_db = _resolve_db_path_arg(getattr(args, "db_path", None), db_path)
        return preflight(
            db_path=resolved_db,
            as_json=getattr(args, "as_json", False),
            auto_heal=getattr(args, "auto_heal", False),
        )
    if args.cmd == _MAINTENANCE_SUBCOMMAND:
        resolved_db = _resolve_db_path_arg(getattr(args, "db_path", None), db_path)
        return maintenance(
            db_path=resolved_db,
            retention_days=getattr(args, "retention_days", None),
            as_json=getattr(args, "as_json", False),
        )
    if args.cmd == "reextract":
        resolved_db = _resolve_db_path_arg(getattr(args, "db_path", None), db_path)
        return reextract(
            source_name=args.source_name,
            db_path=resolved_db,
            limit=getattr(args, "limit", None),
            dry_run=getattr(args, "dry_run", False),
            as_json=getattr(args, "as_json", False),
        )

    # Default (``None`` or ``run``): start the worker loop.
    from kairix.worker import main as worker_main

    worker_main()
    return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
