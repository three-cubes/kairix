"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`Retriever`.

One method (``retrieve``). The Protocol contract is "surface vec_failed
state via a ``vec_failed: bool`` attribute on the result so callers can
distinguish 'no results' from 'vector index unavailable'." That is the
canonical failure surface for this protocol.

Failure surface:

  * ``returns_empty`` — empty ``paths`` list when no documents match the
    query (the FakeRetriever default for unknown queries).
  * ``unavailable`` — ``vec_failed=True`` on the result when the
    underlying vector backend is unreachable; callers gate on this flag
    to surface degraded mode in their telemetry.

F43: every test runs ONE assertion body over a minimal probe Retriever
built on the production hybrid retrieval path
(:func:`kairix.quality.eval.retrieval.retrieve` with ``system="hybrid"``,
the exact call the shipped hybrid-sweep retriever makes, with the
search pipeline replaced through the public ``RetrievalDeps(searcher=)``
seam) AND :class:`tests.fakes.FakeRetriever`.

The shipped retrievers are private and seam-less
(``hybrid_sweep._DefaultHybridRetriever`` hard-wires the default
``RetrievalDeps``; ``gold_builder._DefaultGoldRetriever`` hits the live
embedding endpoint), so the probe drives the same production ``retrieve``
call. Both legs return the one ``RetrievalResult`` shape (``paths`` +
``vec_failed`` attribute) — the probe returns the production result
unchanged, so the shared assertions exercise the real search-result →
result translation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.protocols import Retriever
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchResult
from kairix.quality.eval.retrieval import RetrievalDeps, RetrievalResult, retrieve
from tests.fakes import FakeRetriever

pytestmark = pytest.mark.contract


class _HybridRetrieverProbe:
    """Minimal :class:`Retriever` over the production ``retrieve(system="hybrid")`` path."""

    def __init__(self, search_result: SearchResult) -> None:
        self._search_result = search_result

    def _searcher(self, **_kwargs: Any) -> SearchResult:
        return self._search_result

    def retrieve(self, query: str, *, collections: list[str] | None = None, cfg: Any = None) -> RetrievalResult:
        return retrieve(
            query=query,
            system="hybrid",
            config=cfg if cfg is not None else RetrievalConfig(),
            collections=collections,
            deps=RetrievalDeps(searcher=self._searcher),
        )


def _real(vec_failed: bool) -> Retriever:
    """Probe whose search pipeline found nothing (vector backend up or down)."""
    return _HybridRetrieverProbe(SearchResult(query="q", intent=QueryIntent.SEMANTIC, vec_failed=vec_failed))


def _fake(vec_failed: bool) -> Retriever:
    if not vec_failed:
        return FakeRetriever()
    return FakeRetriever(results_by_query={"q": RetrievalResult(paths=[], vec_failed=True)})


_IMPLS: list[Callable[[bool], Retriever]] = [_real, _fake]


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_retrieve_returns_empty_when_no_results_configured_for_query(factory: Callable[[bool], Retriever]) -> None:
    """A query with no matches yields an empty paths list — the
    retriever must not invent matches when the corpus has nothing.

    Sabotage proof: in ``kairix.quality.eval.retrieval._retrieve_hybrid``
    change ``paths = [b.result.path for b in sr.results]`` to
    ``paths = [b.result.path for b in sr.results] or ["phantom.md"]``.
    Re-run: the real leg's ``== []`` assertion fails. Restored.
    """
    retriever = factory(False)
    out = retriever.retrieve("q")
    assert out.paths == [], f"no-match query must yield paths=[]; got {out.paths!r}"
    assert out.vec_failed is False, "no-match default must report vec backend healthy"


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_retrieve_unavailable_when_vec_backend_failed(factory: Callable[[bool], Retriever]) -> None:
    """The retriever must surface ``vec_failed=True`` when the vector
    backend was unreachable — callers gate on this to distinguish "no
    results" from "degraded mode".

    Sabotage proof: in ``kairix.quality.eval.retrieval._retrieve_hybrid``
    change ``"vec_failed": sr.vec_failed`` to ``"vec_failed": False``.
    Re-run: the real leg's ``is True`` assertion fails. Restored.
    """
    retriever = factory(True)
    out = retriever.retrieve("q")
    assert out.vec_failed is True, (
        "vec backend failure MUST surface as vec_failed=True so callers can "
        "report degraded mode rather than misread an empty list as 'no matches'"
    )
