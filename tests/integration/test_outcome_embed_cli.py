"""F30 outcome test — ``kairix embed status`` subprocess surface.

Wave 0 Group C paydown for ``kairix/core/embed/cli.py``. The embed
CLI's binary surface has three subcommands (``embed``, ``recall-check``,
``status``, ``rebuild-fts``). ``embed`` itself drives a network-bound
Azure pipeline; ``status`` is the pure read-only surface — it opens
the SQLite index and prints document/vector/pending counts. Operators
run ``kairix embed status`` to confirm the index health before kicking
off a long embed run; this is the F30 outcome test target.

Production change (additive): ``cmd_status`` gains a ``--db-path PATH``
override so the subprocess can drive a tmp SQLite index without
touching ``KAIRIX_DB_PATH`` (F2-clean). Matches the ``--document-root``
convention from ``kairix bootstrap`` and ``kairix store crawl``.

Boundary chain exercised:

  subprocess([kairix, embed, status, --db-path <tmp>])
    → kairix/core/embed/cli.py:main → cmd_status
    → open_db(<tmp>)
    → SELECT counts FROM documents / content_vectors + pending scan
    → print('Kairix index: ...', 'Documents: ...', 'Vectors: ...', 'Pending: ...')

Sabotage-proof anchor: replacing the ``Documents:`` print with a
silent return makes the outcome test fail on the 'Documents:'
assertion. Tested locally.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _seed_empty_index(tmp_path: Path) -> Path:
    """Create the production SQLite schema in a tmp file.

    Uses the real ``create_schema`` — the same path the embed
    pipeline runs at startup. Empty corpus: zero documents, zero
    vectors. Status output is well-defined for the empty case
    (Documents: 0, Vectors: 0, Pending: 0).
    """
    from kairix.core.db.schema import create_schema

    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(str(db_path), timeout=10.0)
    db.execute("PRAGMA journal_mode=WAL")
    create_schema(db)
    db.close()
    return db_path


def test_embed_status_subprocess_renders_index_summary(tmp_path: Path) -> None:
    """Drive ``kairix embed status --db-path <tmp>`` against an empty index;
    assert on the operator-visible status block.

    The status output has a fixed-format header (``Kairix index:
    <path>``) followed by Documents / Vectors / Pending count lines.
    Operators chain this into shell pipelines and dashboards; the
    contract is the line shape, not just exit code.
    """
    db_path = _seed_empty_index(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "embed",
            "status",
            "--db-path",
            str(db_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"embed status exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    assert "Kairix index:" in proc.stdout, f"missing index banner: {proc.stdout!r}"
    assert str(db_path) in proc.stdout, f"banner should name the tmp db_path: {proc.stdout!r}"
    assert "Documents:" in proc.stdout, f"missing Documents: line: {proc.stdout!r}"
    assert "Vectors:" in proc.stdout, f"missing Vectors: line: {proc.stdout!r}"
    assert "Pending:" in proc.stdout, f"missing Pending: line: {proc.stdout!r}"


def test_embed_status_subprocess_errors_on_missing_db(tmp_path: Path) -> None:
    """Pointing at a non-existent db_path → non-zero exit (open_db raises).

    Closes the binary-surface error path for the read-only status
    subcommand. The CLI doesn't print an explicit error envelope —
    the SQLite "unable to open database" message surfaces on stderr
    via the uncaught exception traceback.
    """
    bogus_dir = tmp_path / "no-such-directory"
    bogus_db = bogus_dir / "absent.sqlite"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "embed",
            "status",
            "--db-path",
            str(bogus_db),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode != 0, f"expected non-zero exit for missing db; got {proc.returncode}, stderr={proc.stderr!r}"
    # The SQLite "unable to open" error surfaces in the traceback / stderr.
    # Either operationalerror text appears, or 'no such file/directory',
    # or 'unable to open' — all three are valid SQLite/OS-level signals
    # depending on Python version + libsqlite linkage.
    stderr_lower = proc.stderr.lower()
    assert any(tok in stderr_lower for tok in ("unable to open", "no such file", "operationalerror", "sqlite")), (
        f"expected SQLite/OS open-error in stderr: {proc.stderr!r}"
    )
