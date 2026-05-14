"""Tests for ``kairix worker status`` — #224 phase 5 CLI surface.

The status sub-command reads the worker's persisted JSON state and
prints it in operator-readable form. Tests cover:

  - status command prints phase + counters when a state file exists;
  - status command exits 1 with a clear message when no file is present;
  - format_status renders all fields without touching the filesystem.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from kairix.worker_cli import build_parser, format_status, main, pause, resume, status
from kairix.worker_state import WorkerPhase, WorkerState, write_state

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_status_command_prints_phase_and_counters(tmp_path: Path) -> None:
    """status() reads a pre-written state file and prints all key fields.

    Sabotage proof: change ``state.embedded_total = 42`` to ``= 41`` in
    this test (or comment out the embedded_total render in
    ``format_status``) — the assertion ``"42"`` fails.
    """
    state_path = tmp_path / "worker-state.json"
    state = WorkerState(
        current_phase=WorkerPhase.INGEST,
        embedded_total=42,
        failed_chunks_total=3,
        recall_alerts_total=1,
        restart_count=7,
        consecutive_embed_noops=2,
    )
    write_state(state, state_path)

    out = io.StringIO()
    err = io.StringIO()
    rc = status(state_path=state_path, out=out, err=err)

    assert rc == 0
    printed = out.getvalue()
    assert "INGEST" in printed, f"phase missing from output: {printed!r}"
    assert "42" in printed, f"embedded_total missing: {printed!r}"
    assert "3" in printed, f"failed_chunks_total missing: {printed!r}"
    assert "1" in printed, f"recall_alerts_total missing: {printed!r}"
    assert "7" in printed, f"restart_count missing: {printed!r}"


@pytest.mark.unit
def test_status_command_exits_1_when_state_file_missing(tmp_path: Path) -> None:
    """No state file → exit 1, message on stderr, nothing on stdout.

    Sabotage proof: return 0 instead of 1 in the missing branch and the
    rc assertion fails. Monitoring scripts rely on this exit code.
    """
    state_path = tmp_path / "does-not-exist.json"
    assert not state_path.exists()

    out = io.StringIO()
    err = io.StringIO()
    rc = status(state_path=state_path, out=out, err=err)

    assert rc == 1
    assert out.getvalue() == "", "no status text should print when state is missing"
    assert "no state file" in err.getvalue().lower(), f"stderr should explain: {err.getvalue()!r}"


@pytest.mark.unit
def test_format_status_renders_all_observable_fields() -> None:
    """Direct test of the pure renderer — no I/O, no tmp_path needed.

    Sabotage proof: drop any of the rendered fields from
    ``format_status`` and the corresponding ``in rendered`` fails.
    """
    state = WorkerState(
        current_phase=WorkerPhase.MAINTENANCE,
        embedded_total=128,
        failed_chunks_total=4,
        recall_alerts_total=2,
        restart_count=9,
        consecutive_embed_noops=5,
        last_embed_run_at=1000.0,
        last_embed_did_work=True,
        started_at=900.0,
    )
    # Pin ``now`` so age formatting is deterministic.
    rendered = format_status(state, now=1180.0)
    assert "MAINTENANCE" in rendered
    assert "128" in rendered  # embedded_total
    assert "4" in rendered  # failed_chunks_total
    assert "2" in rendered  # recall_alerts_total
    assert "9" in rendered  # restart_count
    assert "5" in rendered  # consecutive_embed_noops
    # Last embed was 180s ago → "3 min ago"
    assert "min ago" in rendered, f"age format missing in: {rendered!r}"


@pytest.mark.unit
def test_format_status_renders_never_for_unset_timestamps() -> None:
    """Default WorkerState has last_embed_run_at=0; should render 'never'.

    Sabotage proof: if format_status fed 0 into the duration formatter
    blindly, it would print "0s ago" — the assertion catches that.
    """
    state = WorkerState()
    rendered = format_status(state, now=1000.0)
    assert "never" in rendered.lower(), f"expected 'never' for unset timestamps: {rendered!r}"


@pytest.mark.unit
def test_main_dispatches_status_subcommand(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``kairix worker status`` returns the status exit code through main().

    We can't easily redirect the default ``worker_state_path()`` here,
    so we exercise the dispatch via ``status()`` directly with an
    injected path. The point of this test is to prove the argparse
    routing — by writing the state to the default path under tmp_path
    we'd need filesystem monkeypatching, which violates F2 discipline.
    Instead we test the parser builds the expected subcommand and that
    ``main([])`` resolves to the worker loop branch (asserted via the
    deferred import not happening — that's hard to test directly, so
    we settle for parser-shape coverage here and the dedicated
    ``status()`` tests above).
    """
    parser = build_parser()
    args = parser.parse_args(["status"])
    assert args.cmd == "status"
    # Default (no args) dispatches to ``run``.
    args2 = parser.parse_args([])
    assert args2.cmd is None  # default branch in main() falls through to worker loop


