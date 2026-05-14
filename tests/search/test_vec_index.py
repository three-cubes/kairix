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
