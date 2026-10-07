"""End-to-end integration tests for the batched metadata fetch in
``kairix.core.search.vec_index.VectorIndex._resolve_match_metadata`` (#287).

Wires a real ``VectorIndex`` against a real on-disk SQLite database
seeded with a handful of rows AND a real usearch index built from
synthetic vectors. Asserts:

  * result count + ordering match the usearch primitive's output
  * active-flag filter still works through the public ``search`` entry
  * collection-filter still works through the public ``search`` entry
  * a ``k=20`` search resolves all metadata in ONE batched SELECT

The N+1 regression (#287 W2A report, profiled at 440 ms) is guarded by
counting the metadata SELECTs through the SQL-trace seam — a
deterministic outcome — rather than a wall-clock ceiling (F82).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from kairix.core.search.vec_index import VectorIndex

pytestmark = pytest.mark.integration


def _seed(tmp_path: Path, *, n_docs: int) -> Path:
    """Seed the real production schema with ``n_docs`` rows across two collections."""
    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, collection TEXT, path TEXT,
            title TEXT, hash TEXT, active INTEGER DEFAULT 1,
            source_page INTEGER, source_uri TEXT,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        """
    )
    for i in range(n_docs):
        collection = "reference-library" if i % 2 == 0 else "vault-projects"
        db.execute(
            "INSERT INTO documents (collection, path, title, hash, active) VALUES (?,?,?,?,1)",
            (collection, f"{collection}/doc-{i}.md", f"doc-{i}", f"hash{i}"),
        )
        db.execute(
            "INSERT INTO content (hash, doc) VALUES (?,?)",
            (f"hash{i}", f"Content of document {i}. " + ("filler " * 30)),
        )
    db.commit()
    db.close()
    return db_path


def _make_index(tmp_path: Path, db_path: Path, n_docs: int) -> Any:
    """Build a real usearch index whose keys map to the seeded hashes."""
    index_path = tmp_path / "vectors.usearch"
    meta_path = tmp_path / "vectors.meta.json"

    rng = np.random.default_rng(seed=42)
    vectors = rng.random((n_docs, 1536), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / norms

    idx = VectorIndex(index_path=index_path, meta_path=meta_path, db_path=db_path)
    idx.build_from_vectors([f"hash{i}_0" for i in range(n_docs)], vectors)
    return idx


@pytest.mark.integration
def test_search_returns_k_results_in_distance_order(tmp_path: Path) -> None:
    """End-to-end: real usearch + real SQLite + real batched metadata fetch.

    Sabotage: returning ``rows_by_hash.values()`` in ``_build_results``
    instead of the ordered ANN loop breaks the distance-sorted invariant
    — the ``sorted(distances)`` assertion below fires.
    """
    db_path = _seed(tmp_path, n_docs=5)
    idx = _make_index(tmp_path, db_path, n_docs=5)

    rng = np.random.default_rng(seed=99)
    query = rng.random(1536, dtype=np.float32)
    query /= np.linalg.norm(query)
    results = idx.search(query, k=5)

    assert len(results) == 5, f"expected 5 results from a 5-doc index; got {len(results)}"
    distances = [r["distance"] for r in results]
    assert distances == sorted(distances), f"results not in ANN distance order: {distances}"


@pytest.mark.integration
def test_search_respects_active_flag_via_batched_fetch(tmp_path: Path) -> None:
    """Marking a doc inactive removes it from results via the batched fetch.

    Sabotage: dropping ``d.active = 1`` from the WHERE clause leaks the
    inactive doc into the result set — the missing-hash assertion fires.
    """
    db_path = _seed(tmp_path, n_docs=5)
    idx = _make_index(tmp_path, db_path, n_docs=5)

    # Archive hash2 in the DB AFTER the index is built — exactly mirrors
    # the production "doc archived after embedding" path.
    db = sqlite3.connect(str(db_path))
    db.execute("UPDATE documents SET active = 0 WHERE hash = ?", ("hash2",))
    db.commit()
    db.close()

    rng = np.random.default_rng(seed=99)
    query = rng.random(1536, dtype=np.float32)
    query /= np.linalg.norm(query)
    results = idx.search(query, k=5)

    hash_seqs = {r["hash_seq"] for r in results}
    assert "hash2_0" not in hash_seqs, f"inactive doc leaked into results: {hash_seqs}"
    assert len(results) == 4, f"expected 4 results (5 minus inactive); got {len(results)}"


@pytest.mark.integration
def test_search_collection_filter_through_batched_fetch(tmp_path: Path) -> None:
    """Collection filter keeps only reference-library rows.

    Sabotage: dropping ``if collections and row["collection"] not in ...``
    in ``_build_results`` returns vault-projects rows too — the
    "all results in reference-library" assertion fires.
    """
    db_path = _seed(tmp_path, n_docs=10)
    idx = _make_index(tmp_path, db_path, n_docs=10)

    rng = np.random.default_rng(seed=99)
    query = rng.random(1536, dtype=np.float32)
    query /= np.linalg.norm(query)
    results = idx.search(query, k=10, collections=["reference-library"])

    assert results, "expected at least one reference-library hit"
    for r in results:
        assert r["collection"] == "reference-library", f"non-reference-library leaked: {r['collection']} at {r['path']}"


@pytest.mark.integration
def test_search_k20_resolves_metadata_in_one_select(tmp_path: Path) -> None:
    """k=20 over 50 docs resolves all metadata with ONE batched SELECT.

    The N+1 regression (#287 — profiled at 440 ms) issued one metadata
    SELECT per ANN match. The deterministic outcome that matters is the
    query count, not the wall clock (F82): the SQL trace installed via
    the public ``set_metadata_trace_callback`` seam counts exactly the
    SELECTs production issues against ``documents``.

    Sabotage: setting ``_IN_CLAUSE_BATCH_SIZE = 1`` (per-row SELECT, the
    N+1 shape) makes the trace record 20 metadata SELECTs and the
    single-SELECT assertion fires.
    """
    db_path = _seed(tmp_path, n_docs=50)
    idx = _make_index(tmp_path, db_path, n_docs=50)

    rng = np.random.default_rng(seed=99)
    query = rng.random(1536, dtype=np.float32)
    query /= np.linalg.norm(query)

    sql_log: list[str] = []
    idx.set_metadata_trace_callback(sql_log.append)
    try:
        results = idx.search(query, k=20)
    finally:
        idx.set_metadata_trace_callback(None)

    assert len(results) == 20
    metadata_selects = [sql for sql in sql_log if "FROM documents" in sql]
    assert len(metadata_selects) == 1, (
        f"k=20 search should resolve metadata in a single batched SELECT; "
        f"saw {len(metadata_selects)} — possible N+1 regression"
    )
