"""Unit tests for :mod:`kairix.agents.onboarding.doctor_cli` (PR 1.5 / #420).

The doctor_cli module is a thin argparse adapter — these tests drive
:func:`cmd_doctor` in-process against ready-made ``argparse.Namespace``
objects so the branch coverage for the output modes (default report /
--json / --yaml) and the bulk/single dispatch lifts above the F7
per-file floor without paying the subprocess startup cost twice.

The subprocess-driven F30 outcome contract lives in
``tests/integration/test_doctor_cli_outcome.py``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped] — PyYAML ships without type stubs upstream

from kairix.agents.onboarding.doctor_cli import (
    agent_health_to_envelope,
    cmd_doctor,
    main,
)

pytestmark = pytest.mark.unit


def _write_config(tmp_path: Path, *, populate: bool = True, name: str = "agent-alpha") -> Path:
    surface = tmp_path / name
    if populate:
        surface.mkdir()
        (surface / "note.md").write_text("# note\n")
    cfg = tmp_path / "kairix.config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    name: {
                        "harness": "claude-code",
                        "surfaces": [
                            {"path": str(surface), "glob": "**/*.md", "label": "memory"},
                        ],
                    },
                },
            },
        ),
    )
    return cfg


def _ns(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "config": "",
        "name": None,
        "all": False,
        "as_json": False,
        "as_yaml": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# Sabotage-proof (executed): removed the default-output branch in
# cmd_doctor → the human-summary substring assertion failed because
# stdout was empty; restored.
def test_cmd_doctor_default_prints_human_summary(tmp_path: Path) -> None:
    """cmd_doctor with no mode flag emits the human summary."""
    cfg = _write_config(tmp_path)
    args = _ns(config=str(cfg), all=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    rendered = out.getvalue()
    assert "agent-alpha" in rendered


# Sabotage-proof (executed): stripped the --json branch's json.dumps
# call → the json.loads on stdout raised; restored.
def test_cmd_doctor_json_emits_envelope(tmp_path: Path) -> None:
    """cmd_doctor --all --json emits the bulk envelope dict."""
    cfg = _write_config(tmp_path)
    args = _ns(config=str(cfg), all=True, as_json=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    envelope = json.loads(out.getvalue())
    assert "agents" in envelope
    assert envelope["overall"] == "ok"


# Sabotage-proof (executed): hardcoded the --yaml branch to print "{}"
# → the yaml.safe_load assertion that the agent block was a mapping
# failed; restored.
def test_cmd_doctor_yaml_emits_parseable_block(tmp_path: Path) -> None:
    """cmd_doctor --all --yaml emits a parseable agents: block."""
    cfg = _write_config(tmp_path)
    args = _ns(config=str(cfg), all=True, as_yaml=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    parsed = yaml.safe_load(out.getvalue())
    assert "agents" in parsed


# Sabotage-proof (executed): made the single-agent JSON branch always
# return the bulk envelope → the agent["name"] assertion failed because
# the key was "agents" not "agent"; restored.
def test_cmd_doctor_single_agent_json_envelope(tmp_path: Path) -> None:
    """cmd_doctor --name X --json emits a single-agent envelope."""
    cfg = _write_config(tmp_path)
    args = _ns(config=str(cfg), name="agent-alpha", as_json=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    envelope = json.loads(out.getvalue())
    assert envelope["agent"]["name"] == "agent-alpha"


# Sabotage-proof (executed): made the missing-surface branch return 0
# → the assertion that rc == 1 failed; restored.
def test_cmd_doctor_missing_surface_exits_non_zero(tmp_path: Path) -> None:
    """A configured surface that doesn't exist on disk → exit 1."""
    cfg = _write_config(tmp_path, populate=False)
    args = _ns(config=str(cfg), all=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 1
    rendered = out.getvalue()
    assert "path missing" in rendered


# Sabotage-proof (executed): made cmd_doctor without --all/--name
# return rc=2 → the default-to-all assertion failed; restored.
def test_cmd_doctor_no_flag_defaults_to_all(tmp_path: Path) -> None:
    """When neither --all nor --name is set, cmd_doctor runs --all."""
    cfg = _write_config(tmp_path)
    args = _ns(config=str(cfg))
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    assert "agent-alpha" in out.getvalue()


# Sabotage-proof (executed): made cmd_doctor with no --config and no
# inline config raise → the graceful-error assertion failed; restored.
def test_cmd_doctor_no_config_runs_against_empty(tmp_path: Path) -> None:
    """When --config is not set, cmd_doctor runs against an empty
    config and reports overall=ok with zero agents."""
    _ = tmp_path
    args = _ns(all=True, as_json=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    envelope = json.loads(out.getvalue())
    assert envelope["agents"] == []


# Sabotage-proof (executed): replaced the agent_health_to_envelope
# field set with {} → every downstream assertion about envelope shape
# failed; restored.
def test_agent_health_to_envelope_carries_every_field(tmp_path: Path) -> None:
    """agent_health_to_envelope's load-bearing fields survive the
    round trip used by every --json / MCP envelope."""
    from kairix.agents.onboarding.doctor import doctor_check_agent

    cfg = _write_config(tmp_path)
    config = yaml.safe_load(cfg.read_text())
    health = doctor_check_agent("agent-alpha", config=config)
    envelope = agent_health_to_envelope(health)
    assert set(envelope) == {
        "name",
        "harness",
        "surfaces",
        "overall",
        "issues",
    }
    assert envelope["name"] == "agent-alpha"


# Sabotage-proof (executed): replaced the main() doctor branch with
# `pass` → the assertion that the return value was 0 failed;
# restored.
def test_main_dispatches_doctor(tmp_path: Path) -> None:
    """The standalone main() entry point dispatches doctor correctly."""
    cfg = _write_config(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["agent", "--all", "--json", "--config", str(cfg)])
    assert rc == 0
    envelope = json.loads(out.getvalue())
    assert "agents" in envelope


# Sabotage-proof (executed): added an extra subcommand body without an
# argparse subparser → argparse rejected the unknown subcommand and
# raised SystemExit; restored.
def test_main_missing_subcommand_exits_non_zero(tmp_path: Path) -> None:
    """The standalone main() requires a subcommand."""
    _ = tmp_path
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit):
        main([])


# Sabotage-proof (executed): made the single-agent --yaml branch
# print "" → yaml.safe_load returned None and the dict-key assertion
# failed; restored.
def test_cmd_doctor_single_agent_yaml_emits_block(tmp_path: Path) -> None:
    """cmd_doctor --name X --yaml emits a parseable agent block."""
    cfg = _write_config(tmp_path)
    args = _ns(config=str(cfg), name="agent-alpha", as_yaml=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    parsed = yaml.safe_load(out.getvalue())
    assert parsed["agent"]["name"] == "agent-alpha"


# Sabotage-proof (executed): removed the single-agent default-mode
# branch → no stdout was emitted; assertion failed; restored.
def test_cmd_doctor_single_agent_default_text(tmp_path: Path) -> None:
    """cmd_doctor --name X with no mode flag emits text summary."""
    cfg = _write_config(tmp_path)
    args = _ns(config=str(cfg), name="agent-alpha")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    rendered = out.getvalue()
    assert "agent-alpha" in rendered
    assert "1 agent checked" in rendered


# Sabotage-proof (executed): made _load_config_from_path skip the
# is_file() False branch → the assertion that a missing path produces
# an empty-agents report failed; restored.
def test_cmd_doctor_missing_config_path_runs_empty(tmp_path: Path) -> None:
    """A --config path that does not exist on disk is treated as no
    config — doctor reports zero agents."""
    args = _ns(config=str(tmp_path / "no-such-file.yaml"), all=True, as_json=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    envelope = json.loads(out.getvalue())
    assert envelope["agents"] == []


# Sabotage-proof (executed): removed the yaml.YAMLError except → a
# malformed config file raised; restored to the swallow-and-empty
# path.
def test_cmd_doctor_malformed_yaml_config_runs_empty(tmp_path: Path) -> None:
    """A --config path with malformed YAML is treated as no config —
    doctor reports zero agents rather than crashing."""
    cfg = tmp_path / "broken.yaml"
    cfg.write_text("key: : : : invalid yaml content")
    args = _ns(config=str(cfg), all=True, as_json=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    envelope = json.loads(out.getvalue())
    assert envelope["agents"] == []


# Sabotage-proof (executed): removed the `isinstance(loaded, dict)`
# guard → a yaml file containing a bare list raised AttributeError on
# the agents lookup; restored.
def test_cmd_doctor_non_mapping_yaml_runs_empty(tmp_path: Path) -> None:
    """A --config path with a non-mapping yaml body (e.g. a bare list)
    is treated as no config."""
    cfg = tmp_path / "list.yaml"
    cfg.write_text("- just\n- a\n- list\n")
    args = _ns(config=str(cfg), all=True, as_json=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    envelope = json.loads(out.getvalue())
    assert envelope["agents"] == []


# Sabotage-proof (executed): hardcoded the "(no agents configured)"
# branch to fall through to the empty stdout path → the substring
# assertion failed; restored.
def test_cmd_doctor_empty_config_renders_no_agents_line(tmp_path: Path) -> None:
    """Default mode with zero agents prints the "(no agents configured)"
    placeholder line so operators see *something*."""
    cfg = tmp_path / "empty.yaml"
    cfg.write_text("agents: {}\n")
    args = _ns(config=str(cfg), all=True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    rendered = out.getvalue()
    assert "(no agents configured)" in rendered


# Sabotage-proof (executed): dropped the agent-level issues rendering
# in _render_text_report → the fallback-warning substring failed;
# restored.
def test_cmd_doctor_renders_agent_level_issues(tmp_path: Path) -> None:
    """Agent-level issues (e.g. fallback-to-defaults) render in the
    default text output with the leading `!` marker."""
    # Defaults synthesis → agent-level issue
    memory_root = tmp_path / "memory"
    (memory_root / "agent-alpha").mkdir(parents=True)
    (memory_root / "agent-alpha" / "note.md").write_text("# n\n")
    cfg = tmp_path / "fallback.yaml"
    cfg.write_text(
        yaml.safe_dump({"agent_defaults": {"memory_root": str(memory_root), "glob": "**/*.md"}}),
    )
    args = _ns(config=str(cfg), name="agent-alpha")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_doctor(args)
    assert rc == 0
    rendered = out.getvalue()
    assert "no explicit config" in rendered


def test_module_main_guard_imports_cleanly(tmp_path: Path) -> None:
    """Module-level imports do not have side effects.

    Sabotage: importing doctor_cli must not write to stdout. If a
    future contributor adds top-level print/log statements this test
    catches the regression.
    """
    import subprocess

    # A fresh interpreter is the only honest "first import" — reloading the
    # module inside the shared test process would swap the module object
    # every other test holds (F1). Executed sabotage: a top-level
    # ``print("x")`` in onboarding/doctor_cli.py makes stdout non-empty;
    # restored.
    proc = subprocess.run(
        [sys.executable, "-c", "import kairix.agents.onboarding.doctor_cli"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert proc.stderr == ""


# ---------------------------------------------------------------------------
# Write-access probe surfacing (PLA-259)
# ---------------------------------------------------------------------------


def test_surface_envelope_carries_writable(tmp_path: Path) -> None:
    """The per-surface envelope exposes the ``writable`` probe so the --json /
    MCP surface carries write-access state alongside the disk-state fields
    (PLA-259).

    Sabotage-proof (executed): dropped the ``"writable"`` key from
    ``_surface_health_to_envelope`` → the key-presence assertion failed;
    restored.
    """
    from kairix.agents.onboarding.doctor import doctor_check_agent

    cfg = _write_config(tmp_path)
    config = yaml.safe_load(cfg.read_text())
    health = doctor_check_agent("agent-alpha", config=config)

    envelope = agent_health_to_envelope(health)
    assert envelope["surfaces"], "expected at least one surface in the envelope"
    surface = envelope["surfaces"][0]
    assert "writable" in surface
    assert surface["writable"] is True


def test_cmd_doctor_flags_unwritable_surface_with_actionable_error(tmp_path: Path) -> None:
    """``kairix doctor agent`` (direct handler, F30 outcome) exits 1 and prints
    an actionable not-writable verdict when a configured surface exists but
    kairix cannot write to it (PLA-259). Skips on hosts where mode bits do not
    block writes (e.g. CI run as root).

    Sabotage-proof (executed): removed the not-writable branch from
    ``_rollup_overall`` → overall stayed warn, rc was 0, and the rc==1
    assertion failed; restored.
    """
    if os.geteuid() == 0:
        pytest.skip("permission denial cannot be simulated as root (touch succeeds despite 0o500)")
    cfg = _write_config(tmp_path)
    surface = tmp_path / "agent-alpha"  # the dir _write_config populated
    surface.chmod(0o500)
    try:
        args = _ns(config=str(cfg), all=True)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cmd_doctor(args)
        rendered = out.getvalue()
        if "writable=False" not in rendered:
            pytest.skip("filesystem ignores mode bits (write probe succeeded despite 0o500)")
        assert rc == 1
        assert "not writable" in rendered
        assert "fix:" in rendered and "next:" in rendered
    finally:
        surface.chmod(0o700)
