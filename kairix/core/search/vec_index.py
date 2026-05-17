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

# Maximum number of placeholders to put in a single SQLite ``IN (...)`` clause.
# Older SQLite builds cap parameter count at 999; newer builds at 32 766. We
# pick 500 conservatively — this lets the batched fetch span ~25x the
# default ``k`` before we have to issue a second query. Defensive only:
# typical ANN searches surface ≤20 results.
_IN_CLAUSE_BATCH_SIZE: int = 500

# Single SELECT body for the batched metadata lookup. Lifted to a constant
# so we don't duplicate the JOIN text per chunk (F17).
_METADATA_SELECT_SQL: str = (
    "SELECT d.hash, d.path, d.collection, d.title, COALESCE(c.doc, '') AS snippet "
    "FROM documents d LEFT JOIN content c ON d.hash = c.hash "
    "WHERE d.active = 1 AND d.hash IN ({placeholders})"
)


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

        # Parse meta ONCE — the file can be 14 MB+ on a fully-indexed corpus.
        # The dimension check and the key-mapping load both used to call
        # json.loads independently, doubling the warm-up cost.
        meta: dict[str, Any] | None = None
        if self._meta_path.exists():
            try:
                meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("vec_index: meta unreadable — index loaded without key mapping (%s)", e)
                meta = None

        if meta is not None:
            stored_ndim = meta.get("ndim", 0)
            if stored_ndim and stored_ndim != self._ndim:
                logger.warning(
                    "vec_index: dimension mismatch (index=%d, expected=%d) — deleting old index",
                    stored_ndim,
                    self._ndim,
                )
                self._delete_index_files()
                return 0

        self._index = Index.restore(str(self._index_path), view=True)
        if meta is not None:
            try:
                self._key_to_hash_seq = {int(k): v for k, v in meta["keys"].items()}
                self._next_key = meta.get("next_key", max(self._key_to_hash_seq.keys(), default=-1) + 1)
            except (KeyError, ValueError) as e:
                logger.warning("vec_index: meta missing 'keys' — index loaded without key mapping (%s)", e)
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
        """Resolve ANN match keys to document metadata via a batched SQLite query.

        Issues ONE multi-row SELECT against ``documents``/``content`` using an
        ``IN (?, ?, ...)`` clause, then zips the rows back to the input order
        in Python. Reduces per-search SQLite journal-lock acquisitions from
        ``k x N_threads`` (one per ANN hit) to ``ceil(k / batch) x N_threads``
        — typically one — and dropped vector_ann mean latency from ~440 ms to
        ~30-50 ms at conc=10 in profiling (#287).

        Correctness invariants preserved:

        * ``d.active = 1`` filter
        * Collection filter (in-Python after the fetch, mirroring the row
          filter the old per-row loop applied)
        * Result ordering follows the ANN ranking in ``matches.keys`` — NOT
          the SQL row order, which is undefined in SQLite without ORDER BY
        * Frontmatter stripping on the snippet
        * ``k`` cap on the returned list
        * ``[]`` on DB failure

        Returns list of VecResult-compatible dicts. Returns [] on DB failure
        or when ``matches`` carries no keys.
        """
        ordered = self._ordered_content_hashes(matches)
        if not ordered:
            return []

        unique_hashes = list({content_hash for _, _, content_hash in ordered})
        try:
            rows_by_hash = self._fetch_metadata_batched(unique_hashes)
        except (sqlite3.Error, OSError) as e:
            logger.warning("vec_index: metadata lookup failed — %s", e)
            return []

        return self._build_results(ordered, rows_by_hash, k, collections)

    def _ordered_content_hashes(self, matches: Any) -> list[tuple[str, float, str]]:
        """Flatten ANN matches into ``(hash_seq, distance, content_hash)`` tuples.

        Drops keys with no mapping in ``self._key_to_hash_seq`` (same skip
        as the pre-batch implementation). Order follows ``matches.keys`` so
        downstream code can rely on ANN ranking.
        """
        ordered: list[tuple[str, float, str]] = []
        for key, distance in zip(matches.keys, matches.distances, strict=True):
            hash_seq = self._key_to_hash_seq.get(int(key))
            if hash_seq is None:
                continue
            content_hash = hash_seq.rsplit("_", 1)[0]
            ordered.append((hash_seq, float(distance), content_hash))
        return ordered

    def _fetch_metadata_batched(self, unique_hashes: list[str]) -> dict[str, sqlite3.Row]:
        """Fetch all metadata rows for ``unique_hashes`` in batches.

        Defensive: SQLite caps placeholders at 999 (older) or 32 766
        (newer). We chunk at :data:`_IN_CLAUSE_BATCH_SIZE` so absurd
        ``k`` values still work. The common path (k ≤ 20) issues a
        single query.
        """
        rows_by_hash: dict[str, sqlite3.Row] = {}
        db = open_db(Path(self._db_path))
        try:
            db.row_factory = sqlite3.Row
            for start in range(0, len(unique_hashes), _IN_CLAUSE_BATCH_SIZE):
                chunk = unique_hashes[start : start + _IN_CLAUSE_BATCH_SIZE]
                placeholders = ",".join("?" * len(chunk))
                sql = _METADATA_SELECT_SQL.format(placeholders=placeholders)
                for row in db.execute(sql, tuple(chunk)).fetchall():
                    rows_by_hash[row["hash"]] = row
        finally:
            db.close()
        return rows_by_hash

    def _build_results(
        self,
        ordered: list[tuple[str, float, str]],
        rows_by_hash: dict[str, sqlite3.Row],
        k: int,
        collections: list[str] | None,
    ) -> list[dict]:
        """Zip ordered ANN hits with the batched rows; apply filters and ``k`` cap."""
        results: list[dict] = []
        for hash_seq, distance, content_hash in ordered:
            row = rows_by_hash.get(content_hash)
            if row is None:
                continue
            if collections and row["collection"] not in collections:
                continue
            snippet_raw = row["snippet"]
            snippet = strip_frontmatter(snippet_raw)[:300] if snippet_raw else ""
            results.append(
                {
                    "hash_seq": hash_seq,
                    "distance": distance,
                    "path": row["path"],
                    "collection": row["collection"],
                    "title": row["title"],
                    "snippet": snippet,
                }
            )
            if len(results) >= k:
                break
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


def get_vector_index(db_path: Path | None = None) -> Any:
    """Lazily load the usearch VectorIndex singleton.

    Args:
        db_path: SQLite index path. The vector files (``vectors.usearch``
                 and ``vectors.meta.json``) are expected in the same
                 directory. Defaults to ``kairix.paths.db_path()`` for
                 production use; tests pass an explicit path.

    Returns the loaded index, or None if the index is empty/missing/unloadable.
    Subsequent calls return the cached instance.
    Never raises — returns None on any failure.
    """
    global _VECTOR_INDEX
    if _VECTOR_INDEX is not None:
        return _VECTOR_INDEX
    try:
        if db_path is None:
            from kairix.paths import db_path as _resolve_db_path

            db_path = _resolve_db_path()
        index_path = db_path.parent / "vectors.usearch"
        meta_path = db_path.parent / "vectors.meta.json"
        idx = VectorIndex(index_path=index_path, meta_path=meta_path, db_path=db_path)
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


def reset_vector_index_singleton() -> None:
    """Clear the cached singleton. For test isolation."""
    global _VECTOR_INDEX
    _VECTOR_INDEX = None
