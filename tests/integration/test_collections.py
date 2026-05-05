"""Tests for multi-collection scanning in the embed pipeline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kairix.core.db.scanner import CollectionConfig, DocumentScanner

pytestmark = pytest.mark.integration


def _create_scanner_schema(db: sqlite3.Connection) -> None:
    """Create the production schema (single source of truth with kairix.core.db.schema)."""
    from kairix.core.db.schema import create_schema

    create_schema(db)


@pytest.fixture()
def multi_collection_dirs(tmp_path: Path) -> dict[str, Path]:
    """Create a document root with two separate collection directories."""
    # Main documents
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "architecture.md").write_text("# Architecture\nService mesh pattern.")
    (docs_dir / "runbook.md").write_text("# Runbook\nRestart sequence.")

    # Agent workspace memories
    ws_dir = tmp_path / "workspaces" / "agent-beta" / "memory"
    ws_dir.mkdir(parents=True)
    (ws_dir / "2026-04-27.md").write_text("# Session Notes\nDeployed kairix v2.")
    (ws_dir / "2026-04-26.md").write_text("# Session Notes\nFixed CI pipeline.")

    return {"root": tmp_path, "docs": docs_dir, "workspaces": tmp_path / "workspaces"}


class TestMultiCollectionScanning:
    """DocumentScanner handles multiple collections."""

    @pytest.mark.integration
    def test_single_collection_scans_root(self, multi_collection_dirs: dict, tmp_path: Path) -> None:
        """Default single-collection scan finds all documents under root."""
        import sqlite3

        db = sqlite3.connect(":memory:")
        _create_scanner_schema(db)
        scanner = DocumentScanner(db, document_root=multi_collection_dirs["root"])
        report = scanner.scan([CollectionConfig(name="default", path=".")])
        assert report.new == 4  # 2 docs + 2 workspace memories

    @pytest.mark.integration
    def test_multi_collection_scans_separately(self, multi_collection_dirs: dict) -> None:
        """Multiple collections scan their own directories."""
        import sqlite3

        db = sqlite3.connect(":memory:")
        _create_scanner_schema(db)
        scanner = DocumentScanner(db, document_root=multi_collection_dirs["root"])
        collections = [
            CollectionConfig(name="docs", path="docs"),
            CollectionConfig(name="workspaces", path="workspaces", glob="**/memory/**/*.md"),
        ]
        report = scanner.scan(collections)
        assert report.new == 4  # 2 + 2

        # Verify collection names are stored
        rows = db.execute("SELECT DISTINCT collection FROM documents").fetchall()
        names = {r[0] for r in rows}
        assert "docs" in names
        assert "workspaces" in names

    @pytest.mark.integration
    def test_empty_collection_returns_zero(self, tmp_path: Path) -> None:
        """A collection pointing to an empty directory returns 0 new."""
        import sqlite3

        db = sqlite3.connect(":memory:")
        _create_scanner_schema(db)
        empty = tmp_path / "empty"
        empty.mkdir()
        scanner = DocumentScanner(db, document_root=tmp_path)
        report = scanner.scan([CollectionConfig(name="empty", path="empty")])
        assert report.new == 0

    @pytest.mark.integration
    def test_fallback_when_no_collections_configured(self, multi_collection_dirs: dict) -> None:
        """When no collections config exists, embed falls back to single default collection."""
        from kairix.core.search.config_loader import parse_collections

        result = parse_collections({})
        assert result is None  # triggers fallback in embed CLI

    @pytest.mark.integration
    def test_workspace_glob_filters_correctly(self, multi_collection_dirs: dict) -> None:
        """Workspace glob only matches files under memory/ subdirectories."""
        # Add a non-memory file to workspaces
        tool_dir = multi_collection_dirs["workspaces"] / "agent-beta" / "tools"
        tool_dir.mkdir(parents=True)
        (tool_dir / "output.md").write_text("# Tool Output\nThis should be excluded.")

        db = sqlite3.connect(":memory:")
        _create_scanner_schema(db)
        scanner = DocumentScanner(db, document_root=multi_collection_dirs["root"])
        report = scanner.scan(
            [
                CollectionConfig(name="workspaces", path="workspaces", glob="**/memory/**/*.md"),
            ]
        )
        # Only 2 memory files, not the tool output
        assert report.new == 2
