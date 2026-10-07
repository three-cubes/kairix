"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`VectorSearcher`.

``VectorSearcher.search_vectors(vector, *, limit)`` is the index seam the
post-embed recall gate (:class:`RecallChecker`) drives. When the usearch
index is unavailable the searcher must degrade to an empty path list —
the recall gate then scores every query as a miss (score 0.0), which is
exactly the alarm the gate exists to raise. It must never crash the
embed pipeline that calls it.

One body, two implementations (F43 limb 2):

* the real :class:`UsearchVectorSearcher`, whose ``index_resolver`` seam
  raises (index file missing / corrupt);
* the canonical :class:`tests.fakes.FakeVectorSearcher` with no paths.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.embed.recall_check import RecallChecker, UsearchVectorSearcher
from tests.fakes import FakeEmbedProvider, FakeVectorSearcher

pytestmark = pytest.mark.contract

_QUERIES = [
    ("R01", "architecture decision record", "architecture"),
    ("R02", "how to deploy", "deploy"),
]


def _index_unavailable() -> Any:
    raise RuntimeError("F68-usearch-index-unavailable")


_FACTORIES: list[Callable[[], Any]] = [
    lambda: UsearchVectorSearcher(index_resolver=_index_unavailable),
    lambda: FakeVectorSearcher(paths=[]),
]


@pytest.mark.parametrize("factory", _FACTORIES, ids=["real-usearch", "fake"])
def test_search_vectors_returns_empty_when_index_unavailable_recall_scores_zero(
    factory: Callable[[], Any],
) -> None:
    """An unavailable index yields ``[]`` and the recall gate scores 0.0.

    Every query is embedded (not skipped), searched, and recorded as a
    miss with an empty ``returned`` list — the observable signal operators
    alert on.

    Sabotage proof: in ``UsearchVectorSearcher.search_vectors`` replace the
    ``except Exception`` fallback ``return []`` with ``raise``. Re-run: the
    real case fails because ``RecallChecker.check`` propagates the index
    error instead of reporting a 0.0 score. Restored.
    """
    searcher = factory()
    checker = RecallChecker(
        embed_provider=FakeEmbedProvider(vector=[0.0, 0.6, 0.8]),
        vector_searcher=searcher,
    )
    db = sqlite3.connect(":memory:")
    try:
        result = checker.check(db=db, recall_queries=_QUERIES, canary_cache_path=None)
    finally:
        db.close()

    assert result["score"] == pytest.approx(0.0)
    assert result["passed"] == 0
    assert result["total"] == len(_QUERIES)
    assert [d["returned"] for d in result["detail"]] == [[], []]
    assert [d["skipped"] for d in result["detail"]] == [False, False]
