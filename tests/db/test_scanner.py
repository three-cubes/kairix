"""Tests for kairix.core.db.scanner — document store file discovery, hashing, and ingestion."""

import sqlite3

import pytest

from kairix.core.db.scanner import (
    CollectionConfig,
    DocumentScanner,
    ScanReport,
)
from kairix.core.db.schema import create_schema
from kairix.knowledge.reflib.dedup import hash_content
from kairix.knowledge.reflib.frontmatter import extract_title


def _setup_schema(db: sqlite3.Connection) -> None:
    """Build the production schema in a fresh in-memory DB.

    Replaces the previous per-test hand-crafted CREATE TABLE so the test
    schema is always a single source of truth with production. New columns
    (e.g. ``agent_owner`` for #114) flow through automatically.
    """
    create_schema(db)


@pytest.mark.unit
def test_hash_content_deterministic() -> None:
    """Same content produces same hash."""
    assert hash_content("hello world") == hash_content("hello world")


@pytest.mark.unit
def test_hash_content_different_for_different_text() -> None:
    """Different content produces different hashes."""
    assert hash_content("hello") != hash_content("world")


@pytest.mark.unit
def test_extract_title_from_frontmatter() -> None:
    """Extracts title from YAML frontmatter."""
    text = "---\ntitle: My Document\ntype: note\n---\n\nBody text here."
    assert extract_title(text, __import__("pathlib").Path("test.md")) == "My Document"


@pytest.mark.unit
def test_extract_title_from_heading() -> None:
    """Falls back to first # heading when no frontmatter title."""
    text = "# Hello World\n\nSome content."
    assert extract_title(text, __import__("pathlib").Path("test.md")) == "Hello World"


@pytest.mark.unit
def test_extract_title_from_filename() -> None:
    """Falls back to filename when no frontmatter or heading."""
    text = "Just plain text with no heading."
    assert extract_title(text, __import__("pathlib").Path("my-document.md")) == "My Document"


@pytest.mark.unit
def test_scan_discovers_new_files(tmp_path: __import__("pathlib").Path) -> None:
    """Scanner discovers new markdown files and inserts them."""
    vault = tmp_path / "vault"
    area = vault / "02-Areas"
    area.mkdir(parents=True)

    (area / "doc1.md").write_text("# Document One\n\nContent of doc one.")
    (area / "doc2.md").write_text("---\ntitle: Doc Two\n---\n\nContent of doc two.")

    db = sqlite3.connect(":memory:")
    _setup_schema(db)

    scanner = DocumentScanner(db, vault)
    report = scanner.scan([CollectionConfig(name="vault-areas", path="02-Areas")])

    assert report.new == 2
    assert report.unchanged == 0
    assert report.removed == 0

    docs = db.execute("SELECT path, title FROM documents ORDER BY path").fetchall()
    assert len(docs) == 2
    assert docs[0][1] == "Document One"
    assert docs[1][1] == "Doc Two"


@pytest.mark.unit
def test_scan_detects_unchanged_files(tmp_path: __import__("pathlib").Path) -> None:
    """Unchanged files are not re-inserted."""
    vault = tmp_path / "vault"
    area = vault / "02-Areas"
    area.mkdir(parents=True)
    (area / "doc.md").write_text("# Stable\n\nUnchanged content.")

    db = sqlite3.connect(":memory:")
    _setup_schema(db)

    scanner = DocumentScanner(db, vault)
    r1 = scanner.scan([CollectionConfig(name="test", path="02-Areas")])
    assert r1.new == 1

    r2 = scanner.scan([CollectionConfig(name="test", path="02-Areas")])
    assert r2.unchanged == 1
    assert r2.new == 0


@pytest.mark.unit
def test_scan_detects_updated_files(tmp_path: __import__("pathlib").Path) -> None:
    """Modified files are detected by hash change."""
    vault = tmp_path / "vault"
    area = vault / "02-Areas"
    area.mkdir(parents=True)
    doc = area / "doc.md"
    doc.write_text("# Version 1\n\nOriginal.")

    db = sqlite3.connect(":memory:")
    _setup_schema(db)

    scanner = DocumentScanner(db, vault)
    scanner.scan([CollectionConfig(name="test", path="02-Areas")])

    doc.write_text("# Version 2\n\nUpdated.")
    r2 = scanner.scan([CollectionConfig(name="test", path="02-Areas")])
    assert r2.updated == 1
    assert r2.new == 0


@pytest.mark.unit
def test_scan_marks_removed_files_inactive(
    tmp_path: __import__("pathlib").Path,
) -> None:
    """Deleted files are marked as active=0."""
    vault = tmp_path / "vault"
    area = vault / "02-Areas"
    area.mkdir(parents=True)
    doc = area / "doc.md"
    doc.write_text("# Will Be Removed")

    db = sqlite3.connect(":memory:")
    _setup_schema(db)

    scanner = DocumentScanner(db, vault)
    scanner.scan([CollectionConfig(name="test", path="02-Areas")])
    assert db.execute("SELECT active FROM documents").fetchone()[0] == 1

    doc.unlink()
    r2 = scanner.scan([CollectionConfig(name="test", path="02-Areas")])
    assert r2.removed == 1
    assert db.execute("SELECT active FROM documents").fetchone()[0] == 0


