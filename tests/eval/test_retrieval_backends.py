"""Unit tests for kairix.quality.eval.retrieval — backend dispatch and BM25 path.

Covers:
  - retrieve(system="mock")          → delegates to mock_retrieve
  - retrieve(system="mock-reflib")   → delegates to mock_reflib_retrieve
  - retrieve(system="unknown")       → raises ValueError
  - retrieve(system="hybrid", agent="x") wires agent + scope into the searcher
  - retrieve(system="bm25")          → _retrieve_bm25 path, no kairix DB needed
    (sabotage-proven by injecting a FakeDocumentRepository via deps where
     possible; otherwise the bm25 path tolerates an empty repo and returns
     an empty result without raising)

Uses RetrievalDeps to inject a spy searcher / pipeline_builder rather than
patching internals.  No @patch, no monkeypatch on KAIRIX_*, no internal-name
imports beyond the public RetrievalDeps/retrieve surface and RetrievalConfig.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.quality.eval.retrieval import (
    RetrievalDeps,
    RetrievalResult,
    retrieve,
)

pytestmark = pytest.mark.unit


@dataclass
class _CapturedSearchResult:
    """Minimal SearchResult stand-in — only the fields _retrieve_hybrid reads."""

    results: list[Any] = field(default_factory=list)
    intent: Any = field(default_factory=lambda: type("Intent", (), {"value": "semantic"})())
    bm25_count: int = 0
    vec_count: int = 0
    fused_count: int = 0
    vec_failed: bool = False
    latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# system="hybrid" with agent → agent + scope flow into searcher kwargs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetrieveHybridAgentKwargs:
    @pytest.mark.unit
    def test_agent_kwarg_sets_agent_and_scope_on_searcher_call(self) -> None:
        """When ``agent="alice"`` is passed, the searcher receives
        ``agent="alice"`` AND ``scope="shared+agent"``.

        Sabotage proof: the captured kwargs MUST contain both keys with the
        exact values, or the assert fires.
        """
        captured: list[dict[str, Any]] = []

        def _spy_searcher(**kwargs: Any) -> _CapturedSearchResult:
            captured.append(kwargs)
            return _CapturedSearchResult()

        result = retrieve(
            query="anything",
            system="hybrid",
            agent="alice",
            deps=RetrievalDeps(searcher=_spy_searcher),
        )

        assert isinstance(result, RetrievalResult)
        assert len(captured) == 1
        assert captured[0]["agent"] == "alice"
        assert captured[0]["scope"] == "shared+agent"
        assert captured[0]["query"] == "anything"

    @pytest.mark.unit
    def test_no_agent_kwarg_omits_agent_and_scope(self) -> None:
        """When ``agent`` is omitted, neither key is set on the searcher call.

        Sabotage proof: regressing to "always set agent=None" would leak the
        key into the kwargs and break the contract.
        """
        captured: list[dict[str, Any]] = []

        def _spy_searcher(**kwargs: Any) -> _CapturedSearchResult:
            captured.append(kwargs)
            return _CapturedSearchResult()

        retrieve(
            query="anything",
            system="hybrid",
            deps=RetrievalDeps(searcher=_spy_searcher),
        )

        assert "agent" not in captured[0], f"agent key leaked into searcher kwargs when not provided: {captured[0]!r}"
        assert "scope" not in captured[0]


# ---------------------------------------------------------------------------
# system="mock" → mock_retrieve delegation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetrieveMockBackend:
    @pytest.mark.unit
    def test_mock_returns_retrieval_result_with_paths_snippets_meta(self) -> None:
        """``retrieve(system="mock")`` returns a RetrievalResult populated from
        ``kairix.quality.benchmark.mock_retrieval.mock_retrieve``.

        Sabotage proof: the mock fixture corpus is deterministic; if the
        wiring regressed (e.g. wrong import path), the call would raise.
        """
        result = retrieve(query="anything mock-related", system="mock", limit=5)

        assert isinstance(result, RetrievalResult)
        # Paths / snippets / meta are populated from the mock engine.
        # Even if the query matches nothing in the fixture corpus, the
        # call must succeed and return list-shaped fields.
        assert isinstance(result.paths, list)
        assert isinstance(result.snippets, list)
        assert isinstance(result.meta, dict)


# ---------------------------------------------------------------------------
# system="mock-reflib" → mock_reflib_retrieve delegation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetrieveMockReflibBackend:
    @pytest.mark.unit
    def test_mock_reflib_returns_retrieval_result(self) -> None:
        """``retrieve(system="mock-reflib")`` returns a RetrievalResult
        populated from the reflib mock engine.

        Sabotage proof: the call would raise ImportError if the wiring
        regressed; today it must succeed and return list-shaped fields.
        """
        result = retrieve(query="reflib citation lookup", system="mock-reflib", limit=5)

        assert isinstance(result, RetrievalResult)
        assert isinstance(result.paths, list)
        assert isinstance(result.snippets, list)
        assert isinstance(result.meta, dict)


# ---------------------------------------------------------------------------
# system="<unknown>" → ValueError
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetrieveUnknownSystem:
    @pytest.mark.unit
    def test_unknown_system_raises_value_error(self) -> None:
        """``retrieve(system="not-a-backend")`` raises ValueError with a
        message listing the valid backends.

        Sabotage proof: returning silently (or defaulting to hybrid) would
        hide the typo from the caller.
        """
        with pytest.raises(ValueError) as exc_info:
            retrieve(query="anything", system="not-a-backend")

        msg = str(exc_info.value)
        assert "not-a-backend" in msg
        # Must list valid options so the caller can self-correct.
        assert "hybrid" in msg
        assert "bm25" in msg
        assert "mock" in msg


# ---------------------------------------------------------------------------
# system="bm25" → _retrieve_bm25 path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetrieveBM25Backend:
    @pytest.mark.unit
    def test_bm25_path_returns_retrieval_result(self) -> None:
        """``retrieve(system="bm25")`` returns a RetrievalResult with
        ``meta["system"] == "bm25"``. ``bm25_search`` never raises and
        returns ``[]`` on any failure (missing DB, missing table, etc.),
        so this contract is robust against the local CI environment.

        Sabotage proof: regressing ``_retrieve_bm25`` to forget the
        ``meta={"system": "bm25"}`` annotation would break the assertion;
        regressing it to raise instead of returning ``[]`` would also fail.
        """
        result = retrieve(query="anything bm25", system="bm25", limit=5)

        assert isinstance(result, RetrievalResult)
        assert isinstance(result.paths, list)
        assert isinstance(result.snippets, list)
        assert result.meta == {"system": "bm25"}, (
            f"_retrieve_bm25 must tag meta with system='bm25'; got {result.meta!r}"
        )
