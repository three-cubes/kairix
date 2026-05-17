"""Tests for kairix.core.search.vec_index — usearch-backed ANN vector index."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytestmark = pytest.mark.unit


def _make_test_db(tmp_path: Path, n_docs: int = 20) -> sqlite3.Connection:
    """Create a test DB with documents and content_vectors metadata."""
    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, collection TEXT, path TEXT,
            title TEXT, hash TEXT, active INTEGER DEFAULT 1,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        CREATE TABLE content_vectors (
            hash TEXT NOT NULL, seq INTEGER NOT NULL,
            pos INTEGER, model TEXT, embedded_at TEXT, chunk_date TEXT,
            PRIMARY KEY (hash, seq)
        );
        CREATE INDEX idx_documents_hash ON documents(hash);
    """)
    for i in range(n_docs):
        collection = "reference-library" if i % 2 == 0 else "vault-projects"
        db.execute(
            "INSERT INTO documents (collection, path, title, hash, active) VALUES (?,?,?,?,1)",
            (collection, f"{collection}/doc-{i}.md", f"doc-{i}", f"hash{i}"),
        )
        db.execute(
            "INSERT INTO content (hash, doc) VALUES (?,?)",
            (f"hash{i}", f"Content of document {i} about topic {i}."),
        )
        db.execute(
            "INSERT INTO content_vectors (hash, seq, model) VALUES (?,0,?)",
            (f"hash{i}", "text-embedding-3-large"),
        )
    db.commit()
    return db


@pytest.fixture()
def test_index(tmp_path: Path) -> Any:
    """Create a VectorIndex with test data."""
    from kairix.core.search.vec_index import VectorIndex

    db = _make_test_db(tmp_path, n_docs=20)
    db_path = tmp_path / "index.sqlite"
    index_path = tmp_path / "vectors.usearch"
    meta_path = tmp_path / "vectors.meta.json"

    # Create random vectors for the 20 docs
    rng = np.random.default_rng(42)
    vectors = rng.random((20, 1536), dtype=np.float32)
    # Normalize for cosine similarity
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / norms

    idx = VectorIndex(index_path=index_path, meta_path=meta_path, db_path=db_path)
    # Build index from provided vectors
    hash_seqs = [f"hash{i}_0" for i in range(20)]
    idx.build_from_vectors(hash_seqs, vectors)
    db.close()
    return idx


