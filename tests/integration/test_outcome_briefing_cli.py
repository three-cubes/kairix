"""F30 outcome test — ``kairix brief`` subprocess surface.

Plan B-parity post-mortem (2026-05-21) identified the gap: unit tests
asserted on internal call shapes; nothing exercised the subprocess
binary surface for each CLI subcommand. This is the briefing CLI's
half of the paydown — the existing unit tests in
``tests/briefing/test_cli.py`` continue to cover the function-call
seam (in-process ``deps=BriefDeps(...)`` injection); this test adds
the F30-required subprocess outcome assertion.

PR 1.2 / #420 — the legacy ``--memory-root`` flag has been removed.
The F2-clean subprocess seam is now a ``kairix.config.yaml`` written
to ``tmp_path`` with the subprocess ``cwd`` set there — the
``load_paths_from_config`` loader picks up the cwd-relative config and
the ``agents:`` block drives every brief callsite via AgentScope.
This is NOT ``monkeypatch.setenv`` (F2 targets pytest monkeypatch);
seeding a config file in a tmp dir is the F2-clean subprocess seam.

Boundary chain exercised (config-onboarded custom agent, degraded happy-path):

  subprocess([kairix, brief, agent-alpha], cwd=tmp_path with config)
    → kairix/agents/briefing/cli.py:main
    → kairix.use_cases.brief.run_brief
    → AgentScope resolves agent-alpha from the seeded ``agents:`` block
    → probe_health() → chat=offline (no KAIRIX_LLM_API_KEY in subprocess env)
    → returns BriefOutput(content="", path="", error="") + degraded health
    → CLI: no error branch, no path branch, format_output returns ""
    → exit 0, stderr has the "Generating briefing for agent: ..." line

This is the PLA-265 fix: ``agent-alpha`` is NOT one of the legacy four
names, but an operator who onboarded it via ``agents:`` can brief it.

Boundary chain exercised (no-surface error-path):

  subprocess([kairix, brief, ghost], cwd=tmp_path with `ghost: {surfaces: []}`)
    → AgentScope resolves ghost to a scope with zero surfaces
    → run_brief returns BriefOutput(error="InvalidAgent: ...")
    → CLI prints "Error generating briefing: InvalidAgent..." to stderr
    → exit 1

The error path is independent of LLM availability — agent-surface
resolution runs before any LLM call — so it's the stable F30 anchor.

Sabotage-proof (executed): re-adding the dropped
``if normalised not in {"builder","shape","growth","consultant"}:`` guard
to ``kairix/use_cases/brief.py`` makes ``agent-alpha`` return an
InvalidAgent envelope → the happy-path test fails on ``returncode == 0``
(it gets exit 1 + "Error generating briefing" on stderr). Restored.

No wall-clock ceiling is asserted (F82): elapsed time measures the host
scheduler, not kairix behaviour. The subprocess ``timeout=`` is a hang
guard only; the assertions are on exit code + emitted output.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _seed_minimal_brief_workspace(tmp_path: Path, agent: str) -> None:
    """Seed a ``kairix.config.yaml`` + minimum agent memory dir.

    The brief pipeline's source fetchers tolerate missing files (they
    return empty content for the source), so a bare agent surface is
    enough to drive the CLI's "Generating briefing for agent: ..."
    stderr line without crashing the sources step.
    """
    agent_dir = tmp_path / agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    config_yaml = tmp_path / "kairix.config.yaml"
    config_yaml.write_text(
        textwrap.dedent(f"""\
            agents:
              {agent}:
                surfaces:
                  - path: {agent_dir}
                    label: memory
            """),
        encoding="utf-8",
    )


def _seed_no_surface_agent(tmp_path: Path, agent: str) -> None:
    """Seed a ``kairix.config.yaml`` declaring ``agent`` with no surfaces.

    An explicit empty ``surfaces: []`` entry is the only way an operator
    can produce a scope that genuinely resolves to nothing — the
    InvalidAgent shape that survives PLA-265. Used to drive the
    error-path subprocess test deterministically.
    """
    config_yaml = tmp_path / "kairix.config.yaml"
    config_yaml.write_text(
        textwrap.dedent(f"""\
            agents:
              {agent}:
                surfaces: []
            """),
        encoding="utf-8",
    )


def _subprocess_env_without_llm_keys() -> dict[str, str]:
    """Return a subprocess env that strips LLM credential vars.

    The brief use case's health probe checks ``KAIRIX_LLM_API_KEY`` to
    decide ``chat=offline`` vs ``chat=ok``. We need a deterministic
    "chat offline" path in subprocess so the degraded happy-path test
    doesn't depend on the developer's shell having a real LLM key.

    Building this via ``{**os.environ, ...}`` minus the LLM keys is
    NOT ``monkeypatch.setenv("KAIRIX_*")`` — F2 targets pytest
    monkeypatch on KAIRIX_* keys. Passing an explicit env dict to
    ``subprocess.run`` is the F2-clean way to control a child
    process's environment.
    """
    env = dict(os.environ)
    env.pop("KAIRIX_LLM_API_KEY", None)
    env.pop("KAIRIX_AZURE_API_KEY", None)
    return env


def test_brief_cli_subprocess_configured_agent_outcome(tmp_path: Path) -> None:
    """Drive the real ``kairix brief`` binary for a config-onboarded custom agent.

    ``agent-alpha`` is not one of the legacy four role labels — it only
    works because the seeded ``agents:`` block declares its surface, which
    is exactly the PLA-265 fix. Without an LLM credential the use case
    takes the degraded path (``chat=offline``) and returns an
    empty-content envelope — exit 0, stderr carries the operator-facing
    "Generating briefing for agent: ..." line. Asserts on the stderr
    trace + clean exit, NOT on returncode alone, NOT on internal fake
    call-counts.
    """
    _seed_minimal_brief_workspace(tmp_path, "agent-alpha")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "brief",
            "agent-alpha",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=_subprocess_env_without_llm_keys(),
        cwd=str(tmp_path),
    )

    assert proc.returncode == 0, (
        f"brief exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    assert "Generating briefing for agent: agent-alpha" in proc.stderr, (
        f"missing operator trace in stderr: {proc.stderr!r}"
    )
    # Degraded path: no error message, no write-confirmation, no content body.
    assert "Error generating briefing" not in proc.stderr, f"unexpected error path in stderr: {proc.stderr!r}"


def test_brief_cli_subprocess_no_surface_agent_exits_non_zero(tmp_path: Path) -> None:
    """An agent that resolves to no surface must surface a non-zero exit
    + the structured InvalidAgent error on stderr. Independent of LLM
    availability — agent-surface resolution runs before any LLM call, so
    this test is the F30 stable anchor.
    """
    _seed_no_surface_agent(tmp_path, "ghost")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "brief",
            "ghost",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=_subprocess_env_without_llm_keys(),
        cwd=str(tmp_path),
    )

    assert proc.returncode == 1, f"expected exit 1 for no-surface agent, got {proc.returncode}. stderr={proc.stderr!r}"
    assert "Error generating briefing" in proc.stderr, f"stderr missing error prefix: {proc.stderr!r}"
    assert "InvalidAgent" in proc.stderr, f"stderr missing InvalidAgent class name: {proc.stderr!r}"
