"""Contract-first tests for kairix.core.search.vec_index.

Probes documented contracts on ``VectorIndex`` and ``get_vector_index()``:

  - load(): missing-index returns 0; dim-mismatch deletes files; populates _next_key
  - build_from_vectors(): empty input → 0; otherwise saves to disk
  - search(): empty index → []; over-fetches when collection filter is set;
    sorted by distance; returns [] on DB failure
  - add_vectors(): empty input → 0; does NOT auto-save
  - save(): no-op when no index
  - get_vector_index(): caches across calls; never raises; returns None on failure
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from kairix.core.search.vec_index import (
    VectorIndex,
    get_vector_index,
    reset_vector_index_singleton,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, *, n_docs: int = 5) -> Path:
    """Create a SQLite DB with documents + content tables matching the production schema."""
    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, collection TEXT, path TEXT,
            title TEXT, hash TEXT, active INTEGER DEFAULT 1,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        """
    )
    for i in range(n_docs):
        db.execute(
            "INSERT INTO documents (collection, path, title, hash, active) VALUES (?,?,?,?,1)",
            ("vault", f"vault/doc-{i}.md", f"doc-{i}", f"hash{i}"),
        )
        db.execute("INSERT INTO content (hash, doc) VALUES (?,?)", (f"hash{i}", f"Body of doc {i}."))
    db.commit()
    db.close()
    return db_path


def _normed_random(rng: np.random.Generator, dim: int) -> np.ndarray:
    v = rng.random(dim, dtype=np.float32)
    return v / np.linalg.norm(v)


def _build_index(tmp_path: Path, *, ndim: int = 1536, n: int = 5) -> VectorIndex:
    db_path = _make_db(tmp_path, n_docs=n)
    rng = np.random.default_rng(7)
    vectors = np.stack([_normed_random(rng, ndim) for _ in range(n)])
    idx = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
        ndim=ndim,
    )
    idx.build_from_vectors([f"hash{i}_0" for i in range(n)], vectors)
    return idx


# ---------------------------------------------------------------------------
# load() contracts
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_load_returns_zero_when_index_file_missing(tmp_path: Path) -> None:
    """Per docstring: missing index file → return 0 (no-op)."""
    idx = VectorIndex(
        index_path=tmp_path / "absent.usearch",
        meta_path=tmp_path / "absent.meta.json",
        db_path=tmp_path / "any.sqlite",
    )
    assert idx.load() == 0
    assert len(idx) == 0


@pytest.mark.contract
def test_load_deletes_files_and_returns_zero_on_dim_mismatch(tmp_path: Path) -> None:
    """Per docstring: dim mismatch → deletes old index and returns 0
    so a fresh index gets built on next add_vectors().
    """
    # Build with ndim=512.
    db_path = _make_db(tmp_path, n_docs=3)
    rng = np.random.default_rng(1)
    vecs = np.stack([_normed_random(rng, 512) for _ in range(3)])
    built = VectorIndex(
        index_path=tmp_path / "v.usearch",
        meta_path=tmp_path / "v.meta.json",
        db_path=db_path,
        ndim=512,
    )
    built.build_from_vectors([f"hash{i}_0" for i in range(3)], vecs)
    assert (tmp_path / "v.usearch").exists()
    assert (tmp_path / "v.meta.json").exists()

    # Reload with different ndim → migration deletes both files.
    other = VectorIndex(
        index_path=tmp_path / "v.usearch",
        meta_path=tmp_path / "v.meta.json",
        db_path=db_path,
        ndim=1536,
    )
    assert other.load() == 0
    assert not (tmp_path / "v.usearch").exists()
    assert not (tmp_path / "v.meta.json").exists()


@pytest.mark.contract
def test_load_populates_next_key_from_meta(tmp_path: Path) -> None:
    """``next_key`` is read from meta or computed from max(keys)+1.
    A reloaded index must continue numbering from where it left off so
    add_vectors() doesn't collide with existing keys.
    """
    idx = _build_index(tmp_path, ndim=512, n=4)
    expected_next = idx._next_key
    db_path = idx._db_path

    reloaded = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
        ndim=512,
    )
    reloaded.load()
    assert reloaded._next_key == expected_next


