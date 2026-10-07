"""
Integration test fixtures — real SQLite database with indexed documents.

Provides session-scoped fixtures that:
  1. Copy reflib_fixture/ and synthetic_agents/ into a temp directory
  2. Create a real SQLite database with the kairix schema
  3. Run DocumentScanner to index all documents
  4. Rebuild the FTS5 index
  5. Yield the database connection and temp path

Usage:
  @pytest.mark.integration
  def test_something(real_db, real_db_path):
      results = bm25_search(query="...", db_path=real_db_path)
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from kairix.core.db.scanner import CollectionConfig, DocumentScanner
from kairix.core.db.schema import create_schema

# Directories containing test data
_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
_REFLIB_FIXTURE_DIR = Path(__file__).parent / "reflib_fixture"
_SYNTHETIC_AGENTS_DIR = _FIXTURES_DIR / "synthetic_agents"


def _populate_fts(db: sqlite3.Connection) -> None:
    """Rebuild the FTS5 index from all active documents."""
    db.execute("DELETE FROM documents_fts")
    db.execute("""
        INSERT INTO documents_fts (rowid, filepath, title, doc)
        SELECT d.id, d.path, d.title, c.doc
        FROM documents d
        JOIN content c ON c.hash = d.hash
        WHERE d.active = 1
        """)
    db.commit()


def _roll_memory_dates_to_today(tmp_root: Path) -> None:
    """Shift date-named memory fixture files so the most recent file lands today.

    The synthetic-agent fixture ships with hardcoded date stems (e.g.
    ``2026-04-28.md``). The briefing source fetcher reads ``today - 7d``
    onwards by literal filename, so as the calendar advances the fixture's
    most recent file falls outside the read window and the integration
    test ``test_briefing_fetches_memory_logs`` flips to FAIL on the day
    that happens.

    This shifts every date-named file in every ``*/memory/`` directory by
    ``today - max(fixture_dates)`` so the relative spacing is preserved
    and the most recent fixture file is always dated today. Non-date
    filenames are left alone.
    """
    today = date.today()
    # Walk every directory under tmp_root; pick up date-named .md files at the
    # top level of each agent-knowledge dir (the v2 flat layout) as well as any
    # legacy ``memory/`` subdirs for fixtures still using the v1 layout.
    candidate_dirs = [tmp_root, *(p for p in tmp_root.rglob("*") if p.is_dir())]
    for memory_dir in candidate_dirs:
        if not memory_dir.is_dir():
            continue
        dated: list[tuple[date, Path]] = []
        for f in memory_dir.glob("*.md"):
            try:
                dated.append((date.fromisoformat(f.stem), f))
            except ValueError:
                continue
        if not dated:
            continue
        latest = max(d for d, _ in dated)
        shift = today - latest
        if shift == timedelta(0):
            continue
        # Two-pass rename via temp suffix to avoid name collisions when the
        # shift produces a date that's already used by another fixture file.
        for _, path in dated:
            path.rename(path.with_suffix(".md.rolling"))
        for original_date, path in dated:
            tmp = path.with_suffix(".md.rolling")
            new_name = (original_date + shift).isoformat() + ".md"
            tmp.rename(path.with_name(new_name))


@pytest.fixture(autouse=True)
def _pre_warm_mcp_state() -> None:
    """Pre-mark kairix as warm so existing integration tests hit the
    production tool path, not the ColdStart envelope (#278).

    A separate test asserts the cold path explicitly — see
    test_mcp_cold_start.py.
    """
    from kairix.platform.warm.state import mark_warm, reset_warm_state

    mark_warm()
    yield
    reset_warm_state()


@pytest.fixture(scope="session")
def _integration_env(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[sqlite3.Connection, Path]:
    """Session-scoped: build temp dir, DB, scan, and FTS index."""
    tmp_root = tmp_path_factory.mktemp("integration_docs")

    # Copy reflib fixture (becomes the reference-library collections)
    if _REFLIB_FIXTURE_DIR.exists():
        for child in _REFLIB_FIXTURE_DIR.iterdir():
            if child.is_dir():
                shutil.copytree(child, tmp_root / child.name)
            elif child.name.endswith(".md"):
                shutil.copy2(child, tmp_root / child.name)

    # Copy synthetic agents (04-Agent-Knowledge tree)
    if _SYNTHETIC_AGENTS_DIR.exists():
        for child in _SYNTHETIC_AGENTS_DIR.iterdir():
            if child.is_dir():
                shutil.copytree(child, tmp_root / child.name)
            elif child.name.endswith(".md"):
                shutil.copy2(child, tmp_root / child.name)

    # Shift date-named memory files so the most recent one is today (see helper).
    _roll_memory_dates_to_today(tmp_root)

    # Create real SQLite database
    db_path = tmp_root / "test_index.sqlite"
    db = sqlite3.connect(str(db_path), timeout=10.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    create_schema(db)

    # Build collection configs from top-level directories
    collections: list[CollectionConfig] = []
    for child in sorted(tmp_root.iterdir()):
        if child.is_dir() and child.name != "__pycache__":
            collections.append(CollectionConfig(name=child.name, path=child.name))

    # Run the scanner
    scanner = DocumentScanner(db, document_root=tmp_root)
    scanner.scan(collections)

    # Rebuild FTS5 index
    _populate_fts(db)

    yield db, tmp_root

    db.close()


@pytest.fixture(scope="session")
def real_db(_integration_env: tuple[sqlite3.Connection, Path]) -> sqlite3.Connection:
    """Session-scoped real SQLite database with indexed documents."""
    return _integration_env[0]


@pytest.fixture(scope="session")
def _integration_paths(
    _integration_env: tuple[sqlite3.Connection, Path],
) -> tuple[Path, Path]:
    """Session-scoped paths: (tmp_root, db_path)."""
    _db, tmp_root = _integration_env
    db_path = tmp_root / "test_index.sqlite"
    return tmp_root, db_path


@pytest.fixture
def real_document_root(_integration_paths: tuple[Path, Path]) -> Path:
    """The session's indexed document root (reflib fixture + synthetic agents).

    Tests pass it (or :func:`real_db_path` / :func:`real_agents_config`)
    explicitly to the unit under test — no ``KAIRIX_*`` env mutation (F2).
    """
    tmp_root, _db_path = _integration_paths
    return tmp_root


@pytest.fixture
def real_db_path(_integration_paths: tuple[Path, Path]) -> Path:
    """Path to the session's real SQLite index — pass as ``db_path=``."""
    _tmp_root, db_path = _integration_paths
    return db_path


@pytest.fixture
def real_agents_config(real_document_root: Path) -> dict[str, object]:
    """Parsed-config dict pointing agent memory discovery at the synthetic agents.

    Explicit ``agents:`` surfaces for the two synthetic agents plus an
    ``agent_defaults`` block so any other agent name resolves under the same
    tmp tree. Passed as ``config=`` to the timeline / briefing readers
    instead of steering them through ``KAIRIX_DOCUMENT_ROOT`` (F2).
    """
    memory_root = real_document_root / "04-Agent-Knowledge"
    return {
        "agents": {
            name: {"surfaces": [{"path": str(memory_root / name), "label": "memory"}]}
            for name in ("agent-alpha", "agent-beta")
        },
        "agent_defaults": {"memory_root": str(memory_root)},
    }
