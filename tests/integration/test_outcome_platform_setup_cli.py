"""F30 outcome test — ``kairix setup`` subprocess surface.

Wave 0 paydown (Group E). Closes the F30 gap on
``kairix/platform/setup/cli.py``: existing unit + BDD tests drive
``main()`` / ``run_setup()`` in-process via ``SetupContext`` and
``WizardDeps`` DI seams; this test adds the F30-required subprocess
outcome assertion.

The setup CLI already exposes the seams the subprocess test needs:

  ``--non-interactive``  — skip prompts, use defaults
  ``--json``             — emit the resolved config to stdout as JSON
                           and skip the file-write + embed + health
                           check steps (scripted-bootstrap surface)
  ``--preset PRESET``    — pick the template preset
  ``--path PATH``        — document root override (skips the
                           document-source prompt; matches the F30
                           subprocess seam in
                           ``kairix bootstrap --document-root``)

In JSON + non-interactive mode the wizard emits the assembled
``full_config`` dict to stdout as JSON, with all narrative chatter
diverted to stderr. F2-clean: no ``KAIRIX_*`` env vars are set in the
subprocess invocation; the tmp document root is passed as ``--path``.

Boundary chain exercised:

  subprocess([kairix, setup, --non-interactive, --json,
              --path <tmp>, --preset general])
    → kairix/cli.py dispatch
    → kairix/platform/setup/cli.py:main
    → SetupContext.auto_detect(non_interactive=True, json_mode=True)
    → run_setup (JSON branch) → _emit_json_config
    → JSON to real stdout

Sabotage-proof (executed locally):
    Mutated ``doc_root = os.path.expanduser(document_path)`` in
    ``_resolve_document_root`` to ignore the arg and prompt instead —
    in non-interactive mode the prompt returned the default
    ``~/Documents`` path, the assertion on ``document_root ==
    str(tmp_path)`` failed. Restored after observing the failure.

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


def test_setup_cli_subprocess_json_envelope_outcome(tmp_path: Path) -> None:
    """Drive the real ``kairix setup`` binary surface against a tmp document root.

    Asserts on the JSON config envelope content that scripted-bootstrap
    callers consume — NOT on returncode alone, NOT on internal fake
    call-counts. F30 contract: subprocess + stdout assertion.
    """
    doc_root = tmp_path / "vault"
    doc_root.mkdir()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "setup",
            "--non-interactive",
            "--json",
            "--path",
            str(doc_root),
            "--preset",
            "general",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, (
        f"setup exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    envelope = json.loads(proc.stdout)
    # The CLI threaded our --path argument all the way through to the
    # config envelope's document_root field — this is the F30 seam under
    # test. If the wizard ignored --path and prompted instead, the
    # default would land here instead.
    assert envelope["paths"]["document_root"] == str(doc_root), (
        f"document_root not threaded from --path: {envelope['paths']!r}"
    )
    # Resolved config carries the canonical retrieval block.
    assert "retrieval" in envelope, f"retrieval block missing: {sorted(envelope.keys())}"
    # #474 defect 1: the emitted config must name the provider plugin —
    # a provider-less config fails at factory construction downstream.
    assert envelope.get("provider") == "azure_foundry", f"provider missing/wrong: {sorted(envelope.keys())}"
    # And a non-empty collections block — the wizard's idx=0 default
    # ('all documents') populates this in non-interactive mode.
    assert "collections" in envelope, f"collections block missing: {sorted(envelope.keys())}"
    assert envelope["collections"]["shared"], f"collections.shared empty: {envelope['collections']!r}"


def test_setup_cli_subprocess_exits_non_zero_on_missing_document_root(tmp_path: Path) -> None:
    """Pointing ``--path`` at a non-existent directory must surface a
    non-zero exit. The backend folder scan rejects the missing dir,
    ``run_setup`` returns False, and the CLI exits 1.

    Closes the binary-surface error path the unit tests cover only
    in-process.
    """
    bogus_root = tmp_path / "does-not-exist"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "setup",
            "--non-interactive",
            "--json",
            "--path",
            str(bogus_root),
            "--preset",
            "general",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}. stderr={proc.stderr!r}"
    # The wizard's narrative chatter (including the error line) is
    # diverted to stderr in JSON mode. Combined output is searched so
    # the test stays robust to any future re-routing. The wording is the
    # backend scan's rejection — the same the web wizard shows.
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "not found" in combined.lower(), f"missing folder-not-found diagnostic: {combined!r}"
