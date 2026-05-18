"""CLI binding tests for `kairix warm`.

The Python API is tested in test_runner.py. These tests cover the CLI
shell: exit codes (0 ok, 1 partial failure), text vs JSON output, and
the affordance text in --help.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from unittest import mock

import pytest

from kairix.platform.warm import cli as warm_cli
from kairix.platform.warm.runner import WarmFailure, WarmResult, WarmStep

pytestmark = pytest.mark.unit


def _capture(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = warm_cli.main(argv)
    return rc, buf.getvalue()


def _ok_result() -> WarmResult:
    return WarmResult(
        steps=[
            WarmStep(name="build_search_pipeline", ok=True, duration_s=0.5),
            WarmStep(name="probe_search", ok=True, duration_s=0.1),
            WarmStep(name="open_graph_client", ok=True, duration_s=0.05),
        ],
        ok=True,
        total_duration_s=0.65,
    )


def _partial_failure_result() -> WarmResult:
    return WarmResult(
        steps=[
            WarmStep(name="build_search_pipeline", ok=True, duration_s=0.5),
            WarmStep(name="probe_search", ok=True, duration_s=0.1),
            WarmStep(name="open_graph_client", ok=False, duration_s=0.0, detail="Neo4j unreachable"),
        ],
        failures=[WarmFailure(step="open_graph_client", detail="Neo4j unreachable")],
        ok=False,
        total_duration_s=0.6,
    )


def test_all_steps_ok_exits_zero() -> None:
    with mock.patch.object(warm_cli, "run_warm", return_value=_ok_result()):
        rc, stdout = _capture([])
    assert rc == 0
    assert "warm-up complete" in stdout


def test_partial_failure_exits_one_with_affordance() -> None:
    """A failing step exits 1 and emits the F21 affordance markers."""
    with mock.patch.object(warm_cli, "run_warm", return_value=_partial_failure_result()):
        rc, stdout = _capture([])
    assert rc == 1
    assert "warm-up partial" in stdout
    assert "fix:" in stdout
    assert "next:" in stdout
    assert "Neo4j unreachable" in stdout


def test_json_mode_emits_envelope() -> None:
    with mock.patch.object(warm_cli, "run_warm", return_value=_ok_result()):
        rc, stdout = _capture(["--json"])
    assert rc == 0
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert len(payload["steps"]) == 3
    assert payload["total_duration_s"] == 0.65


def test_help_text_names_mcp_equivalent() -> None:
    """CLI --help must name the MCP equivalent (operational-tests design pattern 3)."""
    parser = warm_cli._build_parser()
    help_text = parser.format_help()
    assert "MCP equivalent:" in help_text
    assert "tool_warm" in help_text


def test_top_level_cli_dispatches_warm() -> None:
    """The top-level `kairix` CLI knows about the 'warm' command."""
    from kairix.cli import COMMANDS

    assert "warm" in COMMANDS
    module_path, fn_name, accepts_args = COMMANDS["warm"]
    assert module_path == "kairix.platform.warm.cli"
    assert fn_name == "main"
    assert accepts_args is True
