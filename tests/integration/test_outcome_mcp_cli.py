"""F30 outcome test — ``kairix mcp`` subprocess surface.

Plan B-parity post-mortem (2026-05-21) identified the gap: unit tests
asserted on internal call shapes; nothing exercised the subprocess
binary surface for each CLI subcommand. This is the MCP CLI's half of
the paydown — the existing unit tests in ``tests/mcp/test_cli_unit.py``
continue to cover the function-call seam (``McpCliDeps`` injection of
fake server/runner); this test adds the F30-required subprocess
outcome assertion.

The MCP CLI's primary surface is ``kairix mcp serve`` — which binds a
socket / launches a uvicorn process and blocks indefinitely. We can't
subprocess that for an outcome test without a teardown story. The F30
brief sanctions ``--help`` / ``--version`` / ``--probe-only`` for
socket-binding CLIs: "F30 wants the subprocess path tested, not
necessarily a complex workflow." This commit takes that route — two
non-blocking subprocess paths that prove the binary surface and the
argparse tree, plus the no-subcommand error path that exercises the
production exit-1 branch:

  (1) ``kairix mcp --help`` → exit 0 with usage on stdout
  (2) ``kairix mcp serve --transport invalid`` → exit 2 from argparse
      with an actionable usage error on stderr (argparse-managed)
  (3) ``kairix mcp`` with no subcommand → exit 1 with help on stdout

The production CLI is unchanged; F30 paydown for this surface is the
outcome test plus the baseline removal.

Sabotage-proof (both executed):
  (a) Mutated ``kairix/agents/mcp/cli.py``'s argparse ``description=``
      from "MCP server: expose search/entity/prep/timeline as MCP tools"
      to "F30-SABOTAGE-MARKER (was: MCP server)" — the help test's
      "expose search/entity/prep/timeline as MCP tools" assertion fails.
      Restored.
  (b) Mutated the no-subcommand branch's ``sys.exit(1)`` to ``sys.exit(0)``
      — the no-subcommand test's ``returncode == 1`` assertion fails.
      Restored.

No wall-clock ceiling is asserted (F82): elapsed time measures the host
scheduler, not kairix behaviour. The subprocess ``timeout=`` is a hang
guard only; the assertions are on exit code + emitted output.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def test_mcp_cli_subprocess_help_outcome() -> None:
    """``kairix mcp --help`` exits 0 with usage on stdout.

    Proves the binary surface boots, the argparse tree is intact, and
    the ``serve`` subcommand is discoverable from the help output. F30
    contract: subprocess + stdout content assertion.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "mcp", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"mcp --help exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    assert "expose search/entity/prep/timeline as MCP tools" in proc.stdout, (
        f"help text missing top-level CLI description: {proc.stdout!r}"
    )
    assert "serve" in proc.stdout, f"help text missing serve subcommand: {proc.stdout!r}"


def test_mcp_cli_subprocess_no_subcommand_exits_one() -> None:
    """``kairix mcp`` with no subcommand prints help and exits 1.

    Asserts on the (stderr or stdout) content the operator sees + the
    non-zero exit code — proves the production no-subcommand branch in
    ``kairix/agents/mcp/cli.py:main`` is wired to ``sys.exit(1)``.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "mcp"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1, (
        f"expected exit 1 for missing subcommand, got {proc.returncode}.\n"
        f"--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    combined = proc.stdout + proc.stderr
    assert "serve" in combined, f"help output (stdout+stderr) missing 'serve': {combined!r}"


def test_mcp_cli_subprocess_invalid_transport_exits_two() -> None:
    """``kairix mcp serve --transport <bogus>`` exits 2 (argparse error).

    Proves the argparse choices guard reaches the production binary.
    Argparse writes the actionable usage block to stderr — assert on
    that content, not just the exit code.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "mcp", "serve", "--transport", "F30-INVALID"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 2, (
        f"expected exit 2 (argparse) for invalid transport, got {proc.returncode}.\n"
        f"--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    assert "--transport" in proc.stderr, f"stderr missing flag name: {proc.stderr!r}"
    assert "invalid choice" in proc.stderr, f"stderr missing argparse error phrase: {proc.stderr!r}"
