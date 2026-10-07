"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`Retriever`.

One method (``retrieve``). The Protocol contract is "surface vec_failed
state via a ``vec_failed: bool`` attribute on the result so callers can
distinguish 'no results' from 'vector index unavailable'." That is the
canonical failure surface for this protocol.

Failure surface:

  * ``returns_empty`` — empty results list when no documents match the
    query (the FakeRetriever default for unknown queries).
  * ``unavailable`` — ``vec_failed=True`` on the result when the
    underlying vector backend is unreachable; callers gate on this flag
    to surface degraded mode in their telemetry.
"""

from __future__ import annotations

import pytest

from kairix.quality.eval.retrieval import RetrievalResult
from tests.fakes import FakeRetriever

pytestmark = pytest.mark.contract


def test_retrieve_returns_empty_when_no_results_configured_for_query() -> None:
    """Unknown query yields an empty results list — the retriever must
    not invent matches when the corpus has nothing.

    Sabotage proof: in ``FakeRetriever.retrieve`` change the default
    branch to ``return RetrievalResult(paths=["phantom.md"],
    vec_failed=False)``. Re-run: the ``== []`` assertion fails because
    a phantom row leaks through. Restored.
    """
    retriever = FakeRetriever()
    out = retriever.retrieve("unseen query")
    assert out.paths == [], f"unknown query must yield paths=[]; got {out.paths!r}"
    assert out.vec_failed is False, "unknown-query default must report vec backend healthy"


def test_retrieve_unavailable_when_vec_backend_failed() -> None:
    """The retriever must surface ``vec_failed=True`` when the vector
    backend was unreachable — callers gate on this to distinguish "no
    results" from "degraded mode".

    Sabotage proof: change the configured result to ``vec_failed=False``;
    the ``is True`` assertion fails. Restored.
    """
    failed_result = RetrievalResult(paths=[], vec_failed=True)
    retriever = FakeRetriever(results_by_query={"q": failed_result})
    out = retriever.retrieve("q")
    assert out.vec_failed is True, (
        "vec backend failure MUST surface as vec_failed=True so callers can "
        "report degraded mode rather than misread an empty list as 'no matches'"
    )
