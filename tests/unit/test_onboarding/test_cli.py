"""Unit tests for :mod:`kairix.agents.onboarding.cli` (PR 1.4 / #420).

The CLI module is a thin argparse adapter — these tests drive
:func:`cmd_scan` and :func:`cmd_agent` in-process against ready-made
``argparse.Namespace`` objects so the branch coverage for the four
output modes (default report / --json / --yaml) lifts above the F7
per-file floor without paying the subprocess startup cost twice.

The subprocess-driven F30 outcome contract lives in
``tests/integration/test_onboard_cli_outcome.py`` — that's where the
"does the production binary actually work" question gets answered.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
import yaml

from kairix.agents.onboarding import cli as onboarding_cli
from kairix.agents.onboarding.cli import cmd_agent, cmd_scan, main

pytestmark = pytest.mark.unit


def _seed_memory(tmp_path: Path) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir()
    alpha = memory / "agent-alpha"
    alpha.mkdir()
    (alpha / "CLAUDE.md").write_text("# memory\n")
    return memory


def _ns(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "memory_root": "",
        "workspace_root": None,
        "as_json": False,
        "as_yaml": False,
        "name": "",
        "harness": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# Sabotage-proof (executed): removed the default-output branch in
# cmd_scan → the validation-report substring assertion failed because
# stdout was empty; restored.
def test_cmd_scan_default_prints_validation_report(tmp_path: Path) -> None:
    """cmd_scan with no mode flag emits the validation report."""
    memory = _seed_memory(tmp_path)
    args = _ns(memory_root=str(memory))
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_scan(args)
    assert rc == 0
    rendered = out.getvalue()
    assert "agent-alpha" in rendered


# Sabotage-proof (executed): hardcoded the --yaml branch to print "{}"
# → the yaml.safe_load assertion that "agents" was a mapping failed;
# restored.
def test_cmd_scan_yaml_emits_parseable_block(tmp_path: Path) -> None:
    """cmd_scan with --yaml emits a parseable agents: block."""
    memory = _seed_memory(tmp_path)
    args = _ns(memory_root=str(memory), as_yaml=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_scan(args)
    assert rc == 0
    parsed = yaml.safe_load(out.getvalue())
    assert "agents" in parsed
    assert "agent-alpha" in parsed["agents"]


# Sabotage-proof (executed): stripped the --json branch's json.dumps
# call → the json.loads on stdout raised; restored.
def test_cmd_scan_json_emits_envelope(tmp_path: Path) -> None:
    """cmd_scan with --json emits the envelope dict."""
    memory = _seed_memory(tmp_path)
    args = _ns(memory_root=str(memory), as_json=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_scan(args)
    assert rc == 0
    envelope = json.loads(out.getvalue())
    assert envelope["error"] == ""
    assert [a["name"] for a in envelope["agents"]] == ["agent-alpha"]


# Sabotage-proof (executed): forced workspace_root to None even when
# the operator passed it → the "workspace surface" assertion below
# failed; restored.
def test_cmd_scan_workspace_root_kwarg_threaded(tmp_path: Path) -> None:
    """cmd_scan threads --workspace-root through to scan_for_agents."""
    memory = _seed_memory(tmp_path)
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    (workspace / "agent-alpha").mkdir()
    args = _ns(
        memory_root=str(memory),
        workspace_root=str(workspace),
        as_json=True,
    )
    out = io.StringIO()
    with redirect_stdout(out):
        cmd_scan(args)
    envelope = json.loads(out.getvalue())
    labels = {s["label"] for s in envelope["agents"][0]["surfaces"]}
    assert "workspace" in labels


# Sabotage-proof (executed): made cmd_agent return 0 on the
# ValueError branch → the exit code assertion failed; restored.
def test_cmd_agent_unknown_returns_one_and_prints_stderr(tmp_path: Path) -> None:
    """cmd_agent for an unknown agent exits 1 and names the agent on stderr."""
    memory = tmp_path / "memory"
    memory.mkdir()
    args = _ns(memory_root=str(memory), name="ghost")
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cmd_agent(args)
    assert rc == 1
    assert "ghost" in err.getvalue()


# Sabotage-proof (executed): replaced the --json error branch with a
# bare `pass` → stdout was empty so json.loads raised; restored.
def test_cmd_agent_unknown_json_envelope_carries_error(tmp_path: Path) -> None:
    """cmd_agent --json for an unknown agent emits an envelope with
    ``agent: None`` and ``error`` carrying the agent name."""
    memory = tmp_path / "memory"
    memory.mkdir()
    args = _ns(memory_root=str(memory), name="ghost", as_json=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_agent(args)
    assert rc == 1
    envelope = json.loads(out.getvalue())
    assert envelope["agent"] is None
    assert "ghost" in envelope["error"]


# Sabotage-proof (executed): made cmd_agent return the wrong shape
# (a tuple instead of dict in the envelope) → the agent["name"]
# assertion failed; restored.
def test_cmd_agent_json_happy_path_envelope(tmp_path: Path) -> None:
    """cmd_agent --json for a known agent emits a populated envelope."""
    memory = _seed_memory(tmp_path)
    args = _ns(memory_root=str(memory), name="agent-alpha", as_json=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_agent(args)
    assert rc == 0
    envelope = json.loads(out.getvalue())
    assert envelope["error"] == ""
    assert envelope["agent"]["name"] == "agent-alpha"


# Sabotage-proof (executed): hardcoded the --yaml branch to print
# "{}" → the yaml.safe_load assertion that "agents.agent-alpha" was
# a mapping failed; restored.
def test_cmd_agent_yaml_emits_single_block(tmp_path: Path) -> None:
    """cmd_agent --yaml emits a kairix.config.yaml block for one agent."""
    memory = _seed_memory(tmp_path)
    args = _ns(memory_root=str(memory), name="agent-alpha", as_yaml=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_agent(args)
    assert rc == 0
    parsed = yaml.safe_load(out.getvalue())
    assert "agent-alpha" in parsed["agents"]


# Sabotage-proof (executed): made cmd_agent print empty string on
# default mode → the validation-report substring assertion failed;
# restored.
def test_cmd_agent_default_prints_validation_report(tmp_path: Path) -> None:
    """cmd_agent with no mode flag prints the validation report."""
    memory = _seed_memory(tmp_path)
    args = _ns(memory_root=str(memory), name="agent-alpha")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_agent(args)
    assert rc == 0
    assert "agent-alpha" in out.getvalue()


# Sabotage-proof (executed): replaced the scan branch in `main()` with
# a `pass` → the assertion that the return value was 0 failed because
# main fell through to the dead-branch sentinel (2); restored.
def test_main_dispatches_scan(tmp_path: Path) -> None:
    """The standalone main() entry point dispatches scan correctly."""
    memory = _seed_memory(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["scan", "--memory-root", str(memory), "--json"])
    assert rc == 0
    envelope = json.loads(out.getvalue())
    assert "agents" in envelope


# Sabotage-proof (executed): replaced the agent branch in `main()`
# with a `pass` → the assertion below failed because no envelope was
# emitted; restored.
def test_main_dispatches_agent(tmp_path: Path) -> None:
    """The standalone main() entry point dispatches agent correctly."""
    memory = _seed_memory(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["agent", "--name", "agent-alpha", "--memory-root", str(memory), "--json"])
    assert rc == 0
    envelope = json.loads(out.getvalue())
    assert envelope["agent"]["name"] == "agent-alpha"


# Sabotage-proof (executed): added an extra subcommand body without an
# argparse subparser → argparse rejected the unknown subcommand and
# raised SystemExit; restored.
def test_main_missing_subcommand_exits_non_zero(tmp_path: Path) -> None:
    """The standalone main() requires a subcommand."""
    _ = tmp_path
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit):
        # argparse with required=True raises SystemExit on missing
        # subcommand; that's the expected shape.
        main([])


# Sabotage-proof (executed): hardcoded scope_to_envelope to return {}
# → every assertion about envelope shape downstream failed; restored.
def test_scope_to_envelope_carries_every_field(tmp_path: Path) -> None:
    """scope_to_envelope's six fields survive the round trip used by
    every --json / MCP envelope."""
    from kairix.agents.onboarding.scanner import discover_single_agent

    memory = _seed_memory(tmp_path)
    scope = discover_single_agent("agent-alpha", memory_root=memory)
    envelope = onboarding_cli.scope_to_envelope(scope)
    assert set(envelope) == {
        "name",
        "harness",
        "confidence",
        "file_count",
        "most_recent_mtime",
        "surfaces",
    }


def test_module_main_guard_imports_cleanly(tmp_path: Path) -> None:
    """Module-level imports do not have side effects.

    Sabotage: importing onboarding.cli must not write to stdout. If
    a future contributor adds top-level print/log statements this
    test catches the regression.
    """
    import subprocess

    # A fresh interpreter is the only honest "first import" — reloading the
    # module inside the shared test process would swap the module object
    # every other test holds (F1). Executed sabotage: a top-level
    # ``print("x")`` in onboarding/cli.py makes stdout non-empty; restored.
    proc = subprocess.run(
        [sys.executable, "-c", "import kairix.agents.onboarding.cli"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert proc.stderr == ""


# Sabotage-proof (executed): swapped the empty-scope return to `return 0` →
# the rc == 1 assertion failed because empty scan silently returned 0; restored.
def test_cmd_scan_returns_one_on_empty_discovery(tmp_path: Path) -> None:
    """cmd_scan returns exit 1 when no agents are discovered — the
    actionable signal that the memory_root is misconfigured (otherwise
    pipelines could silently produce empty config blocks).
    """
    empty = tmp_path / "empty-memory"
    empty.mkdir()
    args = _ns(memory_root=str(empty))
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_scan(args)
    assert rc == 1
