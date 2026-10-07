"""F30 outcome test — ``kairix store`` subprocess surface.

Pays down ``kairix/knowledge/store/cli.py`` from the F30 baseline. The
store CLI already exposes ``--document-root`` (it is the canonical
reference for the F30 paydown pattern across the other knowledge CLIs);
this test adds the missing subprocess outcome assertion.

Boundary chain exercised:

  subprocess([kairix, store, crawl, --document-root <tmp>, --dry-run])
    → kairix/knowledge/store/cli.py:main → _cmd_crawl
    → _resolve_document_root (arg wins, no env read)
    → crawler.crawl over the seeded tmp document root
    → _print_crawl_report → stdout
    → exit 0

The error-path test points ``--document-root`` at a non-existent dir
plus omits ``--dry-run``; the crawl auto-degrades to dry-run when
Neo4j is unavailable so the failure manifests as the absence of any
"Document store crawl complete" line plus a non-zero exit when the
caller drops the required arg entirely.

F2-clean: no ``KAIRIX_*`` env mutation in the subprocess invocation.
The tmp document root is passed via the existing ``--document-root``
flag.

Sabotage-proof anchor: mutating ``_resolve_document_root`` to return
``"/nonexistent-sabotage"`` (instead of ``arg``) makes the crawl walk
the wrong tree → the "Document store crawl complete: <tmp>" line
flips to "<sabotage>" and the substring assertion fails. Verified
locally.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _seed_minimal_document_root(root: Path) -> None:
    """Create a minimal document root layout the crawler can walk.

    ``crawler.crawl`` happily walks an empty tree — it returns a
    ``CrawlReport`` with zero entities. The dry-run path doesn't need
    Neo4j. One placeholder file keeps the structure realistic.
    """
    (root / "00-Home").mkdir(parents=True, exist_ok=True)
    (root / "00-Home" / "README.md").write_text("# Home\n", encoding="utf-8")


def test_store_crawl_subprocess_dry_run_outcome(tmp_path: Path) -> None:
    """Drive ``kairix store crawl --document-root <tmp> --dry-run`` end-to-end.

    Asserts on the human-readable summary the operator sees on stdout —
    not on returncode alone, not on internal fake call-counts. The
    F30 contract: subprocess + stdout assertion.
    """
    _seed_minimal_document_root(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "store",
            "crawl",
            "--document-root",
            str(tmp_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"store crawl --dry-run exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )

    assert "Document store crawl complete" in proc.stdout, f"summary line missing from stdout: {proc.stdout!r}"
    assert str(tmp_path) in proc.stdout, f"document root path missing from summary: {proc.stdout!r}"
    assert "Organisations:" in proc.stdout
    assert "Persons:" in proc.stdout
    assert "Outcomes:" in proc.stdout
    assert "Edges:" in proc.stdout


def test_store_health_subprocess_degraded_envelope(tmp_path: Path) -> None:
    """Without Neo4j configured, ``store health --json`` returns a parseable
    envelope marking the graph unavailable and exits non-zero.

    Closes the binary-surface error path the unit tests cover only in-
    process. Asserts on the JSON envelope content the operator (and the
    container healthcheck) consume.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "store",
            "health",
            "--document-root",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1, (
        f"store health expected exit 1 (degraded), got {proc.returncode}. stderr={proc.stderr!r}"
    )

    envelope = json.loads(proc.stdout)
    assert envelope["neo4j_available"] is False, f"expected neo4j_available False, got {envelope!r}"
    assert envelope["ok"] is False, f"expected ok False, got {envelope!r}"
    assert envelope.get("issues"), f"expected non-empty issues, got {envelope!r}"
    assert any("Neo4j unavailable" in issue for issue in envelope["issues"]), (
        f"expected Neo4j-unavailable issue, got: {envelope['issues']!r}"
    )
