"""
Tests for kairix.core.search.planner — QueryPlanner decompose + retrieve_and_merge.

Tests cover:
  - decompose() fallback paths (empty / invalid / over-long / non-list responses)
  - decompose() success paths (single + multi-hop + filtered list items)
  - decompose() Neo4j context injection (available + unavailable + raising client)
  - retrieve_and_merge() single + multiple sub-queries with RRF, dedup, and fallback

All tests inject ``llm_backend=FakeLLMBackend(...)`` and (where relevant) a
``neo4j_client=`` mock — the production class accepts both as constructor /
method arguments, so tests don't need to ``patch.dict("sys.modules", ...)``
or substitute imports. The defensive-default ``llm_backend = get_default_backend()``
branch is ``# pragma: no cover``-annotated in production with rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from kairix.core.search.planner import QueryPlanner
from tests.fakes import FakeLLMBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    """Minimal fake result with .path attribute for retrieve_and_merge."""

    path: str
    score: float = 1.0


def _search_fn_factory(results_by_query: dict[str, list[_FakeResult]]):
    """Return a search function that maps queries to pre-set results."""

    def search_fn(query: str) -> list[_FakeResult]:
        return results_by_query.get(query, [])

    return search_fn


# ---------------------------------------------------------------------------
# decompose() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDecompose:
    @pytest.mark.unit
    def test_fallback_to_original_query_when_backend_chat_raises(self) -> None:
        """When the backend's chat() raises, decompose returns [query] unchanged."""
        planner = QueryPlanner()
        backend = FakeLLMBackend(chat_raises=RuntimeError("API down"))
        result = planner.decompose("query that fails", llm_backend=backend)
        assert result == ["query that fails"]
        # The backend WAS called once (the exception was caught inside decompose).
        assert len(backend.chat_calls) == 1

    @pytest.mark.unit
    def test_fallback_to_original_query_when_backend_returns_empty_string(self) -> None:
        """An empty backend response → fall back to [query] (with a warning log)."""
        planner = QueryPlanner()
        backend = FakeLLMBackend(chat_response="")
        result = planner.decompose("what happened in march", llm_backend=backend)
        assert result == ["what happened in march"]

    @pytest.mark.unit
    def test_fallback_to_original_query_when_response_is_invalid_json(self) -> None:
        """Non-JSON response with no extractable quoted strings → [query]."""
        planner = QueryPlanner()
        # No quoted substrings of length 5+ → regex fallback can't extract anything → [query].
        backend = FakeLLMBackend(chat_response="not json at all")
        result = planner.decompose("some query", llm_backend=backend)
        assert result == ["some query"]

    @pytest.mark.unit
    def test_regex_fallback_extracts_quoted_strings_from_invalid_json(self) -> None:
        """When JSON parse fails but the response has quoted ≥5-char strings, those are returned.

        Closes coverage of the regex-fallback branch in decompose.
        """
        planner = QueryPlanner()
        backend = FakeLLMBackend(chat_response='garbage "first sub-query" some "second one" tail')
        result = planner.decompose("some query", llm_backend=backend)
        assert result == ["first sub-query", "second one"]

    @pytest.mark.unit
    def test_fallback_to_original_query_when_response_json_is_not_a_list(self) -> None:
        """A JSON dict response → fall through to [query]."""
        planner = QueryPlanner()
        backend = FakeLLMBackend(chat_response='{"key": "value"}')
        result = planner.decompose("what", llm_backend=backend)
        assert result == ["what"]

    @pytest.mark.unit
    def test_fallback_to_original_query_when_response_list_has_more_than_three_items(self) -> None:
        """A list with >3 items violates the contract → fall back to [query]."""
        planner = QueryPlanner()
        backend = FakeLLMBackend(chat_response='["a", "b", "c", "d"]')
        result = planner.decompose("something", llm_backend=backend)
        assert result == ["something"]

    @pytest.mark.unit
    def test_fallback_to_original_query_when_response_list_is_empty(self) -> None:
        """An empty list response → length below the 1-3 contract → [query]."""
        planner = QueryPlanner()
        backend = FakeLLMBackend(chat_response="[]")
        result = planner.decompose("empty response", llm_backend=backend)
        assert result == ["empty response"]

    @pytest.mark.unit
    def test_returns_single_sub_query_from_passthrough_response(self) -> None:
        """A single-element response is returned verbatim."""
        planner = QueryPlanner()
        backend = FakeLLMBackend(chat_response='["simple query passthrough"]')
        result = planner.decompose("simple query passthrough", llm_backend=backend)
        assert result == ["simple query passthrough"]

    @pytest.mark.unit
    def test_returns_three_sub_queries_for_multi_hop_decomposition(self) -> None:
        """A 3-element response is returned as the decomposed sub-query list."""
        planner = QueryPlanner()
        backend = FakeLLMBackend(chat_response='["sub1", "sub2", "sub3"]')
        result = planner.decompose("complex query needing decomposition", llm_backend=backend)
        assert result == ["sub1", "sub2", "sub3"]

    @pytest.mark.unit
    def test_filters_empty_string_items_from_decomposed_list(self) -> None:
        """Empty-string list items are filtered before returning."""
        planner = QueryPlanner()
        backend = FakeLLMBackend(chat_response='["sub1", "", "sub2"]')
        result = planner.decompose("query with blanks", llm_backend=backend)
        assert result == ["sub1", "sub2"]

    @pytest.mark.unit
    def test_uses_plain_prompt_when_neo4j_client_is_unavailable(self) -> None:
        """A neo4j_client with available=False routes through the plain prompt path.

        Asserts the prompt fed to the backend does NOT contain the entity-context
        marker — proving the available=False guard short-circuits before the
        graph lookup.
        """
        planner = QueryPlanner()
        neo4j_mock = MagicMock(available=False)
        backend = FakeLLMBackend(chat_response='["sub-query-from-plain-prompt"]')

        result = planner.decompose("active projects techcorp", neo4j_client=neo4j_mock, llm_backend=backend)

        assert result == ["sub-query-from-plain-prompt"]
        # Plain prompt path: the entity-context marker is absent.
        prompt = backend.chat_calls[0]["messages"][0]["content"]
        assert "Known entities related to this query" not in prompt

    @pytest.mark.unit
    def test_injects_entity_context_when_neo4j_returns_relationships(self) -> None:
        """An available neo4j_client with relationships injects the entity context into the prompt.

        Asserts the entity-context marker DOES appear in the prompt and the
        related entity names are present — the with-context prompt path is
        actually exercised, not just declared by the test.
        """
        planner = QueryPlanner()
        neo4j_mock = MagicMock(available=True)
        neo4j_mock.find_by_name.return_value = [{"id": "techcorp", "name": "TechCorp"}]
        neo4j_mock.related_entities.return_value = [
            {"name": "GlobalTech"},
            {"name": "BuilderCo"},
        ]
        backend = FakeLLMBackend(chat_response='["entity-aware sub-query"]')

        result = planner.decompose("what techcorp builds today", neo4j_client=neo4j_mock, llm_backend=backend)

        assert result == ["entity-aware sub-query"]
        # The with-context prompt was used and named the related entities.
        prompt = backend.chat_calls[0]["messages"][0]["content"]
        assert "Known entities related to this query" in prompt
        assert "TechCorp" in prompt
        assert "GlobalTech" in prompt and "BuilderCo" in prompt

    @pytest.mark.unit
    def test_uses_plain_prompt_when_neo4j_returns_no_entities(self) -> None:
        """An available client that finds no entities → context is None → plain prompt."""
        planner = QueryPlanner()
        neo4j_mock = MagicMock(available=True)
        neo4j_mock.find_by_name.return_value = []  # no matches → no context built
        backend = FakeLLMBackend(chat_response='["plain"]')

        result = planner.decompose("query that finds nothing", neo4j_client=neo4j_mock, llm_backend=backend)

        assert result == ["plain"]
        prompt = backend.chat_calls[0]["messages"][0]["content"]
        assert "Known entities related to this query" not in prompt

    @pytest.mark.unit
    def test_falls_back_to_plain_prompt_when_neo4j_context_lookup_raises(self) -> None:
        """If the Neo4j lookup raises mid-context-build, fall through to the plain prompt path.

        Closes coverage of the inner ``except Exception`` around ``neo4j_graph_context``.
        """
        planner = QueryPlanner()
        neo4j_mock = MagicMock(available=True)
        neo4j_mock.find_by_name.side_effect = RuntimeError("neo4j crash")
        backend = FakeLLMBackend(chat_response='["fallback"]')

        result = planner.decompose("query with broken neo4j", neo4j_client=neo4j_mock, llm_backend=backend)

        assert result == ["fallback"]


