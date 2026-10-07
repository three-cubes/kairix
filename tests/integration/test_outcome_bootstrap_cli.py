"""F30 outcome test — ``kairix bootstrap`` subprocess surface.

Plan B-parity post-mortem (2026-05-21) identified the gap: unit tests
asserted on internal call shapes; nothing exercised the subprocess
binary surface for each CLI subcommand. This is the bootstrap CLI's
half of the paydown — the existing unit tests in
``tests/test_bootstrap_cli.py`` continue to cover the function-call
seam (in-process injection via ``deps=BootstrapDeps(...)``); this test
adds the F30-required subprocess outcome assertion.

F2-clean by construction: subprocess is driven via ``--document-root``
(the canonical pattern from ``kairix store crawl --document-root``).
No ``KAIRIX_*`` env vars are set in the subprocess invocation — the
test runs against the production binary's actual CLI surface, with the
tmp vault path passed as an explicit argument.

Boundary chain exercised:

  subprocess([kairix, bootstrap, <agent>, --document-root <tmp>, --json])
    → kairix/bootstrap_cli.py:main
    → BootstrapDeps(document_root_fn=lambda: <tmp>)
    → kairix.use_cases.bootstrap.run_bootstrap
    → board/memory/goals file reads from <tmp>/04-Agent-Knowledge/<agent>/
    → bootstrap_output_to_envelope
    → JSON to stdout

Sabotage-proof anchor: deleting ``Goals.md`` from the seeded vault
makes the envelope's ``active_goals`` field land empty AND the use
case sets ``error`` non-empty → CLI exits 1 → test fails on the
``returncode == 0`` assertion. Tested locally.

No wall-clock ceiling is asserted (F82): elapsed time measures the host
scheduler, not kairix behaviour. The subprocess ``timeout=`` is a hang
guard only; the assertions are on exit code + emitted output.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _seed_minimal_vault(root: Path, agent: str) -> None:
    """Mirror ``tests/test_bootstrap_cli.py:_seed_minimal_vault``.

    The use case reads board / goals / memory under
    ``<root>/04-Agent-Knowledge/<agent>/``. Minimum content for a
    successful envelope: a Board, a Goals file, one dated memory file.
    """
    agent_dir = root / "04-Agent-Knowledge" / agent
    (agent_dir / "memory").mkdir(parents=True, exist_ok=True)
    (agent_dir / "Board.md").write_text("priorities: ship outcome tests", encoding="utf-8")
    (agent_dir / "Goals.md").write_text("- pay down F30 baseline\n- lift codebase standard\n", encoding="utf-8")
    (agent_dir / "memory" / "2026-05-14.md").write_text("today: subprocess outcome test green\n", encoding="utf-8")


def test_bootstrap_cli_subprocess_envelope_outcome(tmp_path: Path) -> None:
    """Drive the real ``kairix bootstrap`` binary surface against a tmp vault.

    Asserts on the JSON envelope content the agent harness consumes —
    NOT on returncode alone, NOT on internal fake call-counts. The
    F30 contract: subprocess + stdout/stderr/envelope assertion.
    """
    _seed_minimal_vault(tmp_path, "agent-alpha")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "bootstrap",
            "agent-alpha",
            "--document-root",
            str(tmp_path),
            "--json",
            "--max-memory-days",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"bootstrap exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    envelope = json.loads(proc.stdout)
    assert envelope["agent"] == "agent-alpha", f"envelope: {envelope}"
    assert envelope["board"].startswith("priorities"), f"board missing: {envelope.get('board')!r}"
    assert envelope["active_goals"], f"active_goals empty: {envelope.get('active_goals')!r}"
    assert envelope["recent_memory"], f"recent_memory empty: {envelope.get('recent_memory')!r}"
    assert "health" in envelope, f"health missing: {sorted(envelope.keys())}"


def test_bootstrap_cli_subprocess_exits_non_zero_on_missing_vault(tmp_path: Path) -> None:
    """Pointing at a non-existent vault must surface a non-zero exit + a
    parseable error message on stderr. Closes the binary-surface error
    path the unit tests cover only in-process."""
    bogus = tmp_path / "does-not-exist"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "bootstrap",
            "agent-alpha",
            "--document-root",
            str(bogus),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}. stderr={proc.stderr!r}"
    assert "kairix bootstrap:" in proc.stderr, f"stderr missing error prefix: {proc.stderr!r}"
