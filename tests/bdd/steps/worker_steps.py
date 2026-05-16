"""Step definitions for worker.feature.

Drives ``kairix.worker_cli.main`` directly through its ``state_path`` /
``flag_path`` injection seams — no env-var monkeypatching, no real
filesystem state outside the per-scenario tmp_path.

Operator-facing language → implementation mapping:
- "status envelope" = the JSON dict written by :func:`worker_state.write_state`
  (round-trip via :func:`worker_state.read_state` + ``to_dict``).
- "phase field" = ``current_phase`` key in the envelope.
- "last_run timestamp" = ``last_embed_run_at`` key (epoch seconds).
- "last_error field" = ``failed_chunks_total`` key (error-volume signal that
  surfaces in :func:`worker_cli.format_status` as "Failed chunks total:").
- "paused flag" = presence/absence of the touch-file at the flag path; the
  worker loop reads this file to decide whether to enter the PAUSED phase.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.worker_cli import main as worker_main
from kairix.worker_cli import status as worker_status
from kairix.worker_state import WorkerPhase, WorkerState, read_state, write_state

pytestmark = pytest.mark.bdd


# ---------------------------------------------------------------------------
# Per-scenario state — paths into a fresh tmp_path, captured stdio
# ---------------------------------------------------------------------------


@pytest.fixture
def _worker_state(tmp_path: Path) -> dict[str, Any]:
    return {
        "state_path": tmp_path / "worker-state.json",
        "flag_path": tmp_path / ".worker-paused",
        "status_envelope": None,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
    }


# ---------------------------------------------------------------------------
# Given — seed the state file (or flag)
# ---------------------------------------------------------------------------


@given(parsers.parse('a worker state file with phase "{phase}" and last_run "{ts}"'))
def _given_state_file_with_phase_and_run(_worker_state: dict[str, Any], phase: str, ts: str) -> None:
    """Write a state file with the given phase + a last_embed_run_at value.

    The Gherkin ``last_run`` is an ISO 8601 timestamp; we store the
    corresponding epoch-seconds float in ``last_embed_run_at`` (the state
    schema's native shape). The literal value matters less than the fact
    that it is non-zero — the status assertion checks the field exists
    and carries a positive timestamp.
    """
    import datetime as _dt

    dt = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    state = WorkerState(
        current_phase=WorkerPhase(phase),
        last_embed_run_at=dt.timestamp(),
        failed_chunks_total=0,
    )
    write_state(state, _worker_state["state_path"])


@given(parsers.parse('a worker state file with phase "{phase}"'))
def _given_state_file_with_phase(_worker_state: dict[str, Any], phase: str) -> None:
    state = WorkerState(current_phase=WorkerPhase(phase))
    write_state(state, _worker_state["state_path"])


@given("a worker state file with paused flag True")
def _given_pause_flag_present(_worker_state: dict[str, Any]) -> None:
    """The pause "flag" is a touch-file alongside the state file (see worker_cli)."""
    flag: Path = _worker_state["flag_path"]
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    # Also seed a state file so concurrent reads don't fall through to the
    # "missing state" branch on platforms that exit early on that.
    write_state(WorkerState(current_phase=WorkerPhase.PAUSED), _worker_state["state_path"])


# ---------------------------------------------------------------------------
# When — invoke worker_cli
# ---------------------------------------------------------------------------


@when("the operator runs worker status")
def _when_run_status(_worker_state: dict[str, Any]) -> None:
    out = io.StringIO()
    err = io.StringIO()
    rc = worker_status(state_path=_worker_state["state_path"], out=out, err=err)
    _worker_state["exit_code"] = rc
    _worker_state["stdout"] = out.getvalue()
    _worker_state["stderr"] = err.getvalue()
    # Read the persisted envelope back via the documented loader so the
    # assertions hit the same dict shape an external monitor would see.
    state = read_state(_worker_state["state_path"])
    _worker_state["status_envelope"] = state.to_dict() if state is not None else None


@when("the operator runs worker pause")
def _when_run_pause(_worker_state: dict[str, Any]) -> None:
    rc = worker_main(["pause"], flag_path=_worker_state["flag_path"])
    _worker_state["exit_code"] = rc


@when("the operator runs worker resume")
def _when_run_resume(_worker_state: dict[str, Any]) -> None:
    rc = worker_main(["resume"], flag_path=_worker_state["flag_path"])
    _worker_state["exit_code"] = rc


# ---------------------------------------------------------------------------
# Then — assertions on envelope + flag-file state
# ---------------------------------------------------------------------------


@then("the status envelope contains a phase field")
def _then_envelope_has_phase(_worker_state: dict[str, Any]) -> None:
    envelope = _worker_state["status_envelope"]
    # Sabotage: drop ``d["current_phase"] = self.current_phase.value`` from
    # WorkerState.to_dict and this key disappears from the envelope.
    assert envelope is not None, "expected a status envelope; state file was missing"
    assert "current_phase" in envelope, f"envelope missing phase field: keys={sorted(envelope)}"
    assert envelope["current_phase"], "phase field present but empty"


@then("the status envelope contains a last_run timestamp")
def _then_envelope_has_last_run(_worker_state: dict[str, Any]) -> None:
    envelope = _worker_state["status_envelope"]
    # Sabotage: drop ``last_embed_run_at`` from the dataclass and the
    # envelope would not carry the timestamp; this assertion fires.
    assert envelope is not None
    assert "last_embed_run_at" in envelope, f"envelope missing last_run timestamp; keys={sorted(envelope)}"
    assert envelope["last_embed_run_at"] > 0, (
        f"expected non-zero last_run timestamp; got {envelope['last_embed_run_at']!r}"
    )


@then("the status envelope contains a last_error field")
def _then_envelope_has_last_error(_worker_state: dict[str, Any]) -> None:
    envelope = _worker_state["status_envelope"]
    # The state schema's error-volume signal is ``failed_chunks_total`` —
    # surfaced in format_status as "Failed chunks total:" and the field an
    # operator reads to spot last-period error spikes.
    # Sabotage: drop ``failed_chunks_total`` from the dataclass and the
    # envelope can't surface error-volume to the operator.
    assert envelope is not None
    assert "failed_chunks_total" in envelope, f"envelope missing last_error field; keys={sorted(envelope)}"


@then("the state file's paused flag is True")
def _then_paused_flag_true(_worker_state: dict[str, Any]) -> None:
    flag: Path = _worker_state["flag_path"]
    # Sabotage: remove the ``path.touch()`` line in worker_cli.pause and
    # the flag never lands on disk; this assertion fires.
    assert flag.exists(), f"expected pause flag at {flag} after 'worker pause'; not present"
    assert _worker_state["exit_code"] == 0, f"pause should exit 0; got {_worker_state['exit_code']}"


@then("the state file's paused flag is False")
def _then_paused_flag_false(_worker_state: dict[str, Any]) -> None:
    flag: Path = _worker_state["flag_path"]
    # Sabotage: replace ``unlink(missing_ok=True)`` in worker_cli.resume with
    # a no-op and the flag stays in place — this assertion catches it.
    assert not flag.exists(), f"expected pause flag at {flag} to be cleared by 'worker resume'; still present"
    assert _worker_state["exit_code"] == 0, f"resume should exit 0; got {_worker_state['exit_code']}"