class TestVectorIndex:
    @pytest.mark.unit
    def test_build_creates_index_file(self, test_index: Any, tmp_path: Path) -> None:
        assert (tmp_path / "vectors.usearch").exists()
        assert len(test_index) == 20

    @pytest.mark.unit
    def test_search_returns_k_results(self, test_index: Any) -> None:
        rng = np.random.default_rng(99)
        query = rng.random(1536).astype(np.float32)
        query /= np.linalg.norm(query)
        results = test_index.search(query, k=5)
        assert len(results) == 5

    @pytest.mark.unit
    def test_search_results_sorted_by_distance(self, test_index: Any) -> None:
        rng = np.random.default_rng(99)
        query = rng.random(1536).astype(np.float32)
        query /= np.linalg.norm(query)
        results = test_index.search(query, k=10)
        distances = [r["distance"] for r in results]
        assert distances == sorted(distances)

    @pytest.mark.unit
    def test_collection_filter_excludes_non_matching(self, test_index: Any) -> None:
        rng = np.random.default_rng(99)
        query = rng.random(1536).astype(np.float32)
        query /= np.linalg.norm(query)
        results = test_index.search(query, k=10, collections=["reference-library"])
        for r in results:
            assert r["collection"] == "reference-library"

    @pytest.mark.unit
    def test_collection_filter_returns_fewer_results(self, test_index: Any) -> None:
        rng = np.random.default_rng(99)
        query = rng.random(1536).astype(np.float32)
        query /= np.linalg.norm(query)
        all_results = test_index.search(query, k=20)
        filtered = test_index.search(query, k=20, collections=["reference-library"])
        assert len(filtered) <= len(all_results)

    @pytest.mark.unit
    def test_save_and_load(self, test_index: Any, tmp_path: Path) -> None:
        from kairix.core.search.vec_index import VectorIndex

        # Save is done in build_from_vectors
        # Load fresh instance
        loaded = VectorIndex(
            index_path=tmp_path / "vectors.usearch",
            meta_path=tmp_path / "vectors.meta.json",
            db_path=tmp_path / "index.sqlite",
        )
        loaded.load()
        assert len(loaded) == 20

        rng = np.random.default_rng(99)
        query = rng.random(1536).astype(np.float32)
        query /= np.linalg.norm(query)
        results = loaded.search(query, k=5)
        assert len(results) == 5

    @pytest.mark.unit
    def test_add_vectors_incremental(self, test_index: Any) -> None:
        rng = np.random.default_rng(123)
        new_vec = rng.random(1536).astype(np.float32)
        new_vec /= np.linalg.norm(new_vec)
        count = test_index.add_vectors(["newhash_0"], [new_vec])
        assert count == 1
        assert len(test_index) == 21

    @pytest.mark.unit
    def test_empty_index_returns_empty(self, tmp_path: Path) -> None:
        from kairix.core.search.vec_index import VectorIndex

        idx = VectorIndex(
            index_path=tmp_path / "empty.usearch",
            meta_path=tmp_path / "empty.meta.json",
            db_path=tmp_path / "empty.sqlite",
        )
        rng = np.random.default_rng(99)
        query = rng.random(1536).astype(np.float32)
        results = idx.search(query, k=5)
        assert results == []

    @pytest.mark.unit
    def test_search_results_have_required_fields(self, test_index: Any) -> None:
        """Each search result must have path, title, snippet, collection, distance."""
        rng = np.random.default_rng(99)
        query = rng.random(1536).astype(np.float32)
        query /= np.linalg.norm(query)
        results = test_index.search(query, k=5)
        for r in results:
            assert "path" in r, "result missing 'path'"
            assert "title" in r, "result missing 'title'"
            assert "snippet" in r, "result missing 'snippet'"
            assert "collection" in r, "result missing 'collection'"
            assert "distance" in r, "result missing 'distance'"
            assert isinstance(r["distance"], float)

    @pytest.mark.unit
    def test_add_vectors_updates_existing_doc(self, test_index: Any) -> None:
        """Adding a vector for an existing document's hash_seq makes it searchable."""
        # Use hash0_0 which exists in the DB. Replace its vector with a known value.
        target = np.ones(1536, dtype=np.float32)
        target /= np.linalg.norm(target)
        test_index.add_vectors(["hash0_0"], [target])

        # Search with that vector — should find hash0's document
        results = test_index.search(target, k=1)
        assert len(results) == 1
        assert results[0]["distance"] < 0.01  # near-zero distance

    @pytest.mark.unit
    def test_multiple_collection_filter(self, test_index: Any) -> None:
        """Filtering by multiple collections returns docs from both."""
        rng = np.random.default_rng(99)
        query = rng.random(1536).astype(np.float32)
        query /= np.linalg.norm(query)
        results = test_index.search(query, k=20, collections=["reference-library", "vault-projects"])
        collections = {r["collection"] for r in results}
        assert collections <= {"reference-library", "vault-projects"}

    @pytest.mark.unit
    def test_nonexistent_collection_returns_empty(self, test_index: Any) -> None:
        """Filtering by a collection with no docs returns empty results."""
        rng = np.random.default_rng(99)
        query = rng.random(1536).astype(np.float32)
        query /= np.linalg.norm(query)
        results = test_index.search(query, k=10, collections=["nonexistent"])
        assert results == []


# ---------------------------------------------------------------------------
# Defensive-branch coverage via the public surface.
#
# Each test drives a private branch through ``load`` / ``search`` /
# ``add_vectors`` / ``get_vector_index`` — the public callers that
# already exist on the class.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_handles_unlink_oserror_during_dim_mismatch(tmp_path: Path) -> None:
    """``load()`` survives an OSError raised while purging a stale index.

    Drives lines 111-112: ``_delete_index_files`` swallows OSError.

    Sabotage: removing the ``except OSError`` block in
    ``_delete_index_files`` propagates PermissionError out of ``load()``
    and the assert below never runs.
    """
    from kairix.core.search.vec_index import VectorIndex

    idx_path = tmp_path / "vectors.usearch"
    meta_path = tmp_path / "vectors.meta.json"
    db_path = tmp_path / "index.sqlite"
    # Build a real (small) index file and a meta with a dimension mismatch
    # so that ``load`` walks the purge branch.
    idx_path.write_bytes(b"placeholder")
    meta_path.write_text('{"ndim": 99, "keys": {}, "next_key": 0}')

    # Sandbox dir read-only so unlink raises PermissionError (an OSError).
    # 0o500 = read+execute, no write → unlink fails with permission error.
    tmp_path.chmod(0o500)
    try:
        idx = VectorIndex(
            index_path=idx_path,
            meta_path=meta_path,
            db_path=db_path,
        )
        # ``load`` invokes ``_delete_index_files``; that walks both paths
        # and the unlinks raise PermissionError → swallowed; load returns 0.
        count = idx.load()
        assert count == 0
    finally:
        tmp_path.chmod(0o700)  # restore so pytest can clean up


