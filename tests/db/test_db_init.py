"""Tests for kairix.core.db — DB path resolution and open_db."""

from pathlib import Path

import pytest


@pytest.mark.unit
def test_get_db_path_uses_env_override(tmp_path: Path) -> None:
    """KAIRIX_DB_PATH env var takes priority."""
    from kairix.core.db import get_db_path

    db_file = tmp_path / "custom.sqlite"
    db_file.touch()
    assert get_db_path(env={"KAIRIX_DB_PATH": str(db_file)}) == db_file


@pytest.mark.unit
def test_get_db_path_returns_default_when_nothing_exists(tmp_path: Path) -> None:
    """Returns default kairix path when no DB exists anywhere."""
    from kairix.core.db import get_db_path

    result = get_db_path(env={}, home=tmp_path)
    assert str(result).endswith("kairix/index.sqlite")


@pytest.mark.unit
def test_get_db_path_env_override_nonexistent_returns_path(tmp_path: Path) -> None:
    """KAIRIX_DB_PATH returns the path even when the file does not exist yet."""
    from kairix.core.db import get_db_path

    nonexistent = tmp_path / "does_not_exist.sqlite"
    result = get_db_path(env={"KAIRIX_DB_PATH": str(nonexistent)})
    assert result == nonexistent
    assert not result.exists()


@pytest.mark.unit
def test_open_db_returns_working_connection(tmp_path: Path) -> None:
    """open_db() returns a sqlite3 connection with WAL mode."""
    from kairix.core.db import open_db

    db_path = tmp_path / "test.sqlite"
    conn = open_db(db_path)
    try:
        # Verify WAL mode
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        # Verify foreign keys on
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        # Verify it is a working connection
        conn.execute("CREATE TABLE test_t (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO test_t VALUES (1)")
        assert conn.execute("SELECT id FROM test_t").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.unit
def test_open_db_creates_parent_dirs(tmp_path: Path) -> None:
    """open_db() creates parent directories if they do not exist."""
    from kairix.core.db import open_db

    db_path = tmp_path / "deep" / "nested" / "dir" / "test.sqlite"
    conn = open_db(db_path)
    try:
        assert db_path.parent.exists()
    finally:
        conn.close()


# test_open_db_default_path removed: pinned env-coupling. open_db() with
# no path still uses get_db_path()'s env-resolution chain in production;
# that integration is exercised by integration tests of the embed CLI.
# At the unit level, open_db(path=...) is sufficient.


@pytest.mark.unit
def test_embed_vector_dims_constant() -> None:
    """EMBED_VECTOR_DIMS is set to expected value."""
    from kairix.core.db import EMBED_VECTOR_DIMS

    assert EMBED_VECTOR_DIMS == 1536
