"""
Kairix storage layer — owns the SQLite database and FTS5 index.

Kairix maintains its own
database at ``~/.cache/kairix/index.sqlite`` (configurable via ``KAIRIX_DB_PATH``).

Public API:
  - get_db_path()       — resolve the database file path
  - open_db()           — open a connection with WAL mode
"""

import logging
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

# Environment variable for explicit DB path override
_DB_PATH_ENV = "KAIRIX_DB_PATH"

# Embedding dimensions — configurable via KAIRIX_EMBED_DIMS
EMBED_VECTOR_DIMS = int(os.environ.get("KAIRIX_EMBED_DIMS", "1536"))


def get_db_path(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """
    Resolve the kairix database path.

    Search order:
      1. ``KAIRIX_DB_PATH`` environment variable (explicit override)
      2. ``<home>/.cache/kairix/index.sqlite`` (default kairix location)

    Returns the path (which may not exist yet for fresh installs).

    ``env`` and ``home`` are DI seams; tests pass an explicit mapping +
    home directory rather than monkeypatching the process environment.
    """
    if env is None:
        env = os.environ
    if home is None:
        home = Path.home()

    # 1. Explicit override
    env_path = env.get(_DB_PATH_ENV)
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
        # If explicitly set but doesn't exist, return it anyway — caller
        # will create it (e.g. kairix scan on first run).
        return p

    # 2. Default kairix location
    kairix_db = home / ".cache" / "kairix" / "index.sqlite"
    if kairix_db.exists():
        return kairix_db

    # No DB exists — return the default path for creation
    return kairix_db


def open_db(path: Path | None = None) -> sqlite3.Connection:
    """
    Open (or create) the kairix SQLite database.

    Args:
        path: Explicit path. Defaults to ``get_db_path()``.

    Returns:
        An open ``sqlite3.Connection`` with WAL mode enabled.
    """
    if path is None:
        path = get_db_path()

    # Ensure parent directory exists for fresh installs
    path.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(str(path), timeout=10.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    return db
