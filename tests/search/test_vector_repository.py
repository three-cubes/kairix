"""Tests for ``UsearchVectorRepository`` (search/vector_repository.py).

This module is a thin adapter that wraps ``VectorIndex`` behind the
``VectorRepository`` protocol. The repository owns three responsibilities:

  1. Convert the caller's Python ``list[float]`` query into a contiguous
     ``np.float32`` array before delegating to the underlying index.
  2. Short-circuit ``add_vectors([])`` so the index never sees an empty batch
     (avoids spurious "0 vectors added" logging / metric churn downstream).
  3. Unzip ``[(hash_seq, vector), ...]`` items into the parallel-list shape
     the underlying index expects.

Tests drive the repository through its public surface only — the underlying
index is replaced with a small in-test stand-in (``_RecordingIndex``) that
records the exact arguments the repository forwards. No ``@patch`` of kairix
internals (F1), no ``KAIRIX_*`` env monkeypatching (F2), no internal-name
imports (F5). The stand-in is a test double for the implicit ``VectorIndex``
interface and is intentionally local to this module.

Edge-case focus (per #197 DoD): empty-result search, malformed/dimension-
varied query vectors, and missing-collection filtering — these are the
branches where wrong-results-on-edge-case bugs hide.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from kairix.core.search.vector_repository import UsearchVectorRepository


class _RecordingIndex:
    """Minimal stand-in for ``VectorIndex`` that records calls.

    The repository depends only on ``search(vec, k, collections)``,
    ``add_vectors(hash_seqs, vectors)``, and ``__len__``. We record the
    arguments verbatim so tests can assert on the *exact* shape forwarded —
    including the dtype the repository converts to.
    """

    def __init__(
        self,
        *,
        search_results: list[dict[str, Any]] | None = None,
        add_return: int | None = None,
        length: int = 0,
    ) -> None:
        self._search_results = list(search_results or [])
        self._add_return = add_return
        self._length = length

        self.search_calls: list[tuple[np.ndarray, int, list[str] | None]] = []
        self.add_calls: list[tuple[list[str], list[list[float]]]] = []
        self.len_calls: int = 0

    def search(
        self,
        vec: np.ndarray,
        k: int,
        collections: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        self.search_calls.append((vec, k, collections))
        return list(self._search_results)

    def add_vectors(self, hash_seqs: list[str], vectors: list[list[float]]) -> int:
        self.add_calls.append((list(hash_seqs), list(vectors)))
        # Default: echo the count if the test didn't pin a specific value.
        return self._add_return if self._add_return is not None else len(hash_seqs)

    def __len__(self) -> int:
        self.len_calls += 1
        return self._length


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_converts_python_list_to_float32_numpy() -> None:
    """The repository must hand the index a contiguous float32 array.

    Drives the docstring claim that the repo is the conversion seam between
    callers (Python lists) and the index (numpy). A regression that forwarded
    the list verbatim would crash deep inside usearch with a confusing error;
    pinning the dtype at the boundary surfaces the bug here.
    """
    index = _RecordingIndex(search_results=[{"path": "a.md"}])
    repo = UsearchVectorRepository(index=index)

    out = repo.search([0.1, 0.2, 0.3], k=5)

    assert out == [{"path": "a.md"}]
    assert len(index.search_calls) == 1
    forwarded_vec, forwarded_k, forwarded_collections = index.search_calls[0]
    assert isinstance(forwarded_vec, np.ndarray)
    assert forwarded_vec.dtype == np.float32
    np.testing.assert_array_equal(forwarded_vec, np.array([0.1, 0.2, 0.3], dtype=np.float32))
    assert forwarded_k == 5
    assert forwarded_collections is None


@pytest.mark.unit
def test_search_returns_empty_list_when_index_returns_no_hits() -> None:
    """Empty-result handling: the repository must propagate ``[]`` unchanged.

    This is the wrong-results-on-edge-cases class (#197 DoD): some callers
    treat a missing key as zero hits rather than an error. If the repository
    silently swapped ``[]`` for ``None`` (or vice versa), every downstream
    fusion step would crash. Pin the contract here.
    """
    index = _RecordingIndex(search_results=[])
    repo = UsearchVectorRepository(index=index)

    out = repo.search([0.5] * 4, k=10)

    assert out == []


@pytest.mark.unit
def test_search_forwards_collection_filter_unchanged() -> None:
    """Per the protocol, ``collections=`` is forwarded verbatim — the
    repository does not pre-filter, dedupe, or sort the list."""
    index = _RecordingIndex(search_results=[{"path": "x"}])
    repo = UsearchVectorRepository(index=index)

    repo.search([0.0, 1.0], k=3, collections=["notes", "notes", "facts"])

    _, _, forwarded = index.search_calls[0]
    assert forwarded == ["notes", "notes", "facts"]


@pytest.mark.unit
def test_search_uses_default_k_of_10_when_unspecified() -> None:
    """Default ``k`` is part of the public contract — tighten it so an
    accidental change to ``k=20`` etc. trips a test instead of silently
    altering recall."""
    index = _RecordingIndex(search_results=[])
    repo = UsearchVectorRepository(index=index)

    repo.search([0.0, 1.0])

    _, forwarded_k, _ = index.search_calls[0]
    assert forwarded_k == 10


@pytest.mark.unit
def test_search_passes_empty_query_vector_through() -> None:
    """An empty query vector is malformed input from above. The repository
    is not the validation layer — it must forward to the index, which is
    where dimension checks live. Pinning this prevents the repository from
    growing implicit validation later (which would mask real errors)."""
    index = _RecordingIndex(search_results=[])
    repo = UsearchVectorRepository(index=index)

    repo.search([], k=1)

    forwarded_vec, _, _ = index.search_calls[0]
    assert isinstance(forwarded_vec, np.ndarray)
    assert forwarded_vec.shape == (0,)
    assert forwarded_vec.dtype == np.float32


# ---------------------------------------------------------------------------
# add_vectors()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_vectors_short_circuits_on_empty_input() -> None:
    """The repository must NOT delegate when items is empty.

    This is the contract documented in the docstring — empty input returns
    0 without ever calling the index. A regression here would log spurious
    "indexed 0 vectors" entries on every empty refresh and could mask a
    real partial-batch bug. The stand-in records calls, so any unexpected
    delegation will trip the assertion.
    """
    index = _RecordingIndex()
    repo = UsearchVectorRepository(index=index)

    n = repo.add_vectors([])

    assert n == 0
    assert index.add_calls == []  # never touched the index


@pytest.mark.unit
def test_add_vectors_unzips_items_into_parallel_lists() -> None:
    """The repository unzips ``[(hash, vec), ...]`` into ``([hashes], [vecs])``
    and forwards in parallel-list shape. Verify ordering is preserved (the
    underlying index relies on positional alignment)."""
    index = _RecordingIndex(add_return=3)
    repo = UsearchVectorRepository(index=index)

    n = repo.add_vectors(
        [
            ("hash_a", [1.0, 0.0]),
            ("hash_b", [0.0, 1.0]),
            ("hash_c", [0.5, 0.5]),
        ]
    )

    assert n == 3
    assert len(index.add_calls) == 1
    hash_seqs, vectors = index.add_calls[0]
    assert hash_seqs == ["hash_a", "hash_b", "hash_c"]
    assert vectors == [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]


@pytest.mark.unit
def test_add_vectors_returns_index_count_not_input_length() -> None:
    """The repository returns whatever the index returns — not ``len(items)``.

    A buggy implementation that returned the input length would mask
    partial-batch failures (e.g. usearch silently dropping a vector with
    a duplicate key). Sabotage-prove: pin the index to return a different
    count than the input length so we catch any "return len(items)"
    regression."""
    index = _RecordingIndex(add_return=1)  # claim only 1 was actually added
    repo = UsearchVectorRepository(index=index)

    n = repo.add_vectors([("a", [0.0]), ("b", [0.0]), ("c", [0.0])])

    assert n == 1


@pytest.mark.unit
def test_add_vectors_with_single_item_still_delegates() -> None:
    """Single-item batch is the smallest non-empty path — verify it does
    delegate (i.e. the empty-short-circuit isn't accidentally too eager)."""
    index = _RecordingIndex(add_return=1)
    repo = UsearchVectorRepository(index=index)

    n = repo.add_vectors([("only", [0.1, 0.2])])

    assert n == 1
    assert index.add_calls == [(["only"], [[0.1, 0.2]])]


# ---------------------------------------------------------------------------
# count()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_count_delegates_to_index_len() -> None:
    """``count()`` is a pure delegation to ``len(index)``."""
    index = _RecordingIndex(length=42)
    repo = UsearchVectorRepository(index=index)

    assert repo.count() == 42
    assert index.len_calls == 1


@pytest.mark.unit
def test_count_returns_zero_for_empty_index() -> None:
    """The empty-index case — common at first run before any vectors are
    indexed. The repository must not raise; downstream code uses
    ``count() == 0`` as the "needs first build" signal."""
    index = _RecordingIndex(length=0)
    repo = UsearchVectorRepository(index=index)

    assert repo.count() == 0
