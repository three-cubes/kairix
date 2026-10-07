"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`VectorRepository`.

Three methods on the vector index surface. Each is exercised below
for at least one named failure class:

  * ``search`` — raises on backend failure (vec backend unreachable /
    corrupt) AND returns_empty when the index has no matching vectors
    for the collections filter.
  * ``add_vectors`` — raises when the underlying index rejects the
    batch (typed exception, not silent zero-rows).
  * ``count`` — raises when the backing index handle is unavailable.

F43 parity: every body runs over the real
:class:`~kairix.core.search.vector_repository.UsearchVectorRepository`
AND the canonical :class:`tests.fakes.FakeVectorRepository`. Failures
are injected into the real repository through its ``index=`` seam
(:class:`tests.fakes.FakeVectorIndex` with a raising method); the
returns-empty leg drives a REAL usearch-backed
:class:`~kairix.core.search.vec_index.VectorIndex` over a tmp SQLite
metadata DB.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from kairix.core.db import open_db
from kairix.core.db.schema import create_schema
from kairix.core.protocols import VectorRepository
from kairix.core.search.vec_index import VectorIndex
from kairix.core.search.vector_repository import UsearchVectorRepository
from tests.fakes import FakeVectorIndex, FakeVectorRepository

pytestmark = pytest.mark.contract

_NDIM = 4
_QUERY = [0.1, 0.2, 0.3, 0.4]


def _real_over_failing_index(**raises: BaseException) -> VectorRepository:
    return UsearchVectorRepository(index=FakeVectorIndex(**raises))  # type: ignore[arg-type]  # FakeVectorIndex is the structural stand-in for the VectorIndex seam


# Each factory takes the failure to inject and returns a repository whose
# matching method raises it — real via the index seam, fake via its knob.
_SEARCH_RAISES: list[Callable[[BaseException], VectorRepository]] = [
    lambda exc: _real_over_failing_index(search_raises=exc),
    lambda exc: FakeVectorRepository(raises=exc),
]
_ADD_RAISES: list[Callable[[BaseException], VectorRepository]] = [
    lambda exc: _real_over_failing_index(add_raises=exc),
    lambda exc: FakeVectorRepository(add_raises=exc),
]
_COUNT_RAISES: list[Callable[[BaseException], VectorRepository]] = [
    lambda exc: _real_over_failing_index(len_raises=exc),
    lambda exc: FakeVectorRepository(count_raises=exc),
]


@pytest.fixture(params=["real", "fake"])
def alpha_repo(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[VectorRepository]:
    """A repository holding exactly one vector, for a doc in collection ``alpha``."""
    if request.param == "fake":
        yield FakeVectorRepository(results=[{"path": "a.md", "collection": "alpha"}])
        return
    db_path = tmp_path / "index.sqlite"
    db = open_db(db_path)
    create_schema(db)
    db.execute(
        "INSERT INTO documents (collection, path, title, hash, active) VALUES (?,?,?,?,1)",
        ("alpha", "a.md", "a", "hash-a"),
    )
    db.execute("INSERT INTO content (hash, doc) VALUES (?,?)", ("hash-a", "alpha document body"))
    db.commit()
    db.close()
    index = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
        ndim=_NDIM,
    )
    repo = UsearchVectorRepository(index=index)
    repo.add_vectors([("hash-a_0", _QUERY)])
    yield repo
    index.close_meta_conn()


@pytest.mark.parametrize("factory", _SEARCH_RAISES, ids=["real", "fake"])
def test_search_raises_propagates_typed_exception(factory: Callable[[BaseException], VectorRepository]) -> None:
    """A search backend failure surfaces verbatim — callers must not
    silently fall back to BM25-only on a transient vector backend error
    (the caller's job is to retry / classify).

    Sabotage proof: in ``UsearchVectorRepository.search`` wrap the
    ``self._index.search(...)`` call in ``try/except Exception: return []``.
    Re-run: the real leg's pytest.raises sees no exception. Restored.
    """
    repo = factory(RuntimeError("F68-vec-raises"))
    with pytest.raises(RuntimeError, match="F68-vec-raises"):
        repo.search(query_vec=[0.1, 0.2], k=5)


def test_search_returns_empty_when_collections_filter_matches_nothing(alpha_repo: VectorRepository) -> None:
    """Empty result for an unmatched collections filter — callers
    iterate without a None check. The unfiltered search first proves
    the vector IS there, so the empty result is the filter's doing.

    Sabotage proof: in ``VectorIndex._build_results`` delete the
    ``if collections and row[_KEY_COLLECTION] not in collections: continue``
    guard. Re-run: the real leg returns the alpha row and ``== []``
    fails. Restored.
    """
    assert [r["path"] for r in alpha_repo.search(query_vec=_QUERY, k=5)] == ["a.md"]
    out = alpha_repo.search(query_vec=_QUERY, k=5, collections=["nope-collection"])
    assert out == [], f"unmatched filter must yield []; got {out!r}"


@pytest.mark.parametrize("factory", _ADD_RAISES, ids=["real", "fake"])
def test_add_vectors_raises_propagates_typed_exception(factory: Callable[[BaseException], VectorRepository]) -> None:
    """add_vectors raises on backend rejection — caller must NOT
    interpret a swallowed error as "rows written".

    Sabotage proof: in ``UsearchVectorRepository.add_vectors`` wrap the
    ``self._index.add_vectors(...)`` call in ``try/except Exception:
    return 0``. Re-run: the real leg's pytest.raises sees nothing.
    Restored.
    """
    repo = factory(RuntimeError("F68-add-raises"))
    with pytest.raises(RuntimeError, match="F68-add-raises"):
        repo.add_vectors([("a.md", [0.1, 0.2])])


@pytest.mark.parametrize("factory", _COUNT_RAISES, ids=["real", "fake"])
def test_count_raises_propagates_typed_exception(factory: Callable[[BaseException], VectorRepository]) -> None:
    """count raises when the backing index handle is unavailable —
    operators distinguish "0 vectors" from "index unreachable".

    Sabotage proof: in ``UsearchVectorRepository.count`` wrap
    ``len(self._index)`` in ``try/except Exception: return 0``. Re-run:
    the real leg's pytest.raises sees nothing. Restored.
    """
    repo = factory(RuntimeError("F68-count-raises"))
    with pytest.raises(RuntimeError, match="F68-count-raises"):
        repo.count()
