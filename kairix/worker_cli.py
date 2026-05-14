"""
kairix worker — operator CLI for the background worker (#224 phases 4 + 5).

Subcommands:
  run     Start the worker loop (default if no subcommand given).
  status  Print the worker's last-known state from the persisted JSON file.
          Exit 0 if present, 1 if missing — so a shell monitor (or Docker
          healthcheck) can detect a never-started worker. (#224 phase 5)
  pause   Touch the pause flag file. The running worker enters PAUSED phase
          at the next loop iteration (within ``PAUSE_POLL_INTERVAL_S``) and
          stops doing task work until the flag is removed. (#224 phase 4)
  resume  Remove the pause flag file. The running worker transitions back
          to IDLE at the next loop iteration. Idempotent. (#224 phase 4)

Pause/resume are deliberately decoupled from the worker process: they only
toggle a touch-file in the kairix data dir. A stuck/unresponsive worker can
still be paused (so it stops piling on a shared host), and an operator pause
survives worker restarts.

Tests inject ``state_path`` / ``flag_path`` directly so they don't need to
monkeypatch env vars or touch the user's real data dir.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import TextIO

from kairix.paths import worker_pause_flag_path, worker_state_path
from kairix.worker_state import WorkerState, read_state


def _format_age(seconds_ago: float) -> str:
    """Render an epoch-delta as a short human-readable duration."""
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
    """``kairix worker status`` — exit 0 if state file present, 1 if missing.

    I/O sinks are injectable so unit tests capture stdout/stderr without
    monkeypatching ``sys``.
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


def build_parser() -> argparse.ArgumentParser:
    """Argparse for ``kairix worker [run|status|pause|resume]``."""
    parser = argparse.ArgumentParser(
        prog="kairix worker",
        description="Background worker — observable state + operator pause/resume.",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="Start the worker loop (default).")
    sub.add_parser("status", help="Print the worker's last-known phase and counters.")
    sub.add_parser("pause", help="Pause the running worker by creating a flag file.")
    sub.add_parser("resume", help="Resume the running worker by removing the flag file.")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    state_path: Path | None = None,
    flag_path: Path | None = None,
) -> int | None:
    """CLI entry point. Routes to the right subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "status":
        return status(state_path=state_path)
    if args.cmd == "pause":
        return pause(flag_path=flag_path)
    if args.cmd == "resume":
        return resume(flag_path=flag_path)

    # Default (``None`` or ``run``): start the worker loop.
    from kairix.worker import main as worker_main

    worker_main()
    return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