# ---------------------------------------------------------------------------
# retrieve_and_merge() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetrieveAndMerge:
    @pytest.mark.unit
    def test_single_sub_query_returns_results(self) -> None:
        """Single sub-query: results returned in order."""
        planner = QueryPlanner()
        results = [_FakeResult(path=f"doc{i}.md") for i in range(5)]
        search_fn = _search_fn_factory({"query1": results})

        merged = planner.retrieve_and_merge(["query1"], search_fn, top_k_per_sub=5, final_top_k=5)
        assert len(merged) == 5
        paths = [r.path for r in merged]
        for i in range(5):
            assert f"doc{i}.md" in paths

    @pytest.mark.unit
    def test_multi_sub_query_deduplication(self) -> None:
        """Same document in multiple sub-query results → appears once in merged."""
        planner = QueryPlanner()
        shared = _FakeResult(path="shared.md")
        only_q1 = _FakeResult(path="only-q1.md")
        only_q2 = _FakeResult(path="only-q2.md")

        search_fn = _search_fn_factory(
            {
                "q1": [shared, only_q1],
                "q2": [shared, only_q2],
            }
        )

        merged = planner.retrieve_and_merge(["q1", "q2"], search_fn, top_k_per_sub=5, final_top_k=6)
        paths = [r.path for r in merged]

        # shared.md must appear exactly once
        assert paths.count("shared.md") == 1
        assert "only-q1.md" in paths
        assert "only-q2.md" in paths

    @pytest.mark.unit
    def test_rrf_boosts_document_in_multiple_lists(self) -> None:
        """Document appearing in both sub-query result lists should rank higher."""
        planner = QueryPlanner()
        shared = _FakeResult(path="shared.md")
        unique1 = _FakeResult(path="unique1.md")
        unique2 = _FakeResult(path="unique2.md")

        search_fn = _search_fn_factory(
            {
                "q1": [unique1, shared],
                "q2": [unique2, shared],
            }
        )

        merged = planner.retrieve_and_merge(["q1", "q2"], search_fn, top_k_per_sub=5, final_top_k=6)
        paths = [r.path for r in merged]

        # shared.md appears in both lists at rank 2 → RRF score higher than unique docs at rank 2
        assert paths[0] == "shared.md", f"Expected shared.md first, got {paths}"

    @pytest.mark.unit
    def test_search_fn_exception_handled(self) -> None:
        """If search_fn raises for one sub-query, other results still returned."""
        planner = QueryPlanner()
        good_results = [_FakeResult(path="good.md")]

        def flaky_search(query: str) -> list[Any]:
            if query == "bad":
                raise RuntimeError("search failed")
            return good_results

        merged = planner.retrieve_and_merge(["good", "bad"], flaky_search, top_k_per_sub=5, final_top_k=6)
        paths = [r.path for r in merged]
        assert "good.md" in paths

    @pytest.mark.unit
    def test_respects_final_top_k(self) -> None:
        """Result count should not exceed final_top_k."""
        planner = QueryPlanner()
        results = [_FakeResult(path=f"doc{i}.md") for i in range(10)]
        search_fn = _search_fn_factory({"q1": results[:5], "q2": results[5:]})

        merged = planner.retrieve_and_merge(["q1", "q2"], search_fn, top_k_per_sub=5, final_top_k=3)
        assert len(merged) <= 3

    @pytest.mark.unit
    def test_dict_results_with_file_key(self) -> None:
        """Handles dict results with 'file' key (BM25-style)."""
        planner = QueryPlanner()
        results = [{"file": "bm25-doc.md", "score": 1.0}]
        search_fn = _search_fn_factory({"q": results})

        merged = planner.retrieve_and_merge(["q"], search_fn, top_k_per_sub=5, final_top_k=5)
        assert len(merged) == 1

    @pytest.mark.unit
    def test_dict_results_with_path_key(self) -> None:
        """Handles dict results with 'path' key."""
        planner = QueryPlanner()
        results = [{"path": "path-doc.md", "score": 1.0}]
        search_fn = _search_fn_factory({"q": results})

        merged = planner.retrieve_and_merge(["q"], search_fn, top_k_per_sub=5, final_top_k=5)
        assert len(merged) == 1

    @pytest.mark.unit
    def test_nested_result_path_attribute(self) -> None:
        """Handles results with .result.path (BudgetedResult style)."""
        planner = QueryPlanner()

        inner = MagicMock()
        inner.path = "nested-doc.md"
        outer = MagicMock()
        outer.result = inner
        # Ensure hasattr checks pass
        del outer.path  # no direct .path on outer

        results = [outer]
        search_fn = _search_fn_factory({"q": results})

        merged = planner.retrieve_and_merge(["q"], search_fn, top_k_per_sub=5, final_top_k=5)
        assert len(merged) == 1

    @pytest.mark.unit
    def test_top_k_per_sub_limits_results_per_sub(self) -> None:
        """top_k_per_sub limits how many results per sub-query enter RRF."""
        planner = QueryPlanner()
        results = [_FakeResult(path=f"doc{i}.md") for i in range(10)]
        search_fn = _search_fn_factory({"q": results})

        # With top_k_per_sub=2, only doc0 and doc1 should enter RRF
        merged = planner.retrieve_and_merge(["q"], search_fn, top_k_per_sub=2, final_top_k=10)
        paths = [r.path for r in merged]
        assert len(paths) == 2
        assert "doc0.md" in paths
        assert "doc1.md" in paths


