"""KairixNativeMemoryStore over a REAL factory-built SearchPipeline (PLA-472).

Regression for the hit-shape drift: the adapter read ``hit.path`` /
``hit.snippet``, but the real :class:`SearchPipeline` returns
:class:`kairix.core.search.budget.BudgetedResult` rows (path at
``row.result.path``, text at ``row.content``). Against the real pipeline
every memory came back with an empty id and empty content, and the id
could never round-trip into ``update`` / ``delete``.

Composition (F47): the pipeline is built through
``kairix.core.factory.build_search_pipeline(paths=FakePaths(...))`` over a
real SQLite + FTS5 index scanned from a ``tmp_path`` document root. No
monkeypatching, no stub hit shape.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kairix.core.db.scanner import CollectionConfig, DocumentScanner
from kairix.core.db.schema import create_schema
from kairix.core.factory import build_search_pipeline, reset_search_pipeline_cache
from kairix.core.search.config import RetrievalConfig
from kairix.memory_stores import KairixNativeMemoryStore
from tests.fakes import FakePaths, FakeProvider, FakeProviderRegistry

pytestmark = pytest.mark.integration


def _index_document_root(document_root: Path, db_path: Path) -> None:
    """Scan ``document_root`` into a fresh SQLite index and populate FTS5."""
    db = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        create_schema(db)
        DocumentScanner(db, document_root=document_root).scan([CollectionConfig(name="vault", path=".")])
        db.execute("DELETE FROM documents_fts")
        db.execute(
            """
            INSERT INTO documents_fts (rowid, filepath, title, doc)
            SELECT d.id, d.path, d.title, c.doc
            FROM documents d
            JOIN content c ON c.hash = d.hash
            WHERE d.active = 1
            """
        )
        db.commit()
    finally:
        db.close()


def test_search_over_real_pipeline_returns_add_id_and_content_that_round_trip(tmp_path: Path) -> None:
    """add → index → search (real pipeline) → id round-trips into update/delete.

    Sabotage proof: reverting ``_hit_to_memory`` to read ``hit.path`` /
    ``hit.snippet`` off the BudgetedResult yields ``id == ""`` and
    ``content == ""``, failing the first assertions; surfacing the
    document-relative path (``memories/<id>.md``) instead of the stem
    fails the ``id == mem_id`` assertion and the update round-trip.
    """
    document_root = tmp_path / "vault"
    document_root.mkdir()
    db_path = tmp_path / "index.sqlite"
    paths = FakePaths(
        document_root=document_root,
        db_path=db_path,
        log_dir=tmp_path / "logs",
        workspace_root=tmp_path / "workspaces",
    )

    _index_document_root(document_root, db_path)  # empty schema so the factory can open the index
    reset_search_pipeline_cache()
    registry = FakeProviderRegistry({"fake": FakeProvider(name="fake", vector=[0.1] * 1536, dim=1536)})
    pipeline = build_search_pipeline(
        # rerank_intents=() keeps the cross-encoder (a network model download)
        # out of the test — the hit-shape projection is what's under test.
        config=RetrievalConfig(provider="fake", rerank_intents=()),
        registry=registry,
        paths=paths,
    )
    store = KairixNativeMemoryStore(pipeline=pipeline, paths=paths)

    mem_id = store.add("Agent-alpha prefers zanzibar marmalade on toast every morning.")
    _index_document_root(document_root, db_path)  # the caller-run ingest step the adapter documents

    memories = store.search("zanzibar marmalade")
    reset_search_pipeline_cache()

    assert memories, "real pipeline must retrieve the ingested memory"
    top = memories[0]
    assert top.id == mem_id, f"search must surface the same id add() returned; got {top.id!r}"
    assert "zanzibar marmalade" in top.content
    assert top.score > 0.0
    assert top.metadata["path"].endswith(f"memories/{mem_id}.md")

    store.update(top.id, "Agent-alpha switched to quince jam.")
    assert "quince jam" in (document_root / "memories" / f"{mem_id}.md").read_text(encoding="utf-8")

    store.delete(top.id)
    assert not (document_root / "memories" / f"{mem_id}.md").exists()
