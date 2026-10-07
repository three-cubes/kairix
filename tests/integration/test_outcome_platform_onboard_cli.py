"""F30 outcome test — ``kairix onboard`` subprocess surface.

Wave 0 paydown (Group E). Closes the F30 gap on
``kairix/platform/onboard/cli.py``: existing unit tests in
``tests/onboard/test_cli.py`` drive ``main()`` in-process via the
``pkg_root`` / ``document_root_override_fn`` DI seams; this test adds
the F30-required subprocess outcome assertion.

Subcommand targeted: ``onboard guide`` — it already exposes
``--document-root`` and gains ``--guide-src`` here so the subprocess
binary can be driven against a tmp-path placeholder without monkey-
patching ``kairix.__file__`` and without setting any ``KAIRIX_*`` env
vars (F2-clean by construction).

Boundary chain exercised:

  subprocess([kairix, onboard, guide,
              --document-root <tmp>,
              --guide-src <tmp>/guide.md,
              --dry-run])
    → kairix/cli.py dispatch
    → kairix/platform/onboard/cli.py:main
    → cmd_guide → _resolve_doc_root + _resolve_guide_src
    → dry-run path → stdout banner ("Would install...")

Sabotage-proof (executed locally):
    Mutated ``if explicit:`` in ``_resolve_guide_src`` to ``if False:``
    — the CLI then falls through to the installed-package probe, which
    in this worktree resolves to ``<repo>/kairix/docs/agent-usage-guide.md``
    (does not exist) → error message + exit 1 → happy-path test fails
    on ``returncode == 0`` and on the missing "Source: <tmp>" line.
    Restored after observing the failure.

No wall-clock ceiling is asserted (F82): elapsed time measures the host
scheduler, not kairix behaviour. The subprocess ``timeout=`` is a hang
guard only; the assertions are on exit code + emitted output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _seed_guide_source(tmp_path: Path) -> Path:
    """Write a minimal markdown placeholder the CLI accepts as the guide source.

    The CLI's ``_resolve_guide_src`` only checks ``Path.exists()`` —
    content is copied verbatim to the destination on a non-dry-run.
    The dry-run path doesn't read it; existence alone is the gate.
    """
    guide = tmp_path / "agent-usage-guide.md"
    guide.write_text("# agent usage guide placeholder\n", encoding="utf-8")
    return guide


def test_onboard_guide_subprocess_dry_run_emits_source_and_dest(tmp_path: Path) -> None:
    """Drive the real ``kairix onboard guide`` binary surface against a tmp vault.

    Asserts on the dry-run banner content the operator consumes —
    NOT on returncode alone, NOT on internal fake call-counts. F30
    contract: subprocess + stdout assertion.
    """
    guide = _seed_guide_source(tmp_path)
    doc_root = tmp_path / "vault"
    doc_root.mkdir()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "onboard",
            "guide",
            "--document-root",
            str(doc_root),
            "--guide-src",
            str(guide),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"onboard guide exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    stdout = proc.stdout
    assert "Would install agent usage guide" in stdout, f"banner missing: {stdout!r}"
    assert f"Source: {guide}" in stdout, f"source line missing: {stdout!r}"
    assert "Dest:" in stdout, f"dest line missing: {stdout!r}"
    # Destination should be inside the tmp document root, not the host's real vault.
    assert str(doc_root) in stdout, f"dest does not land under tmp doc_root: {stdout!r}"


def test_onboard_guide_subprocess_resolves_bundled_source_without_guide_src(tmp_path: Path) -> None:
    """Drive ``kairix onboard guide`` with NO ``--guide-src`` — the bundled
    package-data guide (#466) must resolve as the source out-of-box.

    Before #466 the source resolution fell through ``docs/`` lookups that
    the image never carried, so ``onboard guide`` could not find a source
    inside the container. The bundled copy under
    ``kairix/agents/usage_guide/data/`` now resolves from the installed
    package, so this dry-run succeeds with no operator-supplied source.

    Sabotage proof (executed locally): point ``_GUIDE_RESOURCE`` in
    ``kairix/use_cases/usage_guide.py`` at a non-existent ``data/`` file —
    ``_bundled_guide_path`` returns None, ``_resolve_guide_src`` falls
    through to the ``docs/`` stub/missing path and (in the installed
    layout) prints the not-found error + exits 1, so ``returncode == 0``
    fails. Restored.
    """
    doc_root = tmp_path / "vault"
    doc_root.mkdir()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "onboard",
            "guide",
            "--document-root",
            str(doc_root),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"onboard guide (bundled source) exited {proc.returncode}\n"
        f"--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    stdout = proc.stdout
    assert "Would install agent usage guide" in stdout, f"banner missing: {stdout!r}"
    # The resolved source must be the bundled package-data copy.
    assert "agents/usage_guide/data/agent-usage-guide.md" in stdout, (
        f"source line did not resolve to the bundled guide: {stdout!r}"
    )


def test_onboard_guide_subprocess_exits_non_zero_on_missing_document_root(tmp_path: Path) -> None:
    """Pointing ``--document-root`` at a non-existent directory must
    surface a non-zero exit + an operator-actionable error message on
    stderr. Closes the binary-surface error path the unit tests cover
    only in-process."""
    guide = _seed_guide_source(tmp_path)
    bogus_root = tmp_path / "does-not-exist"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "onboard",
            "guide",
            "--document-root",
            str(bogus_root),
            "--guide-src",
            str(guide),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}. stderr={proc.stderr!r}"
    assert "document root does not exist" in proc.stderr.lower(), f"stderr missing error message: {proc.stderr!r}"
