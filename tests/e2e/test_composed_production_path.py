"""End-to-end composed production path test — F48 canonical exemplar.

Exercises the full composed production code path:

  paths setup (FakePaths over tmp_path)
    → real SQLite schema + FTS5 index
    → real DocumentScanner ingest of a fixture markdown file
    → real factory.build_search_pipeline(paths=…)
    → real SearchPipeline.search() through the composed pipeline
    → assertion that the ingested document is retrievable

This is the test shape that would have failed during Plan-B-parity. 5,233
green unit/contract/BDD tests passed because every layer's fakes hid
composition failures; this test exercises composition end-to-end against
real SQLite + FTS5 with the production factory wiring it together.

F48 contract: file exists, carries ``@pytest.mark.e2e``, runs in CI Stage
4.5 under ``pytest -m e2e``, and exercises real composition. Every new
top-level capability (provider, connector, extractor, retrieval mode)
gets a sibling ``tests/e2e/test_composed_<capability>_path.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kairix.core.db.scanner import CollectionConfig, DocumentScanner
from kairix.core.db.schema import create_schema
from kairix.core.factory import build_search_pipeline, reset_search_pipeline_cache
from kairix.core.search.config import RetrievalConfig
from tests.fakes import FakePaths, FakeProvider, FakeProviderRegistry


def _extract_path(row: object) -> str:
    """Extract the document path from a ``BudgetedResult`` row.

    ``SearchResult.results`` is ``list[BudgetedResult]`` where each row
    wraps a ``FusedResult`` (`kairix.core.search.rrf.FusedResult`). The
    document filesystem path lives at ``row.result.path``.
    """
    inner = getattr(row, "result", None)
    return str(getattr(inner, "path", "") or "")


def _build_e2e_environment(tmp_path: Path, fixture_text: str, fixture_name: str) -> Path:
    """Build a real on-disk environment: document_root + populated SQLite + FTS5.

    Returns the ``db_path`` for the caller to thread into ``FakePaths``.
    Mirrors the production setup chain: write document → schema → scan →
    FTS populate. No mocks, no monkeypatching — every step is the real
    production code path.
    """
    document_root = tmp_path / "vault"
    document_root.mkdir()
    fixture = document_root / fixture_name
    fixture.write_text(fixture_text)

    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(str(db_path), timeout=10.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    create_schema(db)

    scanner = DocumentScanner(db, document_root=document_root)
    scanner.scan([CollectionConfig(name="vault", path=".")])

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
    db.close()
    return db_path


@pytest.mark.e2e
def test_composed_production_path(tmp_path: Path) -> None:
    """Composed production path: real ingest → real factory → real search.

    Sabotage-proof: mutating the BM25 backend to return empty results
    (commenting out the ``self._bm25.search(...)`` call in
    ``SearchPipeline.search``) makes this assertion fail. The test is
    wired through the composed code path, not against an isolated layer.
    """
    fixture_text = (
        "# Plan B-parity post-mortem\n\n"
        "The Plan B-parity post-mortem identified the composition gap "
        "that no test had previously exercised. 5233 green tests passed "
        "while the production-path LoCoMo benchmark fell to 5%.\n"
    )
    db_path = _build_e2e_environment(
        tmp_path,
        fixture_text=fixture_text,
        fixture_name="post_mortem.md",
    )

    paths = FakePaths(
        document_root=tmp_path / "vault",
        db_path=db_path,
        log_dir=tmp_path / "logs",
        workspace_root=tmp_path / "workspaces",
    )

    reset_search_pipeline_cache()
    cfg = RetrievalConfig(provider="fake")
    registry = FakeProviderRegistry({"fake": FakeProvider(name="fake", vector=[0.1] * 1536, dim=1536)})
    pipeline = build_search_pipeline(config=cfg, registry=registry, paths=paths)

    result = pipeline.search(query="Plan B-parity post-mortem", budget=3000)

    # The ingest path wrote the fixture; the search path must retrieve it.
    # Both asserts are load-bearing: the first proves the composed pipeline
    # returned something; the second proves it returned the right thing.
    assert result.results, (
        f"ingest succeeded (fixture at {db_path}) but search returned no results — "
        f"composed pipeline broken end-to-end. error={result.error!r} "
        f"bm25_count={result.bm25_count} vec_count={result.vec_count}"
    )
    returned_paths = [_extract_path(row) for row in result.results]
    assert any("post_mortem" in p for p in returned_paths if p), (
        f"search returned results but not the ingested fixture: {returned_paths}"
    )


@pytest.mark.e2e
def test_composed_usage_guide_resolves_on_clean_install() -> None:
    """#466 — the usage_guide MCP tool returns content on a clean install.

    A clean install is exactly the absence of any operator action: no
    ``onboard guide`` run, no ``docs/`` copy, no ``--guide-path`` override.
    The bundled guide ships as package data under
    ``kairix/agents/usage_guide/data/``, so the production MCP tool path
    resolves it from the installed package and an agent gets guidance
    instead of the ``UsageGuideNotFound`` error envelope.

    This drives the real MCP handler (``tool_usage_guide``), which composes
    ``kairix.use_cases.usage_guide.run_usage_guide`` with its production
    default resolver — no fakes on the guide-resolution path.

    Sabotage-proof: point ``_GUIDE_RESOURCE`` in
    ``kairix/use_cases/usage_guide.py`` at a non-existent ``data/`` file —
    the resolver no longer finds the bundled copy, the tool returns the
    ``UsageGuideNotFound`` envelope and both assertions below fail.
    """
    from kairix.agents.mcp.server import tool_usage_guide

    result = tool_usage_guide(topic="")
    assert result["error"] == "", (
        f"usage_guide returned an error on a clean install (#466 regression): {result['error']!r}"
    )
    assert result["content"].strip(), f"usage_guide returned empty content on a clean install: {result!r}"
