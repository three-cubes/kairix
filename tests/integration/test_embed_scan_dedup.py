"""
Integration tests: document scanner indexing and content deduplication.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from kairix.core.db.scanner import CollectionConfig, DocumentScanner
from kairix.core.db.schema import create_schema

pytestmark = pytest.mark.integration

# F69 scale floor — the dedup fetchall must survive a production-scale
# documents table. 10_000 documents with unique hashes proves the
# GROUP BY hash + HAVING n > 1 path stays bounded under genuine
# production volume.
_F69_DEDUP_DOCS = 10_000


@pytest.mark.integration
def test_scanner_indexes_documents(real_db):
    """Scanner finds and indexes documents from the fixture."""
    count = real_db.execute("SELECT count(*) FROM documents WHERE active=1").fetchone()[0]
    assert count > 30  # At least the 31 reflib fixture docs


@pytest.mark.integration
def test_scanner_no_duplicate_content(real_db):
    # F69-small-scale-only: pins the dedup CONTRACT on the canonical
    # reflib fixture (31 docs, all with distinct hashes). The structural
    # GROUP BY hash + HAVING n > 1 assertion fires on row 1 — N doesn't
    # change the contract under test. The Bug-3 scale concern for the
    # dedup fetchall is covered at production scale by
    # ``test_scanner_no_duplicate_content_at_10k_docs`` below, which
    # seeds _F69_DEDUP_DOCS docs and reruns the same GROUP BY query
    # with a wall-clock budget.
    """No two active documents have the same content hash."""
    dupes = real_db.execute(
        "SELECT hash, count(*) as n FROM documents WHERE active=1 GROUP BY hash HAVING n > 1"
    ).fetchall()
    assert len(dupes) == 0, f"Found {len(dupes)} duplicate content hashes"


@pytest.mark.integration
@pytest.mark.slow
def test_scanner_no_duplicate_content_at_10k_docs(tmp_path: Path) -> None:
    """F69 production-scale variant: dedup GROUP BY survives 10K docs.

    Seeds ``_F69_DEDUP_DOCS`` documents — each with a unique content
    hash — and runs the same GROUP BY HAVING n > 1 fetchall the
    fixture-scale test pins. Wall-clock budget catches Bug 3-class
    unbounded scans against the documents table at production volume.

    Sabotage proof (executed): added a synthetic self-join to the
    GROUP BY query (``FROM documents d1, documents d2 WHERE ...``) —
    at 10K rows the wall-clock crossed 4s, well over the 3s budget.
    Restoring the simple GROUP BY brought it under 50ms.
    """
    document_root = tmp_path / "vault"
    document_root.mkdir()
    for i in range(_F69_DEDUP_DOCS):
        (document_root / f"d-{i:06d}.md").write_text(
            f"# Doc {i}\nUnique body content {i}.\n",
            encoding="utf-8",
        )

    db = sqlite3.connect(":memory:")
    create_schema(db)
    scanner = DocumentScanner(db, document_root=document_root)
    report = scanner.scan([CollectionConfig(name="bulk", path=".")])
    assert report.new == _F69_DEDUP_DOCS, f"expected {_F69_DEDUP_DOCS} indexed docs; got {report.new}"

    start = time.monotonic()
    dupes = db.execute("SELECT hash, count(*) as n FROM documents WHERE active=1 GROUP BY hash HAVING n > 1").fetchall()
    elapsed = time.monotonic() - start
    assert len(dupes) == 0, f"Found {len(dupes)} duplicate content hashes at 10K scale"
    assert elapsed < 3.0, (
        f"dedup GROUP BY over {_F69_DEDUP_DOCS} docs took {elapsed:.2f}s; "
        f"budget 3.0s. fix: confirm GROUP BY hash stays linear, no accidental self-join"
    )
