"""F86: ``kairix features status --maintenance`` executes its production seam.

``main()``'s ``read_maintenance`` kwarg defaults to
``_default_maintenance_diagnostics`` — the DI-default a real operator runs.
These tests leave that kwarg UNSET (only the unrelated flag-table
``status_provider`` is faked) and point ``--db-path`` at a tmp SQLite
index, so the default seam really opens the database, counts orphans +
soft-deleted rows, reads the (hermetic, absent) worker state and renders
the block.

Sabotage-proof (executed): swapped ``count_current_orphans`` for
``count_pruned_rows`` inside ``_default_maintenance_diagnostics`` —
``test_default_maintenance_seam_reports_live_orphan_counts`` failed
(``current_orphan_count`` 2 != 1). Restored.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kairix.core.features.cli import main
from kairix.core.maintenance import count_current_orphans, count_pruned_rows
from kairix.core.search.config_loader import reset_config_cache
from kairix.paths import clear_cache, db_path

pytestmark = pytest.mark.unit


def _seed_index(path: Path) -> None:
    """One live vector, one orphan vector, two soft-deleted rows."""
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            """
            CREATE TABLE documents (hash TEXT PRIMARY KEY);
            CREATE TABLE content_vectors (hash TEXT, seq INTEGER);
            CREATE TABLE content_vectors_pruned (hash TEXT, seq INTEGER);
            INSERT INTO documents VALUES ('live');
            INSERT INTO content_vectors VALUES ('live', 0), ('orphan', 0);
            INSERT INTO content_vectors_pruned VALUES ('gone-a', 0), ('gone-b', 0);
            """
        )
        db.commit()


def test_default_maintenance_seam_reports_live_orphan_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "index.sqlite"
    _seed_index(db)

    rc = main(["status", "--maintenance", "--db-path", str(db), "--json"], status_provider=lambda: ())

    assert rc == 0
    block = json.loads(capsys.readouterr().out)["maintenance"]
    assert block["current_orphan_count"] == 1
    assert block["pruned_table_size"] == 2
    assert isinstance(block["enabled"], bool)
    assert block["interval_seconds"] > 0


def test_default_maintenance_seam_renders_human_block(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "index.sqlite"
    _seed_index(db)

    assert main(["status", "--maintenance", "--db-path", str(db)], status_provider=lambda: ()) == 0

    out = capsys.readouterr().out
    assert "KFEAT-021 maintenance loop:" in out
    assert "current_orphan_count:     1" in out


def test_default_maintenance_seam_degrades_on_unreadable_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A path that is a directory can't be opened — the block is omitted, no crash."""
    rc = main(["status", "--maintenance", "--db-path", str(tmp_path), "--json"], status_provider=lambda: ())

    assert rc == 0
    assert "maintenance" not in json.loads(capsys.readouterr().out)


def test_default_maintenance_seam_opens_the_platform_index_without_db_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No ``--db-path`` → the seam resolves ``kairix.paths.db_path()``.

    HOME / XDG_DATA_HOME / XDG_CONFIG_HOME and the cwd (not KAIRIX_* —
    F2-clean) point at this test's tmp dir so the fresh index the seam
    opens lands here — never in the session-shared hermetic data dir, and
    never at a ``paths.db_path`` another test's config file declares.
    """
    leaked = [n for n in ("KAIRIX_CONFIG_PATH", "KAIRIX_CONFIG_OVERLAY_PATH") if os.environ.get(n)]
    if leaked:
        # Skip rationale: a developer shell exported a config override (never
        # set in CI, where this seam is enforced); it would pin the db path.
        pytest.skip(f"config override exported in this shell: {', '.join(leaked)}")
    for name, sub in (("HOME", "home"), ("XDG_DATA_HOME", "xdg-data"), ("XDG_CONFIG_HOME", "xdg-config")):
        (tmp_path / sub / "kairix").mkdir(parents=True)
        (tmp_path / sub / ".local" / "share" / "kairix").mkdir(parents=True)
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.chdir(tmp_path)
    clear_cache()
    reset_config_cache()
    try:
        rc = main(["status", "--maintenance", "--json"], status_provider=lambda: ())
        resolved = db_path()
    finally:
        clear_cache()
        reset_config_cache()

    assert rc == 0
    block = json.loads(capsys.readouterr().out)["maintenance"]
    # The seam opened exactly the platform-resolved index (a fresh one in
    # tmp_path in isolation — its counts read 0 before any schema exists).
    assert resolved.exists()
    with closing(sqlite3.connect(resolved)) as db:
        assert block["current_orphan_count"] == count_current_orphans(db)
        assert block["pruned_table_size"] == count_pruned_rows(db)
