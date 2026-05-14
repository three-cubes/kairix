"""
kairix worker — background worker CLI with observable status (#224 phase 5).

Subcommands:
  run     Start the worker loop (default if no subcommand given).
  status  Print the worker's last-known state from the persisted JSON
          file. Exit 0 if the file is present, 1 if missing — so a
          shell-script monitor (or Docker healthcheck) can detect a
          non-running / never-started worker.

Examples:
    kairix worker            # start the loop (back-compat with v2026.5.x)
    kairix worker run        # explicit form
    kairix worker status     # print phase + counters

The status subcommand exists so operators don't have to ``cat`` the
JSON file and parse it themselves — and so monitoring tooling has a
stable shape to wire alerts against.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import TextIO

from kairix.paths import worker_state_path
from kairix.worker_state import WorkerState, read_state


def _format_age(seconds_ago: float) -> str:
    """Render an epoch-delta as a short human-readable duration.

    Mirrors the cadence operators care about: 0..60s → seconds, 0..60m
    → minutes, otherwise hours. ``never`` for the explicit zero
    sentinel (``WorkerState.last_embed_run_at`` defaults to 0.0 until
    the first embed completes).
    """
    if seconds_ago <= 0:
        return "never"
    if seconds_ago < 60:
        return f"{int(seconds_ago)}s ago"
    if seconds_ago < 3600:
        return f"{int(seconds_ago / 60)} min ago"
    return f"{seconds_ago / 3600:.1f} h ago"


def format_status(state: WorkerState, now: float | None = None) -> str:
    """Render a ``WorkerState`` as the human-readable text ``status`` prints.

    Pure function so a unit test can call it directly with a fixed
    ``now`` and assert the rendered string contains the expected fields.
    Keeping the rendering separate from the I/O makes the status
    sub-command testable without ever touching the filesystem.
    """
    now = now if now is not None else time.time()
    last_embed = _format_age(now - state.last_embed_run_at) if state.last_embed_run_at > 0 else "never"
    uptime = _format_age(now - state.started_at) if state.started_at > 0 else "unknown"
    lines = [
        f"Phase: {state.current_phase.value.upper()}",
        f"Last embed: {last_embed} (did work: {state.last_embed_did_work})",
        f"Embedded total: {state.embedded_total}",
        f"Failed chunks total: {state.failed_chunks_total}",
        f"Recall alerts: {state.recall_alerts_total}",
        f"Consecutive no-ops: {state.consecutive_embed_noops}",
        f"Restart count: {state.restart_count}",
        f"Uptime: {uptime}",
    ]
    return "\n".join(lines)


def status(
    *,
    state_path: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``kairix worker status`` implementation.

    Returns the process exit code: 0 if a state file exists and was
    readable, 1 if missing (so monitoring scripts can detect "worker
    never started" without parsing stdout).

    All I/O sinks (``out`` / ``err``) are injectable so the unit test
    captures stdout/stderr without monkeypatching ``sys``.
    """
    state_path = state_path if state_path is not None else worker_state_path()
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    state = read_state(state_path)
    if state is None:
        err.write(f"kairix worker: no state file at {state_path} — worker not running or never started\n")
        return 1
    out.write(format_status(state) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argparse for ``kairix worker [run|status]``.

    ``run`` is the default when no subcommand is given — preserves
    back-compat with pre-#224 callers that invoked ``kairix worker``
    expecting the loop to start.
    """
    parser = argparse.ArgumentParser(
        prog="kairix worker",
        description="Background worker: re-index, recall-check, embedding refresh on a timer.",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="Start the worker loop (default).")
    sub.add_parser("status", help="Print the worker's last-known phase and counters.")
    return parser


def main(argv: list[str] | None = None, *, state_path: Path | None = None) -> int | None:
    """CLI entry point.

    Routes to either ``status`` (read JSON, print, exit code) or the
    worker loop (``kairix.worker.main``). Returns the exit code for the
    parent ``kairix`` dispatcher to sys.exit on; the worker loop returns
    None (never exits normally — runs until SIGTERM).

    ``state_path`` is the test seam: passing an explicit path is the
    F1-clean way to redirect ``status`` to a tmp file without patching
    an internal module attribute.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "status":
        return status(state_path=state_path)

    # Default: run the worker loop. Lazy-import so ``status`` doesn't
    # pay the import cost of the embed pipeline / Azure stack.
    from kairix.worker import main as worker_main

    worker_main()
    return None