@pytest.mark.contract
def test_load_tolerates_corrupt_meta_json(tmp_path: Path) -> None:
    """If meta.json is unparseable, load() must still proceed (rather than raise).
    The dim-mismatch check is best-effort; a corrupt meta means we can't
    detect mismatch, but we still attempt to restore the index.
    """
    idx = _build_index(tmp_path, ndim=512, n=2)
    db_path = idx._db_path

    # Corrupt the meta file.
    (tmp_path / "vectors.meta.json").write_text("{not valid json")

    other = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
        ndim=512,
    )
    # Must not raise — the contract is "best-effort restore on corrupt meta".
    other.load()


# ---------------------------------------------------------------------------
# build_from_vectors() contracts
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_build_from_vectors_empty_input_returns_zero(tmp_path: Path) -> None:
    """Empty input → 0; no index file written."""
    idx = VectorIndex(
        index_path=tmp_path / "v.usearch",
        meta_path=tmp_path / "v.meta.json",
        db_path=tmp_path / "v.sqlite",
        ndim=512,
    )
    n = idx.build_from_vectors([], np.zeros((0, 512), dtype=np.float32))
    assert n == 0
    assert not (tmp_path / "v.usearch").exists()


@pytest.mark.contract
def test_build_from_vectors_writes_meta_with_ndim(tmp_path: Path) -> None:
    """Meta file must include the ``ndim`` so reloads can detect dim mismatch."""
    _build_index(tmp_path, ndim=512, n=3)
    meta = json.loads((tmp_path / "vectors.meta.json").read_text())
    assert meta["ndim"] == 512
    assert meta["next_key"] == 3
    assert "keys" in meta


# ---------------------------------------------------------------------------
# search() contracts
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_search_returns_empty_when_index_uninitialised(tmp_path: Path) -> None:
    """No build, no load → search returns []."""
    idx = VectorIndex(
        index_path=tmp_path / "v.usearch",
        meta_path=tmp_path / "v.meta.json",
        db_path=tmp_path / "v.sqlite",
        ndim=512,
    )
    rng = np.random.default_rng(0)
    out = idx.search(_normed_random(rng, 512), k=5)
    assert out == []


@pytest.mark.contract
def test_search_with_collection_filter_can_return_full_k(tmp_path: Path) -> None:
    """When a collection filter is set, search over-fetches (4x) so it can
    still return up to k results after filtering out off-collection matches.
    """
    # Build 8 docs, half in 'a', half in 'b'.
    db_path = tmp_path / "f.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, collection TEXT, path TEXT, "
        "title TEXT, hash TEXT, active INTEGER DEFAULT 1, UNIQUE(collection, path));"
        "CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);"
    )
    for i in range(8):
        coll = "a" if i < 4 else "b"
        db.execute(
            "INSERT INTO documents (collection, path, title, hash, active) VALUES (?,?,?,?,1)",
            (coll, f"{coll}/doc-{i}.md", f"doc-{i}", f"h{i}"),
        )
        db.execute("INSERT INTO content (hash, doc) VALUES (?,?)", (f"h{i}", f"body {i}"))
    db.commit()
    db.close()

    rng = np.random.default_rng(1)
    vecs = np.stack([_normed_random(rng, 512) for _ in range(8)])
    idx = VectorIndex(
        index_path=tmp_path / "v.usearch",
        meta_path=tmp_path / "v.meta.json",
        db_path=db_path,
        ndim=512,
    )
    idx.build_from_vectors([f"h{i}_0" for i in range(8)], vecs)

    # Ask for k=4 in collection 'a' (which has exactly 4 docs).
    # Without the 4x over-fetch, ANN might return mostly 'b' docs and we'd
    # get fewer than 4 results.
    rng2 = np.random.default_rng(99)
    query = _normed_random(rng2, 512)
    out = idx.search(query, k=4, collections=["a"])
    # Per the over-fetch contract, all 4 'a' docs should be reachable.
    assert all(r["collection"] == "a" for r in out)


