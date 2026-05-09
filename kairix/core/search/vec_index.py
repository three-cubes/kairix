"""usearch-backed ANN vector index for kairix.

usearch HNSW ANN index for
sub-10ms vector search at 50K+ vectors. Memory-mapped persistence
means near-zero RAM for read workloads.

The index file lives alongside index.sqlite:
  ~/.cache/kairix/vectors.usearch  (HNSW index)
  ~/.cache/kairix/vectors.meta.json (key → hash_seq mapping)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, TypedDict

import numpy as np

from kairix.core.db import EMBED_VECTOR_DIMS, open_db
from kairix.text import strip_frontmatter

logger = logging.getLogger(__name__)

# Default dimensions — reads KAIRIX_EMBED_DIMS env var (default 1536)
DIMS = EMBED_VECTOR_DIMS

# Default number of vector results to retrieve before fusion
VECTOR_DEFAULT_K: int = 20


class VecResult(TypedDict):
    """Single vector search result."""

    hash_seq: str
    distance: float
    path: str
    collection: str
    title: str
    snippet: str


class VectorIndex:
    """usearch-backed ANN index with collection-scoped search."""

    def __init__(
        self,
        index_path: Path,
        meta_path: Path,
        db_path: Path,
        ndim: int = DIMS,
    ) -> None:
        self._index_path = Path(index_path)
        self._meta_path = Path(meta_path)
        self._db_path = Path(db_path)
        self._ndim = ndim
        self._index: Any = None
        self._key_to_hash_seq: dict[int, str] = {}
        self._next_key: int = 0
        self._mutable: bool = False

    def __len__(self) -> int:
        if self._index is None:
            return 0
        return len(self._index)

    def load(self) -> int:
        """Load existing usearch index + metadata from disk.

        If the index was built with different dimensions, deletes it and
        returns 0 so a fresh index is created on the next add_vectors() call.
        """
        from usearch.index import Index

        if not self._index_path.exists():
            return 0

        # Check metadata for dimension mismatch before loading
        if self._meta_path.exists():
            try:
                meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
                stored_ndim = meta.get("ndim", 0)
                if stored_ndim and stored_ndim != self._ndim:
                    logger.warning(
                        "vec_index: dimension mismatch (index=%d, expected=%d) — deleting old index",
                        stored_ndim,
                        self._ndim,
                    )
                    self._delete_index_files()
                    return 0
            except (json.JSONDecodeError, OSError):
                pass

        self._index = Index.restore(str(self._index_path), view=True)
        if self._meta_path.exists():
            meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            self._key_to_hash_seq = {int(k): v for k, v in meta["keys"].items()}
            self._next_key = meta.get("next_key", max(self._key_to_hash_seq.keys(), default=-1) + 1)
        return len(self._index)

    def _delete_index_files(self) -> None:
        """Remove index and metadata files from disk."""
        for path in (self._index_path, self._meta_path):
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("vec_index: failed to delete %s — %s", path, e)
        self._index = None
        self._key_to_hash_seq = {}
        self._next_key = 0

    def build_from_vectors(self, hash_seqs: list[str], vectors: np.ndarray) -> int:
        """Build a new index from provided vectors. Saves to disk."""
        from usearch.index import Index

        n = len(hash_seqs)
        if n == 0:
            return 0
        self._index = Index(ndim=self._ndim, metric="cos", dtype="f32")
        keys = np.arange(n, dtype=np.int64)
        self._index.add(keys, vectors)
        self._key_to_hash_seq = {int(k): hs for k, hs in zip(keys, hash_seqs, strict=True)}
        self._next_key = n
        self._save()
        return n

    def _resolve_match_metadata(
        self,
        matches: Any,
        k: int,
        collections: list[str] | None,
    ) -> list[dict]:
        """Resolve ANN match keys to document metadata via SQLite.

        Returns list of VecResult-compatible dicts. Returns [] on DB failure.
        """
        results: list[dict] = []
        try:
            db = open_db(Path(self._db_path))
            db.row_factory = sqlite3.Row
            for key, distance in zip(matches.keys, matches.distances, strict=True):
                hash_seq = self._key_to_hash_seq.get(int(key))
                if hash_seq is None:
                    continue
                content_hash = hash_seq.rsplit("_", 1)[0]
                row = db.execute(
                    "SELECT d.path, d.collection, d.title, COALESCE(c.doc, '') AS snippet "
                    "FROM documents d LEFT JOIN content c ON d.hash = c.hash "
                    "WHERE d.hash = ? AND d.active = 1 LIMIT 1",
                    (content_hash,),
                ).fetchone()
                if row is None:
                    continue
                if collections and row["collection"] not in collections:
                    continue
                results.append(
                    {
                        "hash_seq": hash_seq,
                        "distance": float(distance),
                        "path": row["path"],
                        "collection": row["collection"],
                        "title": row["title"],
                        "snippet": (strip_frontmatter(row["snippet"])[:300] if row["snippet"] else ""),
                    }
                )
                if len(results) >= k:
                    break
            db.close()
        except (sqlite3.Error, OSError) as e:
            logger.warning("vec_index: metadata lookup failed — %s", e)

        return results

    def search(
        self,
        query_vec: np.ndarray,
        k: int = 10,
        collections: list[str] | None = None,
    ) -> list[dict]:
        """ANN search with optional collection filtering.

        Returns list of VecResult-compatible dicts sorted by distance.
        """
        if self._index is None or len(self._index) == 0:
            return []

        fetch_k = min(k * 4 if collections else k, len(self._index))
        matches = self._index.search(query_vec.astype(np.float32), fetch_k)

        return self._resolve_match_metadata(matches, k, collections)

    def _ensure_mutable(self) -> None:
        """Ensure the index is mutable (not a read-only mmap view).

        usearch Index.restore(view=True) creates an immutable memory-mapped
        index. To add vectors we need a mutable copy. This rebuilds the
        index from the existing vectors when needed. Subsequent calls are
        a no-op once the index has been converted.
        """
        from usearch.index import Index

        if self._mutable:
            return

        if self._index is None:
            self._index = Index(ndim=self._ndim, metric="cos", dtype="f32")
            self._mutable = True
            return

        # Check if the index is immutable by attempting a dummy operation
        try:
            test_key = np.array([self._next_key], dtype=np.int64)
            test_vec = np.zeros((1, self._ndim), dtype=np.float32)
            self._index.add(test_key, test_vec)
            self._index.remove(test_key)
            self._mutable = True
        except Exception:
            # Index is immutable — rebuild as mutable
            logger.info(
                "vec_index: converting immutable index to mutable (%d vectors)",
                len(self._index),
            )
            old_keys = np.array(list(self._key_to_hash_seq.keys()), dtype=np.int64)
            old_vecs = np.array([self._index[k] for k in old_keys], dtype=np.float32)
            self._index = Index(ndim=self._ndim, metric="cos", dtype="f32")
            if len(old_keys) > 0:
                self._index.add(old_keys, old_vecs)
            self._mutable = True

    def add_vectors(self, hash_seqs: list[str], vectors: list[list[float]]) -> int:
        """Add new vectors incrementally. Does NOT auto-save — caller controls save timing."""
        if not hash_seqs:
            return 0
        self._ensure_mutable()

        arr = np.array(vectors, dtype=np.float32)
        keys = np.arange(self._next_key, self._next_key + len(hash_seqs), dtype=np.int64)
        self._index.add(keys, arr)
        for k, hs in zip(keys, hash_seqs, strict=True):
            self._key_to_hash_seq[int(k)] = hs
        self._next_key += len(hash_seqs)
        return len(hash_seqs)

    def save(self) -> None:
        """Save index and metadata to disk. Public wrapper for callers."""
        self._save()

    def _save(self) -> None:
        """Save index and metadata to disk."""
        if self._index is None:
            return
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._index.save(str(self._index_path))
        meta = {
            "keys": {str(k): v for k, v in self._key_to_hash_seq.items()},
            "next_key": self._next_key,
            "ndim": self._ndim,
        }
        self._meta_path.write_text(json.dumps(meta), encoding="utf-8")


# ---------------------------------------------------------------------------
# Process-singleton accessor
# ---------------------------------------------------------------------------

_VECTOR_INDEX: Any = None


def get_vector_index() -> Any:
    """Lazily load the usearch VectorIndex singleton from the canonical paths.

    Returns the loaded index, or None if the index is empty/missing/unloadable.
    Subsequent calls return the cached instance.
    Never raises — returns None on any failure.
    """
    global _VECTOR_INDEX
    if _VECTOR_INDEX is not None:
        return _VECTOR_INDEX
    try:
        from kairix.paths import db_path as get_db_path

        db_p = get_db_path()
        index_path = db_p.parent / "vectors.usearch"
        meta_path = db_p.parent / "vectors.meta.json"
        idx = VectorIndex(index_path=index_path, meta_path=meta_path, db_path=db_p)
        count = idx.load()
        if count > 0:
            logger.info("vec_index: loaded usearch index (%d vectors)", count)
            _VECTOR_INDEX = idx
            return idx
        logger.warning("vec_index: usearch index empty or missing at %s", index_path)
        return None
    except Exception as e:
        logger.warning("vec_index: failed to load usearch index — %s", e)
        return None
