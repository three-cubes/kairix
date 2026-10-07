"""Adversarial round-trip corpus for ``EmbeddingCache.put_many`` → ``get_many``.

# F87-corpus: embedding_cache_put_get

:class:`kairix.core.embed.embedding_cache.EmbeddingCache` is the embed
pipeline's restart-resilient source of truth: every provider vector is
``put_many``-written before the vec index sees it, and a later run
``get_many``-reads it back instead of re-paying the provider. A lossy
round trip either re-burns provider spend (key mismatch → silent miss)
or corrupts the index (vector drift). This module sweeps the four
adversarial material classes F87 requires across the real SQLite
write → read boundary:

* multi-line — chunk text / cache keys carrying ``\\n`` / ``\\r\\n``;
* unicode — emoji AND CJK chunk text, keys and model names;
* large — a 64 KiB chunk text, a 64 KiB raw key, and a vector whose
  f32 BLOB is >= 64 KiB (16384 dims * 4 bytes = 65536 bytes);
* escape-lookalike — backslash sequences (``C:\\new\\path``, a literal
  ``\\n``) plus SQL-quote / LIKE-wildcard lookalikes (``'``, ``%``, ``_``)
  that must stay inert inside the parameterised IN clause.

Vectors are compared BIT-exact (``tobytes()``), including IEEE-754 edge
values (-0.0, subnormal, inf, NaN) that ``np.allclose`` would paper over.

Sabotage-proof (executed): changed ``_encode_vector`` to store
``float16`` bytes (and ``_decode_vector`` to read them back up-cast to
f32) — all 17 cases failed on the bit-exact comparison. Restored.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from kairix.core.embed.embedding_cache import EmbeddingCache, hash_chunk_text

pytestmark = pytest.mark.unit

_MODEL = "text-embedding-3-large"
_DIM = 8
_LARGE_DIM = (64 * 1024) // 4  # 16384 f32 values → a 65536-byte BLOB

_TEXT_CORPUS: dict[str, str] = {
    "lf": "first line\nsecond line\n",
    "crlf": "first line\r\nsecond line\r\n",
    "emoji-cjk": "鍵 🔑 世界 한국어 ✨",
    "large": "chunk body " * ((64 * 1024) // 11 + 1),
    "windows-path": "C:\\new\\path\\to\\doc.md",
    "literal-backslash-n": "keep\\nliteral",
    "sql-lookalike": "it's 100% a_b' OR '1'='1",
}

_KEY_CORPUS: dict[str, str] = {
    "multi-line-key": "hash\nwith\r\nbreaks",
    "unicode-key": "ハッシュ-🔑-世界",
    "large-key": "k" * (64 * 1024),
    "escape-lookalike-key": "C:\\new\\path\\n",
    "sql-lookalike-key": "x' OR '1'='1; --",
    "like-wildcard-key": "%_%",
}


def _vector(seed: int, dim: int) -> np.ndarray:
    return np.random.default_rng(seed).random(dim, dtype=np.float32)


def _assert_bit_exact(got: np.ndarray, expected: np.ndarray) -> None:
    assert got.dtype == np.float32
    assert got.tobytes() == np.asarray(expected, dtype=np.float32).tobytes()


@pytest.mark.parametrize("text", list(_TEXT_CORPUS.values()), ids=list(_TEXT_CORPUS))
def test_adversarial_chunk_text_hash_round_trips(tmp_path: Path, text: str) -> None:
    """The production key path: hash_chunk_text(text) → put_many → get_many."""
    cache = EmbeddingCache(tmp_path / "cache.sqlite")
    key = hash_chunk_text(text)
    vec = _vector(1, _DIM)
    assert cache.put_many(_MODEL, _DIM, [(key, vec)]) == 1
    # A fresh instance re-opens the file — the restart-resilience path.
    cache.close()
    reopened = EmbeddingCache(tmp_path / "cache.sqlite")
    got = reopened.get_many(_MODEL, _DIM, [hash_chunk_text(text)])
    assert set(got) == {key}
    _assert_bit_exact(got[key], vec)
    reopened.close()


@pytest.mark.parametrize("key", list(_KEY_CORPUS.values()), ids=list(_KEY_CORPUS))
def test_adversarial_raw_key_round_trips_and_stays_isolated(tmp_path: Path, key: str) -> None:
    """Raw keys of every adversarial shape hit exactly their own row."""
    cache = EmbeddingCache(tmp_path / "cache.sqlite")
    target = _vector(2, _DIM)
    decoy = _vector(3, _DIM)
    cache.put_many(_MODEL, _DIM, [(key, target), ("decoy-row", decoy)])
    got = cache.get_many(_MODEL, _DIM, [key])
    assert list(got) == [key]  # no wildcard / injection widening
    _assert_bit_exact(got[key], target)
    cache.close()


def test_unicode_multi_line_model_name_isolates_its_slice(tmp_path: Path) -> None:
    """Model names are part of the key — an adversarial one keeps its own slice."""
    cache = EmbeddingCache(tmp_path / "cache.sqlite")
    odd_model = "模型-🔬\nv2 C:\\models\\large"
    plain, odd = _vector(4, _DIM), _vector(5, _DIM)
    cache.put_many(_MODEL, _DIM, [("same-hash", plain)])
    cache.put_many(odd_model, _DIM, [("same-hash", odd)])
    _assert_bit_exact(cache.get_many(odd_model, _DIM, ["same-hash"])["same-hash"], odd)
    _assert_bit_exact(cache.get_many(_MODEL, _DIM, ["same-hash"])["same-hash"], plain)
    cache.close()


def test_large_vector_blob_round_trips_bit_exact(tmp_path: Path) -> None:
    """A >= 64 KiB f32 BLOB (65536 bytes) survives the SQLite round trip."""
    cache = EmbeddingCache(tmp_path / "cache.sqlite")
    vec = _vector(6, _LARGE_DIM)
    assert vec.nbytes >= 65536
    cache.put_many(_MODEL, _LARGE_DIM, [("large-vector", vec)])
    _assert_bit_exact(cache.get_many(_MODEL, _LARGE_DIM, ["large-vector"])["large-vector"], vec)
    cache.close()


def test_ieee754_edge_values_round_trip_bit_exact(tmp_path: Path) -> None:
    """-0.0, subnormal, extremes, inf and NaN keep their exact bit pattern."""
    cache = EmbeddingCache(tmp_path / "cache.sqlite")
    edge = np.array(
        [-0.0, np.float32(1e-45), np.finfo(np.float32).max, np.finfo(np.float32).min, np.inf, -np.inf, np.nan, 0.5],
        dtype=np.float32,
    )
    cache.put_many(_MODEL, edge.shape[0], [("edge", edge)])
    _assert_bit_exact(cache.get_many(_MODEL, edge.shape[0], ["edge"])["edge"], edge)
    cache.close()


def test_mixed_adversarial_batch_spans_in_clause_batches(tmp_path: Path) -> None:
    """Every adversarial key in one > 500-key read batch resolves to its own vector."""
    cache = EmbeddingCache(tmp_path / "cache.sqlite")
    keys = list(_KEY_CORPUS.values()) + [hash_chunk_text(t) for t in _TEXT_CORPUS.values()]
    keys += [f"filler-{i}" for i in range(600)]
    vectors = {key: _vector(i, _DIM) for i, key in enumerate(keys)}
    assert cache.put_many(_MODEL, _DIM, vectors.items()) == len(keys)
    got = cache.get_many(_MODEL, _DIM, keys)
    assert set(got) == set(keys)
    for key, vec in vectors.items():
        _assert_bit_exact(got[key], vec)
    cache.close()