@pytest.mark.unit
def test_scan_excludes_patterns(tmp_path: __import__("pathlib").Path) -> None:
    """Exclude patterns filter out matching files."""
    vault = tmp_path / "vault"
    area = vault / "02-Areas"
    (area / "templates").mkdir(parents=True)
    (area / "real.md").write_text("# Real")
    (area / "templates" / "tmpl.md").write_text("# Template")

    db = sqlite3.connect(":memory:")
    _setup_schema(db)

    scanner = DocumentScanner(db, vault)
    report = scanner.scan(
        [
            CollectionConfig(name="test", path="02-Areas", exclude=["templates"]),
        ]
    )
    assert report.new == 1


@pytest.mark.unit
def test_scan_report_str() -> None:
    """ScanReport has a useful string representation."""
    r = ScanReport(new=3, updated=1, removed=2, unchanged=10, collections_scanned=2)
    assert "3 new" in str(r)
    assert "1 updated" in str(r)
    assert "2 removed" in str(r)


# ---------------------------------------------------------------------------
# Content-hash dedup across collections
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scan_skips_duplicate_content_across_collections(
    tmp_path: __import__("pathlib").Path,
) -> None:
    """Same content at different paths is only indexed once."""
    vault = tmp_path / "vault"
    # Create identical content in two collection paths
    area_a = vault / "col-a"
    area_b = vault / "col-b"
    area_a.mkdir(parents=True)
    area_b.mkdir(parents=True)

    content = "# Shared Document\n\nThis content is identical in both collections."
    (area_a / "shared.md").write_text(content)
    (area_b / "shared.md").write_text(content)

    db = sqlite3.connect(":memory:")
    _setup_schema(db)

    scanner = DocumentScanner(db, vault)
    report = scanner.scan(
        [
            CollectionConfig(name="first", path="col-a"),
            CollectionConfig(name="second", path="col-b"),
        ]
    )

    # First collection indexes the doc; second collection skips the duplicate
    assert report.new == 1
    docs = db.execute("SELECT collection, path FROM documents WHERE active = 1").fetchall()
    assert len(docs) == 1
    assert docs[0][0] == "first"


@pytest.mark.unit
def test_scan_allows_update_to_existing_path(
    tmp_path: __import__("pathlib").Path,
) -> None:
    """Changed content at the same path is updated, not blocked by dedup."""
    vault = tmp_path / "vault"
    area = vault / "docs"
    area.mkdir(parents=True)
    (area / "doc.md").write_text("# Version 1\n\nOriginal content.")

    db = sqlite3.connect(":memory:")
    _setup_schema(db)

    scanner = DocumentScanner(db, vault)
    r1 = scanner.scan([CollectionConfig(name="test", path="docs")])
    assert r1.new == 1

    # Modify the file
    (area / "doc.md").write_text("# Version 2\n\nUpdated content.")
    r2 = scanner.scan([CollectionConfig(name="test", path="docs")])
    assert r2.updated == 1
    assert r2.new == 0

    # Content should be the new version
    row = db.execute("SELECT doc FROM content ORDER BY created_at DESC LIMIT 1").fetchone()
    assert "Version 2" in row[0]


# ---------------------------------------------------------------------------
# agent_owner tagging (#114)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scan_tags_documents_with_agent_owner(
    tmp_path: __import__("pathlib").Path,
) -> None:
    """Scanner tags rows with agent_owner from the injected resolver.

    Documents under an agent's write_path get the agent name; documents
    outside any write_path get NULL (treated as shared).
    """
    vault = tmp_path / "vault"
    agent_root = vault / "04-Agent-Knowledge" / "shape" / "memory"
    shared_root = vault / "02-Areas"
    agent_root.mkdir(parents=True)
    shared_root.mkdir(parents=True)
    (agent_root / "note.md").write_text("# Shape memory note")
    (shared_root / "doc.md").write_text("# Shared doc")

    def resolver(_collection: str, rel_path: str) -> str | None:
        if rel_path.startswith("04-Agent-Knowledge/shape/"):
            return "shape"
        return None

    db = sqlite3.connect(":memory:")
    _setup_schema(db)

    scanner = DocumentScanner(db, vault, agent_owner_resolver=resolver)
    scanner.scan(
        [
            CollectionConfig(name="agent-knowledge", path="04-Agent-Knowledge"),
            CollectionConfig(name="areas", path="02-Areas"),
        ]
    )

    rows = db.execute("SELECT path, agent_owner FROM documents WHERE active = 1 ORDER BY path").fetchall()
    by_path = dict(rows)
    assert by_path["04-Agent-Knowledge/shape/memory/note.md"] == "shape"
    assert by_path["02-Areas/doc.md"] is None


@pytest.mark.unit
def test_scan_with_no_resolver_leaves_agent_owner_null(
    tmp_path: __import__("pathlib").Path,
) -> None:
    """When no resolver is injected, agent_owner is NULL for every row."""
    vault = tmp_path / "vault"
    area = vault / "02-Areas"
    area.mkdir(parents=True)
    (area / "doc.md").write_text("# Doc")

    db = sqlite3.connect(":memory:")
    _setup_schema(db)

    scanner = DocumentScanner(db, vault)  # no agent_owner_resolver
    scanner.scan([CollectionConfig(name="areas", path="02-Areas")])

    row = db.execute("SELECT agent_owner FROM documents WHERE active = 1").fetchone()
    assert row[0] is None