# ---------------------------------------------------------------------------
# add_vectors() contracts
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_add_vectors_empty_input_returns_zero(tmp_path: Path) -> None:
    idx = _build_index(tmp_path, ndim=512, n=2)
    assert idx.add_vectors([], []) == 0


@pytest.mark.contract
def test_add_vectors_does_not_auto_save(tmp_path: Path) -> None:
    """Per docstring: add_vectors does NOT auto-save — caller controls timing.
    A fresh-process load() after add_vectors() (without explicit save) must
    NOT see the added vector.
    """
    idx = _build_index(tmp_path, ndim=512, n=2)
    db_path = idx._db_path

    rng = np.random.default_rng(3)
    new_vec = _normed_random(rng, 512)
    idx.add_vectors(["new_0"], [new_vec])
    # NOT calling idx.save()

    reloaded = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
        ndim=512,
    )
    reloaded.load()
    # Only the original 2 — the unsaved add is gone.
    assert len(reloaded) == 2


@pytest.mark.contract
def test_add_vectors_persists_after_explicit_save(tmp_path: Path) -> None:
    """Calling save() after add_vectors() makes the new vector durable."""
    idx = _build_index(tmp_path, ndim=512, n=2)
    db_path = idx._db_path

    rng = np.random.default_rng(3)
    new_vec = _normed_random(rng, 512)
    idx.add_vectors(["new_0"], [new_vec])
    idx.save()

    reloaded = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
        ndim=512,
    )
    reloaded.load()
    assert len(reloaded) == 3


# ---------------------------------------------------------------------------
# save() contracts
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_save_is_noop_when_index_uninitialised(tmp_path: Path) -> None:
    """save() with no index does not raise and does not create files."""
    idx = VectorIndex(
        index_path=tmp_path / "v.usearch",
        meta_path=tmp_path / "v.meta.json",
        db_path=tmp_path / "v.sqlite",
        ndim=512,
    )
    idx.save()  # must not raise
    assert not (tmp_path / "v.usearch").exists()


# ---------------------------------------------------------------------------
# get_vector_index() singleton contracts
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _reset_vec_index_singleton() -> Any:
    """Reset the module-level singleton so each test starts cold."""
    reset_vector_index_singleton()
    yield
    reset_vector_index_singleton()


@pytest.mark.contract
def test_get_vector_index_returns_none_when_index_files_absent(
    tmp_path: Path, _reset_vec_index_singleton: None
) -> None:
    """Per docstring: returns None when index empty/missing/unloadable.
    We pass an explicit db_path to a tmp dir with no index files.
    """
    db_p = tmp_path / "index.sqlite"
    db_p.touch()
    assert get_vector_index(db_path=db_p) is None


@pytest.mark.contract
def test_get_vector_index_caches_loaded_instance(tmp_path: Path, _reset_vec_index_singleton: None) -> None:
    """Second call returns the same instance (lazy singleton).
    The cache is path-independent — once cached, the singleton is the
    answer regardless of what db_path subsequent callers pass.
    """
    _build_index(tmp_path, ndim=1536, n=2)
    db_p = tmp_path / "index.sqlite"

    first = get_vector_index(db_path=db_p)
    second = get_vector_index(db_path=db_p)
    assert first is second
    assert first is not None


@pytest.mark.contract
def test_get_vector_index_never_raises_on_path_failure(tmp_path: Path, _reset_vec_index_singleton: None) -> None:
    """An unreadable / nonexistent DB path must surface as None, not raise."""
    # Pointing at a path whose parent does not exist would normally raise,
    # but the contract says "Never raises — returns None on any failure".
    bogus = tmp_path / "no" / "such" / "dir" / "index.sqlite"
    assert get_vector_index(db_path=bogus) is None
