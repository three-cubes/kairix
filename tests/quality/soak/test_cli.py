"""CLI binding tests for `kairix soak run`.

The Python API is tested directly in test_runner.py. These tests cover
the CLI shell: argument parsing, exit-code semantics, text vs JSON
output, and that the help text carries the MCP-equivalent affordance
required by the operational-tests design.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest import mock

import pytest

from kairix.quality.soak import cli as soak_cli
from kairix.quality.soak.runner import SoakFailure, SoakIteration, SoakResult

pytestmark = pytest.mark.unit


def _fake_workload(payload: dict[str, Any]) -> Any:
    def runner(_suite: str) -> dict[str, Any]:
        return payload

    return runner


def _capture(argv: list[str]) -> tuple[int, str, str]:
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = soak_cli.main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


def _pass_result() -> SoakResult:
    return SoakResult(
        suite="reflib",
        repeat=2,
        iterations=[
            SoakIteration(index=0, duration_s=1.0, memory_mb=1.0, stderr_bytes=10, fd_count=20, signature="abc"),
            SoakIteration(index=1, duration_s=1.05, memory_mb=0.5, stderr_bytes=10, fd_count=20, signature="abc"),
        ],
        passed=True,
    )


def _fail_result() -> SoakResult:
    return SoakResult(
        suite="reflib",
        repeat=2,
        iterations=[
            SoakIteration(index=0, duration_s=1.0, memory_mb=1.0, stderr_bytes=10, fd_count=20, signature="abc"),
            SoakIteration(index=1, duration_s=1.05, memory_mb=0.5, stderr_bytes=10, fd_count=20, signature="abc"),
        ],
        failures=[
            SoakFailure(kind="log_volume", iteration=None, detail="total stderr 7.0 MB exceeds cap 5.0 MB"),
        ],
        passed=False,
    )


def _error_result() -> SoakResult:
    return SoakResult(
        suite="reflib",
        repeat=3,
        iterations=[],
        passed=False,
        error="RuntimeError: workload exploded",
    )


# ---------------------------------------------------------------------------
# Exit codes — 0 pass, 1 fail, 2 indeterminate
# ---------------------------------------------------------------------------


def test_pass_exits_zero() -> None:
    with mock.patch.object(soak_cli, "run_soak", return_value=_pass_result()):
        rc, stdout, _stderr = _capture(["run", "--suite", "reflib", "--repeat", "2"])
    assert rc == 0
    assert "PASS" in stdout


def test_failure_exits_one() -> None:
    with mock.patch.object(soak_cli, "run_soak", return_value=_fail_result()):
        rc, stdout, _ = _capture(["run", "--suite", "reflib", "--repeat", "2"])
    assert rc == 1
    assert "FAIL" in stdout
    assert "log_volume" in stdout


def test_top_level_error_exits_two() -> None:
    with mock.patch.object(soak_cli, "run_soak", return_value=_error_result()):
        rc, stdout, _ = _capture(["run", "--suite", "reflib", "--repeat", "3"])
    assert rc == 2
    assert "RuntimeError" in stdout


def test_repeat_lt_2_exits_two_with_affordance() -> None:
    rc, _stdout, stderr = _capture(["run", "--suite", "reflib", "--repeat", "1"])
    assert rc == 2
    assert "repeat must be >= 2" in stderr
    assert "fix:" in stderr


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------


def test_json_mode_emits_envelope_on_stdout() -> None:
    with mock.patch.object(soak_cli, "run_soak", return_value=_pass_result()):
        rc, stdout, _ = _capture(["run", "--suite", "reflib", "--repeat", "2", "--json"])
    assert rc == 0
    payload = json.loads(stdout)
    assert payload["suite"] == "reflib"
    assert payload["passed"] is True
    assert payload["repeat"] == 2
    assert len(payload["iterations"]) == 2


def test_text_mode_lists_each_iteration() -> None:
    with mock.patch.object(soak_cli, "run_soak", return_value=_pass_result()):
        rc, stdout, _ = _capture(["run", "--suite", "reflib", "--repeat", "2"])
    assert rc == 0
    assert "iter 0:" in stdout
    assert "iter 1:" in stdout


def test_text_mode_failure_includes_next_action() -> None:
    """The F21 affordance rule: every failure surface emits fix: + next: markers."""
    with mock.patch.object(soak_cli, "run_soak", return_value=_fail_result()):
        rc, stdout, _ = _capture(["run", "--suite", "reflib", "--repeat", "2"])
    assert rc == 1
    assert "fix:" in stdout
    assert "next:" in stdout


# ---------------------------------------------------------------------------
# Affordance — CLI help mentions MCP equivalent
# ---------------------------------------------------------------------------


def test_help_text_names_mcp_equivalent() -> None:
    """The CLI --help must surface the MCP binding (or its deliberate absence).

    Sabotage-proof: remove the 'MCP equivalent:' block from _HELP_DESCRIPTION
    and this test fails because operators can no longer learn which surface
    serves which use case from `kairix soak run --help`.
    """
    parser = soak_cli._build_parser()
    help_text = parser.format_help()
    assert "MCP equivalent:" in help_text, "CLI --help must name the MCP equivalent or its absence"
    assert "tool_soak_run" in help_text, "help should point operators at the MCP stub for agent escalation"


# ---------------------------------------------------------------------------
# Wiring — `kairix.cli` dispatch lists 'soak'
# ---------------------------------------------------------------------------


def test_top_level_cli_dispatches_soak() -> None:
    """The top-level kairix CLI knows about the 'soak' command.

    Sabotage-proof: remove the 'soak' entry from COMMANDS and this fails.
    """
    from kairix.cli import COMMANDS

    assert "soak" in COMMANDS, "top-level CLI must dispatch 'soak' to the soak.cli module"
    module_path, fn_name, accepts_args = COMMANDS["soak"]
    assert module_path == "kairix.quality.soak.cli"
    assert fn_name == "main"
    assert accepts_args is True