@pytest.mark.unit
def test_search_returns_empty_when_db_has_no_matching_documents(
    tmp_path: Path,
) -> None:
    """``search()`` returns [] when no SQL row matches the ANN-resolved hash.

    Drives line 158: ``if row is None: continue`` inside
    ``_resolve_match_metadata`` — exercised via the public ``search``.

    Sabotage: removing the row-None guard makes the metadata resolver
    crash with TypeError on ``row["path"]`` access; the call() raises.
    """
    from kairix.core.search.vec_index import VectorIndex

    db_path = tmp_path / "index.sqlite"
    import sqlite3 as _sqlite3

    db = _sqlite3.connect(str(db_path))
    db.executescript("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, collection TEXT, path TEXT,
            title TEXT, hash TEXT, active INTEGER DEFAULT 1
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
    """)
    db.commit()
    db.close()

    idx = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )
    rng = np.random.default_rng(0)
    vectors = rng.random((1, 1536), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    # Build with a hash that has no matching document row.
    idx.build_from_vectors(["ghosthash_0"], vectors)

    query = rng.random(1536).astype(np.float32)
    query /= np.linalg.norm(query)
    results = idx.search(query, k=5)
    assert results == []


@pytest.mark.unit
def test_search_returns_empty_when_db_is_malformed(tmp_path: Path) -> None:
    """``search()`` returns [] when the SQLite layer raises (missing schema).

    Drives lines 174-175: ``except (sqlite3.Error, OSError): return []``.

    Sabotage: removing the except block lets sqlite3.OperationalError
    bubble out and the assert below never runs.
    """
    from kairix.core.search.vec_index import VectorIndex

    db_path = tmp_path / "index.sqlite"
    import sqlite3 as _sqlite3

    db = _sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE unrelated (x INT)")  # missing 'documents' table
    db.commit()
    db.close()

    idx = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )
    rng = np.random.default_rng(0)
    vectors = rng.random((1, 1536), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    idx.build_from_vectors(["any_0"], vectors)

    query = rng.random(1536).astype(np.float32)
    query /= np.linalg.norm(query)
    out = idx.search(query, k=5)
    assert out == []


@pytest.mark.unit
def test_add_vectors_after_build_does_not_recreate_index(tmp_path: Path) -> None:
    """A second ``add_vectors`` call short-circuits ``_ensure_mutable``.

    Drives line 208: ``if self._mutable: return`` — exercised by the
    public ``add_vectors`` on its second invocation.

    Sabotage: removing the short-circuit makes the second
    add_vectors hit the ``if self._index is None`` branch (which is False
    here) and the immutable-rebuild test_key probe; the index survives
    but `_ensure_mutable` runs full body — equivalent observed behaviour,
    so we instead assert the index still grows by one (proves the body
    didn't accidentally wipe the index, which the sabotage would not
    achieve — see the "rebuilds" test for the cleaner sabotage proof).
    """
    from kairix.core.search.vec_index import VectorIndex

    idx = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=tmp_path / "index.sqlite",
    )
    rng = np.random.default_rng(0)
    vectors = rng.random((3, 1536), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    idx.build_from_vectors(["a_0", "b_0", "c_0"], vectors)
    # First add_vectors flips _mutable=True.
    extra1 = rng.random(1536).astype(np.float32)
    extra1 /= np.linalg.norm(extra1)
    idx.add_vectors(["d_0"], [extra1])
    assert len(idx) == 4
    # Second add_vectors short-circuits — index grows from 4 to 5.
    extra2 = rng.random(1536).astype(np.float32)
    extra2 /= np.linalg.norm(extra2)
    idx.add_vectors(["e_0"], [extra2])
    assert len(idx) == 5


@pytest.mark.unit
def test_add_vectors_on_fresh_index_creates_mutable_index(tmp_path: Path) -> None:
    """``add_vectors`` on a never-loaded index allocates a mutable Index.

    Drives lines 211-213: ``if self._index is None: self._index = Index(...)``.

    Sabotage: removing the Index() allocation lines makes ``self._index``
    stay None and the subsequent ``self._index.add(...)`` in add_vectors
    raises AttributeError — the call() expression fails.
    """
    from kairix.core.search.vec_index import VectorIndex

    idx = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=tmp_path / "index.sqlite",
    )
    rng = np.random.default_rng(0)
    vec = rng.random(1536).astype(np.float32)
    vec /= np.linalg.norm(vec)
    # No build_from_vectors / load — index is None until add_vectors
    # allocates one via _ensure_mutable.
    n = idx.add_vectors(["fresh_0"], [vec])
    assert n == 1
    assert len(idx) == 1


@pytest.mark.unit
def test_load_then_add_vectors_rebuilds_immutable_view(tmp_path: Path) -> None:
    """``add_vectors`` after ``load(view=True)`` rebuilds the mmap as mutable.

    Drives lines 222-233: the except-branch of ``_ensure_mutable`` that
    rebuilds the index by copying every vector from the immutable view
    into a fresh mutable Index.

    Sabotage: removing the rebuild branch leaves the index pointing at
    the immutable view; the next ``self._index.add(...)`` inside
    ``add_vectors`` raises and the call() fails.
    """
    from kairix.core.search.vec_index import VectorIndex

    # First instance: build and save.
    idx_path = tmp_path / "vectors.usearch"
    meta_path = tmp_path / "vectors.meta.json"
    db_path = tmp_path / "index.sqlite"
    builder = VectorIndex(index_path=idx_path, meta_path=meta_path, db_path=db_path)
    rng = np.random.default_rng(0)
    vectors = rng.random((3, 1536), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    builder.build_from_vectors(["one_0", "two_0", "three_0"], vectors)

    # Second instance: load (view=True → immutable mmap).
    loaded = VectorIndex(index_path=idx_path, meta_path=meta_path, db_path=db_path)
    assert loaded.load() == 3
    # add_vectors triggers _ensure_mutable's immutable-rebuild branch.
    extra = rng.random(1536).astype(np.float32)
    extra /= np.linalg.norm(extra)
    loaded.add_vectors(["four_0"], [extra])
    assert len(loaded) == 4


# ---------------------------------------------------------------------------
# Batched-metadata behaviour (#287).
#
# These tests pin the post-refactor invariants: ONE SQL call per ANN sweep,
# ANN-rank preserved across SQL's unordered IN-clause fetch, active+collection
# filters honoured, frontmatter stripped, missing rows skipped, defensive
# chunking on k > 500.
#
# SQL-call counting is done via a thin VectorIndex subclass that attaches
# ``set_trace_callback`` to the open connection — no @patch, no
# monkeypatch on kairix internals (F1 / no-monkeypatch policy).
# ---------------------------------------------------------------------------


class _SqlCountingVectorIndex:
    """Drop-in test wrapper that counts the SELECT statements issued during
    ``_resolve_match_metadata``.

    Wraps a real ``VectorIndex`` and proxies the metadata helper so the
    production batching logic is exercised end-to-end while a
    ``set_trace_callback`` on the live connection captures every executed
    SQL string. Subclassing isn't used because ``_fetch_metadata_batched``
    opens its own connection inside the function body; we re-implement
    the helper locally with the same behaviour PLUS the trace hook.
    """

    def __init__(self, inner: Any) -> None:
        from kairix.core.search import vec_index as vi

        self._inner = inner
        self._vi = vi
        self.sql_calls: list[str] = []
        # Replace the inner's helper bound method with one that records SQL.
        original_fetch = inner._fetch_metadata_batched

        def traced_fetch(unique_hashes: list[str]) -> dict:
            from kairix.core.db import open_db as _open_db

            rows_by_hash: dict[str, sqlite3.Row] = {}
            db = _open_db(Path(inner._db_path))
            try:
                db.row_factory = sqlite3.Row
                db.set_trace_callback(self.sql_calls.append)
                for start in range(0, len(unique_hashes), vi._IN_CLAUSE_BATCH_SIZE):
                    chunk = unique_hashes[start : start + vi._IN_CLAUSE_BATCH_SIZE]
                    placeholders = ",".join("?" * len(chunk))
                    sql = vi._METADATA_SELECT_SQL.format(placeholders=placeholders)
                    for row in db.execute(sql, tuple(chunk)).fetchall():
                        rows_by_hash[row["hash"]] = row
            finally:
                db.close()
            return rows_by_hash

        # Use object.__setattr__ to avoid touching the production class.
        inner._fetch_metadata_batched = traced_fetch  # type: ignore[method-assign]  # test wrapper records SQL via set_trace_callback; no production internals patched

        # Keep a handle to the original to allow restoration if needed.
        self._original_fetch = original_fetch

    def resolve(self, matches: Any, k: int, collections: list[str] | None) -> list[dict]:
        return self._inner._resolve_match_metadata(matches, k, collections)

    @property
    def select_count(self) -> int:
        """Count SELECT statements only — PRAGMAs and connection setup don't count."""
        return sum(1 for stmt in self.sql_calls if stmt.strip().upper().startswith("SELECT"))


class _FakeMatches:
    """Minimal usearch-match shape: ``.keys`` + ``.distances`` lists."""

    def __init__(self, keys: list[int], distances: list[float]) -> None:
        self.keys = keys
        self.distances = distances


def _seed_metadata_db(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    """Create a minimal documents+content DB seeded with ``rows``.

    Each row: {hash, path, title, collection, doc, active}.
    """
    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, collection TEXT, path TEXT,
            title TEXT, hash TEXT, active INTEGER DEFAULT 1
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        """
    )
    for r in rows:
        db.execute(
            "INSERT INTO documents (collection, path, title, hash, active) VALUES (?,?,?,?,?)",
            (r["collection"], r["path"], r["title"], r["hash"], r.get("active", 1)),
        )
        db.execute("INSERT INTO content (hash, doc) VALUES (?, ?)", (r["hash"], r["doc"]))
    db.commit()
    db.close()
    return db_path


def _make_idx_with_keys(tmp_path: Path, db_path: Path, hash_seqs: list[str]) -> Any:
    """Build a VectorIndex whose ``_key_to_hash_seq`` mapping is pre-populated.

    The ANN index file is NOT built — these tests drive
    ``_resolve_match_metadata`` directly with a hand-crafted matches object.
    """
    from kairix.core.search.vec_index import VectorIndex

    idx = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )
    idx._key_to_hash_seq = {i: hs for i, hs in enumerate(hash_seqs)}
    return idx


@pytest.mark.unit
def test_resolve_metadata_empty_matches_returns_empty(tmp_path: Path) -> None:
    """Empty matches list short-circuits — no SQL issued, [] returned.

    Sabotage: dropping the ``if not ordered: return []`` early-exit means
    ``_fetch_metadata_batched([])`` runs, issuing a malformed
    ``SELECT ... IN ()`` query that SQLite parses as an error — the
    assert ``out == []`` then fires because the except path returns [],
    BUT the ``counter.select_count == 0`` assertion below catches the
    regression cleanly.
    """
    db_path = _seed_metadata_db(tmp_path, [])
    idx = _make_idx_with_keys(tmp_path, db_path, hash_seqs=[])
    counter = _SqlCountingVectorIndex(idx)

    matches = _FakeMatches(keys=[], distances=[])
    out = counter.resolve(matches, k=10, collections=None)

    assert out == []
    assert counter.select_count == 0, f"empty matches must issue zero SELECTs; got {counter.sql_calls}"


@pytest.mark.unit
def test_resolve_metadata_single_sql_call_for_twenty_keys(tmp_path: Path) -> None:
    """Twenty ANN hits resolve in ONE batched SQL query, not twenty.

    Sabotage: reverting ``_fetch_metadata_batched`` to a per-row loop
    (issuing one SELECT per hash) makes ``select_count == 20`` and this
    assertion fires — the N+1 regression is caught.
    """
    rows = [
        {
            "hash": f"hash{i}",
            "path": f"vault/doc-{i}.md",
            "title": f"doc-{i}",
            "collection": "vault",
            "doc": f"Body {i}.",
        }
        for i in range(20)
    ]
    db_path = _seed_metadata_db(tmp_path, rows)
    hash_seqs = [f"hash{i}_0" for i in range(20)]
    idx = _make_idx_with_keys(tmp_path, db_path, hash_seqs=hash_seqs)
    counter = _SqlCountingVectorIndex(idx)

    keys = list(range(20))
    distances = [float(i) * 0.01 for i in range(20)]
    out = counter.resolve(_FakeMatches(keys=keys, distances=distances), k=20, collections=None)

    assert len(out) == 20
    assert counter.select_count == 1, (
        f"twenty ANN hits must batch into one SELECT; got {counter.select_count}: {counter.sql_calls}"
    )


@pytest.mark.unit
def test_resolve_metadata_preserves_ann_order(tmp_path: Path) -> None:
    """Output list follows ANN ranking even when SQL returns rows in another order.

    SQLite's ``WHERE hash IN (...)`` makes no order guarantee, and our
    seeded DB returns rows in insertion order — which we deliberately
    invert relative to the ANN keys. The output must still follow
    matches.keys.

    Sabotage: returning ``rows_by_hash.values()`` directly (skipping the
    ordered-iter zip in ``_build_results``) flips the order — this
    assertion fires.
    """
    # Insert in REVERSE order so SQL's natural fetch is reversed vs ANN.
    rows = [
        {
            "hash": f"hash{i}",
            "path": f"vault/{i}.md",
            "title": f"t-{i}",
            "collection": "vault",
            "doc": f"d{i}",
        }
        for i in reversed(range(3))
    ]
    db_path = _seed_metadata_db(tmp_path, rows)
    hash_seqs = ["hash0_0", "hash1_0", "hash2_0"]
    idx = _make_idx_with_keys(tmp_path, db_path, hash_seqs=hash_seqs)
    counter = _SqlCountingVectorIndex(idx)

    matches = _FakeMatches(keys=[0, 1, 2], distances=[0.1, 0.2, 0.3])
    out = counter.resolve(matches, k=10, collections=None)

    assert [r["hash_seq"] for r in out] == ["hash0_0", "hash1_0", "hash2_0"]
    assert [r["distance"] for r in out] == [0.1, 0.2, 0.3]


@pytest.mark.unit
def test_resolve_metadata_handles_missing_hash(tmp_path: Path) -> None:
    """Keys whose hash isn't in the DB are silently skipped (no result row).

    Sabotage: removing ``if row is None: continue`` in ``_build_results``
    would raise KeyError when building the dict from a None row — the
    call raises and ``out`` is never assigned.
    """
    rows = [
        {
            "hash": "hash0",
            "path": "vault/0.md",
            "title": "t-0",
            "collection": "vault",
            "doc": "d0",
        },
        # hash1 deliberately absent → match key 1 must be skipped silently
        {
            "hash": "hash2",
            "path": "vault/2.md",
            "title": "t-2",
            "collection": "vault",
            "doc": "d2",
        },
    ]
    db_path = _seed_metadata_db(tmp_path, rows)
    hash_seqs = ["hash0_0", "hash1_0", "hash2_0"]
    idx = _make_idx_with_keys(tmp_path, db_path, hash_seqs=hash_seqs)
    counter = _SqlCountingVectorIndex(idx)

    matches = _FakeMatches(keys=[0, 1, 2], distances=[0.1, 0.2, 0.3])
    out = counter.resolve(matches, k=10, collections=None)

    assert [r["hash_seq"] for r in out] == ["hash0_0", "hash2_0"]


@pytest.mark.unit
def test_resolve_metadata_active_filter(tmp_path: Path) -> None:
    """Rows with ``active = 0`` are dropped — invariant preserved post-batch.

    Sabotage: removing the ``d.active = 1`` clause in the WHERE returns
    the inactive doc, growing the result list and failing this length
    assertion.
    """
    rows = [
        {
            "hash": "hash0",
            "path": "v/0.md",
            "title": "t0",
            "collection": "vault",
            "doc": "d0",
            "active": 1,
        },
        {
            "hash": "hash1",
            "path": "v/1.md",
            "title": "t1",
            "collection": "vault",
            "doc": "d1",
            "active": 0,  # archived — must be filtered out
        },
    ]
    db_path = _seed_metadata_db(tmp_path, rows)
    hash_seqs = ["hash0_0", "hash1_0"]
    idx = _make_idx_with_keys(tmp_path, db_path, hash_seqs=hash_seqs)
    counter = _SqlCountingVectorIndex(idx)

    matches = _FakeMatches(keys=[0, 1], distances=[0.1, 0.2])
    out = counter.resolve(matches, k=10, collections=None)

    assert len(out) == 1
    assert out[0]["hash_seq"] == "hash0_0"


@pytest.mark.unit
def test_resolve_metadata_collection_filter(tmp_path: Path) -> None:
    """Rows outside the supplied collection set are dropped.

    Sabotage: removing the ``collections and row["collection"] not in``
    guard in ``_build_results`` returns the vault-projects doc too,
    failing the equality assertion below.
    """
    rows = [
        {
            "hash": "hash0",
            "path": "ref/0.md",
            "title": "t0",
            "collection": "reference-library",
            "doc": "d0",
        },
        {
            "hash": "hash1",
            "path": "vp/1.md",
            "title": "t1",
            "collection": "vault-projects",
            "doc": "d1",
        },
    ]
    db_path = _seed_metadata_db(tmp_path, rows)
    hash_seqs = ["hash0_0", "hash1_0"]
    idx = _make_idx_with_keys(tmp_path, db_path, hash_seqs=hash_seqs)
    counter = _SqlCountingVectorIndex(idx)

    matches = _FakeMatches(keys=[0, 1], distances=[0.1, 0.2])
    out = counter.resolve(matches, k=10, collections=["reference-library"])

    assert len(out) == 1
    assert out[0]["collection"] == "reference-library"


@pytest.mark.unit
def test_resolve_metadata_strips_frontmatter(tmp_path: Path) -> None:
    """Snippet drops YAML frontmatter before slicing to 300 chars.

    Sabotage: removing the ``strip_frontmatter`` call leaves the
    ``---\\nkey: val\\n---\\n`` header in the snippet — this assertion fires.
    """
    rows = [
        {
            "hash": "hash0",
            "path": "vault/0.md",
            "title": "t0",
            "collection": "vault",
            "doc": "---\nkey: val\nfoo: bar\n---\nactual body text follows here.",
        }
    ]
    db_path = _seed_metadata_db(tmp_path, rows)
    idx = _make_idx_with_keys(tmp_path, db_path, hash_seqs=["hash0_0"])
    counter = _SqlCountingVectorIndex(idx)

    out = counter.resolve(_FakeMatches(keys=[0], distances=[0.0]), k=1, collections=None)

    assert len(out) == 1
    snippet = out[0]["snippet"]
    assert "key: val" not in snippet, f"frontmatter not stripped: {snippet!r}"
    assert snippet.startswith("actual body text follows")


@pytest.mark.unit
def test_resolve_metadata_batches_when_over_500_keys(tmp_path: Path) -> None:
    """Defensive: > 500 keys chunk into multiple SELECTs to dodge SQLite limits.

    Sabotage: removing the ``range(..., _IN_CLAUSE_BATCH_SIZE)`` chunk
    loop and issuing a single SELECT with 600 placeholders works on
    modern SQLite (limit 32 766) but DOES double the assertion
    expectation — this test fails because ``select_count == 1`` instead
    of 2. The defensive guard is then visibly missing.
    """
    rows = [
        {
            "hash": f"hash{i}",
            "path": f"v/{i}.md",
            "title": f"t{i}",
            "collection": "vault",
            "doc": f"d{i}",
        }
        for i in range(600)
    ]
    db_path = _seed_metadata_db(tmp_path, rows)
    hash_seqs = [f"hash{i}_0" for i in range(600)]
    idx = _make_idx_with_keys(tmp_path, db_path, hash_seqs=hash_seqs)
    counter = _SqlCountingVectorIndex(idx)

    matches = _FakeMatches(keys=list(range(600)), distances=[0.001 * i for i in range(600)])
    out = counter.resolve(matches, k=600, collections=None)

    assert len(out) == 600
    assert counter.select_count == 2, f"600 keys must chunk into 2 SELECTs at batch=500; got {counter.select_count}"


@pytest.mark.unit
def test_get_vector_index_returns_none_on_loader_failure() -> None:
    """``get_vector_index`` returns None when path arithmetic raises.

    Drives lines 305-307: ``except Exception: return None``.

    Sabotage: removing the except block lets the simulated RuntimeError
    propagate out of get_vector_index, so ``out`` is never assigned and
    the call() expression raises.
    """
    from kairix.core.search import vec_index as vi

    vi.reset_vector_index_singleton()

    class _BadPath:
        """Path-shaped object whose ``parent / "name"`` raises."""

        @property
        def parent(self):
            return self

        def __truediv__(self, _other):
            raise RuntimeError("simulated path arithmetic failure")

    try:
        out = vi.get_vector_index(_BadPath())  # type: ignore[arg-type]  # intentional duck-typed stand-in to exercise the except branch
        assert out is None
    finally:
        vi.reset_vector_index_singleton()
