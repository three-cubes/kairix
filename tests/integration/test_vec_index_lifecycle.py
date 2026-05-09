"""Integration tests for kairix.core.search.vec_index.

Exercises the full filesystem-backed lifecycle of the usearch index:
  - build → save → reload (cross-process)
  - add → save → reload preserves prior + new vectors
  - dimension migration: changing ndim deletes the old index
  - get_vector_index() singleton across two callers in one process

Each test owns its own tmp_path, a real SQLite DB with the production
schema, and a real usearch index file.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from kairix.core.search.vec_index import (
    VectorIndex,
    get_vector_index,
    reset_vector_index_singleton,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, *, n_docs: int = 5) -> Path:
    """Create a SQLite DB matching the production schema (documents+content)."""
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
        db.execute("INSERT INTO content (hash, doc) VALUES (?,?)", (f"hash{i}", f"Body {i}."))
    db.commit()
    db.close()
    return db_path


def _normed(rng: np.random.Generator, dim: int) -> np.ndarray:
    v = rng.random(dim, dtype=np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    """Each integration test starts with a fresh singleton."""
    reset_vector_index_singleton()
    yield
    reset_vector_index_singleton()


# ---------------------------------------------------------------------------
# Lifecycle: build → save → reload
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_build_save_reload_roundtrip_preserves_vectors_and_metadata(tmp_path: Path) -> None:
    """An index built and saved in one VectorIndex instance must be searchable
    from a fresh instance with the same paths — proving the file format is
    stable across instances.
    """
    db_path = _make_db(tmp_path, n_docs=5)
    rng = np.random.default_rng(42)
    vectors = np.stack([_normed(rng, 1536) for _ in range(5)])

    # Build the first instance and persist.
    builder = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )
    n = builder.build_from_vectors([f"hash{i}_0" for i in range(5)], vectors)
    assert n == 5

    # Reload from a fresh instance and verify it sees the same data.
    reader = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )
    assert reader.load() == 5

    # Searching with a vector identical to one of the inputs should retrieve
    # that document with near-zero distance.
    target = vectors[2]
    results = reader.search(target, k=1)
    assert len(results) == 1
    assert results[0]["distance"] < 0.01
    assert results[0]["path"] == "vault/doc-2.md"


@pytest.mark.integration
def test_add_then_save_persists_added_vectors_across_reload(tmp_path: Path) -> None:
    """Incrementally added vectors must survive a save/reload cycle."""
    db_path = _make_db(tmp_path, n_docs=4)
    rng = np.random.default_rng(11)
    base_vectors = np.stack([_normed(rng, 1536) for _ in range(2)])

    idx = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )
    idx.build_from_vectors(["hash0_0", "hash1_0"], base_vectors)
    # Two more vectors added after the initial build.
    extra_vec = _normed(rng, 1536)
    idx.add_vectors(["hash2_0"], [extra_vec])
    idx.add_vectors(["hash3_0"], [_normed(rng, 1536)])
    idx.save()

    reloaded = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )
    assert reloaded.load() == 4

    # The exact added vector for hash2_0 should be retrievable.
    results = reloaded.search(extra_vec, k=1)
    assert len(results) == 1
    assert results[0]["path"] == "vault/doc-2.md"


@pytest.mark.integration
def test_dimension_migration_deletes_old_index_files(tmp_path: Path) -> None:
    """When the configured ndim differs from the stored ndim, load() must
    delete the old index files so the next build starts clean.
    """
    db_path = _make_db(tmp_path, n_docs=2)
    rng = np.random.default_rng(7)

    # Stage 1: build at ndim=512.
    small = VectorIndex(
        index_path=tmp_path / "v.usearch",
        meta_path=tmp_path / "v.meta.json",
        db_path=db_path,
        ndim=512,
    )
    small.build_from_vectors(["hash0_0", "hash1_0"], np.stack([_normed(rng, 512), _normed(rng, 512)]))
    assert (tmp_path / "v.usearch").exists()
    assert (tmp_path / "v.meta.json").exists()

    # Stage 2: re-instantiate at ndim=1536 and load. Migration kicks in.
    big = VectorIndex(
        index_path=tmp_path / "v.usearch",
        meta_path=tmp_path / "v.meta.json",
        db_path=db_path,
        ndim=1536,
    )
    assert big.load() == 0
    assert not (tmp_path / "v.usearch").exists()
    assert not (tmp_path / "v.meta.json").exists()

    # Stage 3: a fresh build at the new ndim succeeds.
    new_vec = _normed(rng, 1536)
    n = big.build_from_vectors(["hash0_0"], np.stack([new_vec]))
    assert n == 1
    assert (tmp_path / "v.usearch").exists()


# ---------------------------------------------------------------------------
# get_vector_index() singleton lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_vector_index_returns_loaded_index_for_explicit_db_path(tmp_path: Path) -> None:
    """The factory function loads a real on-disk index when given a db_path
    pointing at a directory with the canonical vectors files.
    """
    db_path = _make_db(tmp_path, n_docs=3)
    rng = np.random.default_rng(0)
    vectors = np.stack([_normed(rng, 1536) for _ in range(3)])

    # Pre-populate the canonical vector files alongside db_path.
    seed = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )
    seed.build_from_vectors(["hash0_0", "hash1_0", "hash2_0"], vectors)

    # Factory call resolves the index from the same directory.
    idx = get_vector_index(db_path=db_path)
    assert idx is not None
    assert len(idx) == 3


@pytest.mark.integration
def test_get_vector_index_singleton_persists_within_process(tmp_path: Path) -> None:
    """Once loaded, the singleton stays cached even when the on-disk file
    is deleted out from under it (the in-memory index continues to serve
    until the process restarts).
    """
    db_path = _make_db(tmp_path, n_docs=2)
    rng = np.random.default_rng(0)
    seed = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )
    seed.build_from_vectors(["hash0_0", "hash1_0"], np.stack([_normed(rng, 1536), _normed(rng, 1536)]))

    first = get_vector_index(db_path=db_path)
    # Even if the on-disk file is removed, the singleton keeps serving.
    (tmp_path / "vectors.usearch").unlink()
    second = get_vector_index(db_path=db_path)
    assert first is second
