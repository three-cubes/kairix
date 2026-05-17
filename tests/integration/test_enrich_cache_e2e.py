"""Integration test: SearchPipeline enrich stage hits the chunk-date cache.

Wires a real ``SQLiteDocumentRepository`` (with the LRU cache from W1D's
SQLite-WAL-contention fix) into a real ``SearchPipeline`` and runs two
end-to-end searches whose fused result sets carry overlapping paths. The
cache should serve the second enrich call without re-running the SQL JOIN.

No @patch, no monkeypatching of kairix internals — the repository is
the production class and the SQL backend invocation count is observed
via the LRU wrapper's ``cache_info()``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kairix.core.db import open_db
from kairix.core.db.repository import SQLiteDocumentRepository
from kairix.core.db.schema import create_schema
from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchPipeline
from tests.fakes import (
    FakeClassifier,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakeSearchLogger,
    FakeVectorRepository,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers (kept small — F17 dedup safe, no shared 10+ char string literals)
# ---------------------------------------------------------------------------


def _build_real_db(tmp_path: Path) -> tuple[Path, SQLiteDocumentRepository]:
    """Build an on-disk SQLite DB with the kairix schema and a real repo."""
    db_path = tmp_path / "enrich-e2e.sqlite"
    db = sqlite3.connect(str(db_path), timeout=10.0)
    db.execute("PRAGMA journal_mode=WAL")
    try:
        create_schema(db)
    finally:
        db.close()
    return db_path, SQLiteDocumentRepository(db_path=db_path)


def _seed_chunk_dated_doc(
    db_path: Path,
    repo: SQLiteDocumentRepository,
    *,
    path: str,
    chunk_date: str,
    content_hash: str,
) -> None:
    """Insert a document + content_vectors row with a chunk_date."""
    repo.insert_or_update(path, "notes", "Doc", "alpha keyword body", content_hash)
    db = open_db(db_path)
    try:
        db.execute(
            "INSERT INTO content_vectors (hash, seq, pos, model, embedded_at, chunk_date) VALUES (?, ?, ?, ?, ?, ?)",
            (content_hash, 0, 0, "model", 1000, chunk_date),
        )
        db.commit()
    finally:
        db.close()


def _vec_hit(path: str, distance: float = 0.1) -> dict:
    return {
        "path": path,
        "file": path,
        "distance": distance,
        "collection": "notes",
        "title": "T",
        "snippet": "snip",
    }


def _build_pipeline(
    doc_repo: SQLiteDocumentRepository,
    vec_results: list[dict],
) -> SearchPipeline:
    return SearchPipeline(
        classifier=FakeClassifier(intent=QueryIntent.SEMANTIC),
        bm25=BM25SearchBackend(doc_repo),
        vector=VectorSearchBackend(
            FakeEmbeddingService(),
            FakeVectorRepository(results=vec_results),
        ),
        graph=FakeGraphRepository(available=False),
        fusion=RRFFusion(k=60),
        boosts=[],
        logger=FakeSearchLogger(),
        config=RetrievalConfig.defaults(),
    )


# ---------------------------------------------------------------------------
# Cache-hit behaviour across two pipeline searches
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_second_search_with_overlapping_paths_hits_enrich_cache(tmp_path: Path) -> None:
    """The second pipeline search with the same vector hits must not re-run
    the chunk-date SQL JOIN.

    Sabotage: if the cache were removed (``get_chunk_dates`` straight-piping
    to the SQL helper), every search would record a fresh ``misses``
    increment and the post-second-search assertion ``misses == 1`` would
    fail (it would be 2).
    """
    db_path, repo = _build_real_db(tmp_path)
    _seed_chunk_dated_doc(db_path, repo, path="/abs/notes/dated.md", chunk_date="2026-05-01", content_hash="h-1")

    vec_results = [_vec_hit("/abs/notes/dated.md")]
    pipeline = _build_pipeline(repo, vec_results)

    pipeline.search("alpha")
    assert repo._chunk_dates_cache.cache_info().misses == 1

    pipeline.search("alpha")
    info = repo._chunk_dates_cache.cache_info()
    # Second search produced the same fused-path set, so the enrich-stage
    # frozenset key matches the prior call and the SQL helper is NOT re-entered.
    assert info.misses == 1
    assert info.hits >= 1


@pytest.mark.integration
def test_first_search_actually_populates_cache(tmp_path: Path) -> None:
    """The first search must drive at least one miss — proves the cache is
    on the live code path, not a dead branch.

    Sabotage: if the pipeline never called ``get_chunk_dates`` (e.g. the
    enrich stage stopped wiring through to the repo), ``misses`` would
    stay at 0 and the test would fail.
    """
    db_path, repo = _build_real_db(tmp_path)
    _seed_chunk_dated_doc(
        db_path,
        repo,
        path="/abs/notes/first.md",
        chunk_date="2026-05-03",
        content_hash="h-first",
    )

    vec_results = [_vec_hit("/abs/notes/first.md")]
    pipeline = _build_pipeline(repo, vec_results)

    assert repo._chunk_dates_cache.cache_info().misses == 0
    pipeline.search("alpha")
    assert repo._chunk_dates_cache.cache_info().misses == 1


@pytest.mark.integration
def test_disjoint_search_path_sets_each_hit_the_sql_backend(tmp_path: Path) -> None:
    """Two searches whose fused result sets share no paths each invoke the
    SQL backend exactly once.

    Sabotage: if the cache key collapsed everything onto a single entry,
    the second search would skip the SQL call and ``misses`` would stay
    at 1 — the assertion ``misses == 2`` would fail.
    """
    db_path, repo = _build_real_db(tmp_path)
    _seed_chunk_dated_doc(db_path, repo, path="/abs/notes/p1.md", chunk_date="2026-05-10", content_hash="h-p1")
    _seed_chunk_dated_doc(db_path, repo, path="/abs/notes/p2.md", chunk_date="2026-05-11", content_hash="h-p2")

    pipeline_a = _build_pipeline(repo, [_vec_hit("/abs/notes/p1.md")])
    pipeline_b = _build_pipeline(repo, [_vec_hit("/abs/notes/p2.md")])

    pipeline_a.search("alpha")
    pipeline_b.search("alpha")

    info = repo._chunk_dates_cache.cache_info()
    assert info.misses == 2