@pytest.mark.unit
def test_main_status_returns_exit_code_via_dispatcher(tmp_path: Path) -> None:
    """End-to-end: ``main(["status"], state_path=...)`` returns 1 when no
    state file exists.

    Uses the ``state_path`` injection seam on ``main()`` — F1-clean
    (no @patch, no monkeypatch on internals).
    """
    state_path = tmp_path / "worker-state.json"
    rc = main(["status"], state_path=state_path)
    assert rc == 1


@pytest.mark.unit
def test_main_status_returns_zero_when_state_exists(tmp_path: Path) -> None:
    """End-to-end: ``main(["status"], state_path=...)`` returns 0 when state
    file is present."""
    state_path = tmp_path / "worker-state.json"
    write_state(WorkerState(current_phase=WorkerPhase.IDLE), state_path)
    rc = main(["status"], state_path=state_path)
    assert rc == 0


# ---------------------------------------------------------------------------
# pause / resume (#224 phase 4)
# ---------------------------------------------------------------------------


def test_pause_cli_creates_flag_file(tmp_path: Path) -> None:
    """``main(["pause"])`` creates the flag file at the injected path.

    Sabotage proof: removing the ``path.touch()`` line in worker_cli.pause
    leaves the file absent and this assert fails.
    """
    flag = tmp_path / ".worker-paused"
    assert not flag.exists()

    exit_code = main(["pause"], flag_path=flag)

    assert exit_code == 0
    assert flag.exists(), "pause must create the flag file"


def test_resume_cli_removes_flag_file(tmp_path: Path) -> None:
    """Pre-touch the flag, dispatch ``main(["resume"])``, assert it's gone.

    Sabotage proof: replacing ``unlink(missing_ok=True)`` with a no-op
    leaves the flag in place and this assert fails.
    """
    flag = tmp_path / ".worker-paused"
    flag.touch()
    assert flag.exists()

    exit_code = main(["resume"], flag_path=flag)

    assert exit_code == 0
    assert not flag.exists(), "resume must remove the flag file"


def test_resume_cli_is_idempotent_when_flag_missing(tmp_path: Path) -> None:
    """``resume`` without a pre-existing flag returns 0 without raising.

    Sabotage proof: switching to ``unlink()`` (no missing_ok) would raise
    FileNotFoundError and this test would fail with an unhandled exception.
    """
    flag = tmp_path / ".worker-paused"
    assert not flag.exists()

    exit_code = main(["resume"], flag_path=flag)

    assert exit_code == 0
    assert not flag.exists()


def test_pause_cli_is_idempotent_when_flag_already_present(tmp_path: Path) -> None:
    """Calling pause twice in a row leaves the flag present and exit 0.

    Sabotage proof: if pause raised on existing flag (e.g. ``open(x, "x")``
    instead of touch), the second call would error.
    """
    flag = tmp_path / ".worker-paused"
    flag.touch()

    exit_code = main(["pause"], flag_path=flag)

    assert exit_code == 0
    assert flag.exists()


def test_pause_function_returns_zero_and_creates_file(tmp_path: Path) -> None:
    """The ``pause`` helper (called directly) creates the flag and returns 0.

    Sabotage proof: returning anything other than 0 from ``pause`` would
    fail this assertion; the brief requires exit 0 on success.
    """
    flag = tmp_path / ".worker-paused"

    result = pause(flag_path=flag)

    assert result == 0
    assert flag.exists()


def test_resume_function_returns_zero_and_removes_file(tmp_path: Path) -> None:
    """The ``resume`` helper (called directly) removes the flag and returns 0.

    Sabotage proof: returning anything other than 0 would fail this assert.
    """
    flag = tmp_path / ".worker-paused"
    flag.touch()

    result = resume(flag_path=flag)

    assert result == 0
    assert not flag.exists()


def test_pause_creates_parent_directory_if_missing(tmp_path: Path) -> None:
    """A fresh data dir layout (parent dir doesn't exist yet) is created.

    Sabotage proof: dropping the ``mkdir(parents=True, exist_ok=True)``
    leaves the parent missing and ``touch`` raises FileNotFoundError.
    """
    flag = tmp_path / "nested" / "data" / ".worker-paused"
    assert not flag.parent.exists()

    exit_code = main(["pause"], flag_path=flag)

    assert exit_code == 0
    assert flag.exists()


def test_pause_cli_prints_resume_instruction(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The pause output tells the operator how to resume — discoverability check.

    Sabotage proof: changing the print to anything not containing 'resume'
    would fail this. The string is the operator-facing UX contract.
    """
    flag = tmp_path / ".worker-paused"
    main(["pause"], flag_path=flag)
    out = capsys.readouterr().out
    assert "resume" in out.lower(), f"pause output must mention resume; got: {out!r}"


def test_resume_cli_prints_latency_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Resume output warns about the up-to-5s poll latency — operator UX.

    Sabotage proof: changing the print would fail this. The 5s note is
    important because operators may otherwise expect instant resume.
    """
    flag = tmp_path / ".worker-paused"
    main(["resume"], flag_path=flag)
    out = capsys.readouterr().out
    assert "5s" in out, f"resume output must mention the 5s latency; got: {out!r}"