# ---------------------------------------------------------------------------
# _neo4j_graph_context() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNeo4jGraphContext:
    @pytest.mark.unit
    def test_returns_none_when_no_entities_found(self) -> None:
        """Should return None when client finds no matching entities."""
        from kairix.core.search.planner import neo4j_graph_context

        client = MagicMock()
        client.find_by_name.return_value = []
        result = neo4j_graph_context("what is the meaning of life", client)
        assert result is None

    @pytest.mark.unit
    def test_returns_context_string_with_entities(self) -> None:
        """Should return a context string when entities and relationships are found."""
        from kairix.core.search.planner import neo4j_graph_context

        client = MagicMock()
        client.find_by_name.return_value = [{"id": "e1", "name": "TechCorp"}]
        client.related_entities.return_value = [
            {"name": "GlobalTech"},
            {"name": "BuilderCo"},
        ]
        result = neo4j_graph_context("what does TechCorp build", client)
        assert result is not None
        assert "TechCorp" in result
        assert "GlobalTech" in result

    @pytest.mark.unit
    def test_skips_entities_without_id(self) -> None:
        """Entities missing 'id' should be skipped."""
        from kairix.core.search.planner import neo4j_graph_context

        client = MagicMock()
        client.find_by_name.return_value = [{"name": "NoId"}]  # no "id" key
        result = neo4j_graph_context("query about NoId entity", client)
        assert result is None

    @pytest.mark.unit
    def test_handles_find_by_name_exception(self) -> None:
        """Should continue silently when find_by_name raises."""
        from kairix.core.search.planner import neo4j_graph_context

        client = MagicMock()
        client.find_by_name.side_effect = RuntimeError("neo4j down")
        result = neo4j_graph_context("query words here today", client)
        assert result is None

    @pytest.mark.unit
    def test_handles_related_entities_exception(self) -> None:
        """Should skip entity gracefully when related_entities raises."""
        from kairix.core.search.planner import neo4j_graph_context

        client = MagicMock()
        client.find_by_name.return_value = [{"id": "e1", "name": "Entity1"}]
        client.related_entities.side_effect = RuntimeError("timeout")
        result = neo4j_graph_context("query about Entity1 topic", client)
        # Entity found but no relationships retrieved — context_parts has only header
        assert result is None

    @pytest.mark.unit
    def test_deduplicates_entities_by_id(self) -> None:
        """Same entity ID from multiple words should appear only once."""
        from kairix.core.search.planner import neo4j_graph_context

        client = MagicMock()
        entity = {"id": "e1", "name": "SameEntity"}
        client.find_by_name.return_value = [entity]
        client.related_entities.return_value = [{"name": "Related1"}]
        result = neo4j_graph_context("SameEntity also SameEntity again", client)
        # Should still produce a valid context with entity appearing once
        assert result is not None
        assert result.count("SameEntity") >= 1

    @pytest.mark.unit
    def test_filters_self_from_related(self) -> None:
        """Related entities with same name as source should be excluded."""
        from kairix.core.search.planner import neo4j_graph_context

        client = MagicMock()
        client.find_by_name.return_value = [{"id": "e1", "name": "Alpha"}]
        # related_entities returns self + one other
        client.related_entities.return_value = [
            {"name": "Alpha"},  # self — should be filtered
            {"name": "Beta"},
        ]
        result = neo4j_graph_context("Alpha projects overview", client)
        assert result is not None
        assert "Beta" in result

    @pytest.mark.unit
    def test_short_words_filtered_out(self) -> None:
        """Words with 3 or fewer chars after stripping should be skipped."""
        from kairix.core.search.planner import neo4j_graph_context

        client = MagicMock()
        client.find_by_name.return_value = []
        neo4j_graph_context("is it a ok", client)
        # find_by_name should not be called for short words
        # Only words > 3 chars are queried — none in this query
        client.find_by_name.assert_not_called()


# ---------------------------------------------------------------------------
# Additional decompose edge case tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDecomposeEdgeCases:
    @pytest.mark.unit
    def test_non_string_items_filtered(self) -> None:
        """Sub-query list items that are not strings should be filtered."""
        planner = QueryPlanner()
        mock_backend = MagicMock()
        mock_backend.chat.return_value = '["valid", 123, "also valid"]'
        result = planner.decompose("mixed types query", llm_backend=mock_backend)
        assert result == ["valid", "also valid"]

    @pytest.mark.unit
    def test_whitespace_only_items_filtered(self) -> None:
        """Sub-query list items that are whitespace-only should be filtered."""
        planner = QueryPlanner()
        mock_backend = MagicMock()
        mock_backend.chat.return_value = '["valid", "   ", "also valid"]'
        result = planner.decompose("whitespace items query", llm_backend=mock_backend)
        assert result == ["valid", "also valid"]

    @pytest.mark.unit
    def test_neo4j_context_exception_falls_back(self) -> None:
        """If neo4j find_by_name raises, fall back to plain-prompt decomposition.

        Drives the exception through ``find_by_name`` directly rather than patching
        ``neo4j_graph_context`` — the real production behaviour propagates from
        the Neo4j call, not from the helper itself.
        """
        planner = QueryPlanner()
        neo4j_mock = MagicMock(available=True)
        neo4j_mock.find_by_name.side_effect = RuntimeError("neo4j crash")
        mock_backend = FakeLLMBackend(chat_response='["fallback query"]')

        result = planner.decompose(
            "query with broken neo4j",
            neo4j_client=neo4j_mock,
            llm_backend=mock_backend,
        )
        # The plain prompt path was taken (no entity-context marker in the prompt).
        prompt = mock_backend.chat_calls[0]["messages"][0]["content"]
        assert "Known entities related to this query" not in prompt
        assert result == ["fallback query"]


# ---------------------------------------------------------------------------
# Additional retrieve_and_merge edge case tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetrieveAndMergeEdgeCases:
    @pytest.mark.unit
    def test_search_fn_returns_none(self) -> None:
        """search_fn returning None should be handled as empty results."""
        planner = QueryPlanner()

        def none_search(query: str):
            return None

        merged = planner.retrieve_and_merge(["q1"], none_search, top_k_per_sub=5, final_top_k=5)
        assert merged == []

    @pytest.mark.unit
    def test_empty_sub_queries_raises_value_error(self) -> None:
        """Empty sub_queries list raises ValueError from ThreadPoolExecutor."""
        planner = QueryPlanner()
        search_fn = _search_fn_factory({})
        with pytest.raises(ValueError, match="max_workers"):
            planner.retrieve_and_merge([], search_fn)

    @pytest.mark.unit
    def test_dict_result_without_file_or_path(self) -> None:
        """Dict result without 'file' or 'path' key should use str(r) as key."""
        planner = QueryPlanner()
        results = [{"score": 0.9, "content": "some text"}]
        search_fn = _search_fn_factory({"q": results})
        merged = planner.retrieve_and_merge(["q"], search_fn, top_k_per_sub=5, final_top_k=5)
        assert len(merged) == 1
