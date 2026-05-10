"""
Tests for kairix.agents.mcp.server — MCP tool implementations.

Tool functions are pure Python and importable without the ``mcp`` package.
Tests use dependency injection (DI) — no monkey-patching required.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kairix.agents.mcp.server import (
    tool_contradict,
    tool_entity,
    tool_prep,
    tool_search,
    tool_timeline,
    tool_usage_guide,
)

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class FakeNeo4jClient:
    """Minimal stand-in for the Neo4j graph client."""

    def __init__(self, rows: list[dict] | None = None, *, available: bool = True):
        self._rows = rows or []
        self.available = available

    def cypher(self, query: str, params: dict | None = None) -> list[dict]:
        return self._rows


def _make_search_result(
    query: str = "test query",
    intent: str = "semantic",
    results: list | None = None,
    total_tokens: int = 10,
    latency_ms: float = 42.5,
    error: str = "",
) -> SimpleNamespace:
    """Build a fake SearchResult namespace."""
    if results is None:
        results = [
            SimpleNamespace(
                result=SimpleNamespace(path="notes/foo.md", boosted_score=0.9),
                content="some text here",
                token_estimate=10,
            )
        ]
    return SimpleNamespace(
        query=query,
        intent=SimpleNamespace(value=intent),
        results=results,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        error=error,
    )


def _make_prep_search_result() -> SimpleNamespace:
    """Create a fake search result for prep tests."""
    mock_result = SimpleNamespace(
        result=SimpleNamespace(title="test-doc", path="projects/test-doc.md"),
        content="This is test document content about the topic.",
    )
    return SimpleNamespace(results=[mock_result])


def _fake_search_default(**kw: object) -> SimpleNamespace:
    return _make_search_result(query=str(kw.get("query", "test query")))


def _fake_search_empty(**kw: object) -> SimpleNamespace:
    return _make_search_result(
        query=str(kw.get("query", "q")),
        intent="entity",
        results=[],
        total_tokens=0,
        latency_ms=1.0,
    )


def _fake_prep_search(*args: object, **kw: object) -> SimpleNamespace:
    return _make_prep_search_result()


def _fake_prep_search_empty(*args: object, **kw: object) -> SimpleNamespace:
    return SimpleNamespace(results=[])


def _fake_extract_temporal(query: str, reference_date: object = None) -> tuple[str, str]:
    return ("2026-04-06", "2026-04-13")


def _fake_rewrite_temporal(query: str, reference_date: object = None) -> str:
    return "what happened 2026-04-06..2026-04-13"


def _fake_extract_non_temporal(query: str, reference_date: object = None) -> tuple[None, None]:
    return (None, None)


def _fake_rewrite_none(query: str, reference_date: object = None) -> None:
    return None


def _fake_contradict_empty(**kw: object) -> list:
    return []


# ---------------------------------------------------------------------------
# tool_search
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_search_returns_expected_shape() -> None:
    result = tool_search(
        query="test query",
        agent=None,
        scope="shared+agent",
        budget=3000,
        search_fn=_fake_search_default,
    )

    assert result["query"] == "test query"
    assert result["intent"] == "semantic"
    assert len(result["results"]) == 1
    assert result["results"][0]["path"] == "notes/foo.md"
    assert result["results"][0]["score"] == pytest.approx(0.9)
    assert result["total_tokens"] == 10
    assert result["error"] == ""


@pytest.mark.unit
def test_tool_search_error_handled() -> None:
    def failing_search(**kw: object) -> None:
        raise RuntimeError("db unavailable")

    result = tool_search(
        query="broken",
        agent=None,
        scope="shared",
        budget=3000,
        search_fn=failing_search,
    )

    assert result["query"] == "broken"
    assert result["results"] == []
    assert "failed" in result["error"].lower()


# NOTE: the `tool_search` defensive ImportError branch (when ``kairix.core.factory``
# is not importable) is documented as ``# pragma: no cover`` in server.py. It is
# reachable only when the package is uninstalled — patching ``sys.modules`` to
# simulate that violates the no-monkeypatch rule, and there is no production
# scenario short of a broken install where the import fails.


@pytest.mark.unit
def test_tool_search_passes_agent_and_scope() -> None:
    captured: dict = {}

    def capturing_search(**kw: object) -> SimpleNamespace:
        captured.update(kw)
        return _make_search_result(
            query=str(kw["query"]),
            intent="entity",
            results=[],
            total_tokens=0,
            latency_ms=1.0,
        )

    tool_search(
        query="q",
        agent="builder",
        scope="agent",
        budget=1000,
        search_fn=capturing_search,
    )

    assert captured["query"] == "q"
    assert captured["agent"] == "builder"
    assert captured["scope"] == "agent"
    assert captured["budget"] == 1000


@pytest.mark.unit
def test_tool_search_result_snippet_truncated() -> None:
    long_text = "x" * 1000
    mock_budgeted = SimpleNamespace(
        result=SimpleNamespace(path="a.md", boosted_score=0.5),
        content=long_text,
        token_estimate=50,
    )

    def fake_search(**kw: object) -> SimpleNamespace:
        return _make_search_result(query="q", results=[mock_budgeted], total_tokens=50, latency_ms=5.0)

    result = tool_search(query="q", search_fn=fake_search)

    assert len(result["results"][0]["snippet"]) == 500


# ---------------------------------------------------------------------------
# tool_entity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_entity_neo4j_primary() -> None:
    fake = FakeNeo4jClient(
        [
            {
                "id": "acme",
                "name": "Acme",
                "type": "Organisation",
                "vault_path": "02-Areas/00-Clients/Acme/Acme.md",
                "role": None,
                "org": None,
                "tier": None,
                "engagement_status": None,
                "domain": None,
                "industry": None,
                "category": None,
            }
        ]
    )
    result = tool_entity(name="Acme", neo4j_client=fake)

    assert result["id"] == "acme"
    assert result["name"] == "Acme"
    assert result["type"] == "Organisation"
    assert result["error"] == ""


@pytest.mark.unit
def test_tool_entity_neo4j_not_found_returns_error() -> None:
    fake = FakeNeo4jClient([])
    result = tool_entity(name="Unknown Entity", neo4j_client=fake)

    assert result["id"] == ""
    assert "not found" in result["error"].lower()


@pytest.mark.unit
def test_tool_entity_neo4j_unavailable_returns_error() -> None:
    fake = FakeNeo4jClient(available=False)
    result = tool_entity(name="Test", neo4j_client=fake)

    assert result["error"] != ""


@pytest.mark.unit
def test_tool_entity_neo4j_exception_returns_error() -> None:
    """When the injected neo4j_client raises during query, the function surfaces an error dict."""

    class _RaisingNeo4jClient:
        @property
        def available(self) -> bool:
            return True

        def cypher(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
            raise RuntimeError("no neo4j")

    result = tool_entity(name="Anything", neo4j_client=_RaisingNeo4jClient())
    assert result["error"] != ""
    assert result["id"] == ""


@pytest.mark.unit
def test_tool_entity_summary_includes_category_when_present() -> None:
    """An entity row carrying a ``category`` field appears in the summary string."""
    fake = FakeNeo4jClient(
        [
            {
                "id": "platform",
                "name": "Kairix Platform",
                "type": "Project",
                "vault_path": "02-Areas/Kairix/Kairix.md",
                "role": None,
                "org": None,
                "tier": None,
                "engagement_status": None,
                "domain": None,
                "industry": None,
                "category": "knowledge-platform",
            }
        ]
    )
    result = tool_entity(name="Kairix Platform", neo4j_client=fake)

    assert result["error"] == ""
    # The category value must appear verbatim in the summary so agents see it.
    assert "knowledge-platform" in result["summary"]


# ---------------------------------------------------------------------------
# tool_prep
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_prep_l0() -> None:
    summary_text = "Brief context summary."

    def fake_chat(messages: list, max_tokens: int = 150) -> str:
        return summary_text

    result = tool_prep(
        query="What did we discuss last quarter?",
        tier="l0",
        search_fn=_fake_prep_search,
        chat_fn=fake_chat,
    )

    assert result["tier"] == "l0"
    assert result["summary"] == summary_text
    assert result["error"] == ""
    assert "sources" in result


@pytest.mark.unit
def test_tool_prep_l1() -> None:
    summary_text = "Detailed context summary about the engagement."

    def fake_chat(messages: list, max_tokens: int = 600) -> str:
        return summary_text

    result = tool_prep(
        query="Explain our test engagement",
        tier="l1",
        search_fn=_fake_prep_search,
        chat_fn=fake_chat,
    )

    assert result["tier"] == "l1"
    assert result["error"] == ""


@pytest.mark.unit
def test_tool_prep_no_results_returns_no_content() -> None:
    result = tool_prep(query="something obscure", tier="l0", search_fn=_fake_prep_search_empty)

    assert "no relevant documents" in result["summary"].lower()
    assert result["error"] == ""


@pytest.mark.unit
def test_tool_prep_error_handled() -> None:
    def failing_search(*args: object, **kw: object) -> None:
        raise RuntimeError("search unavailable")

    result = tool_prep(query="anything", tier="l0", search_fn=failing_search)

    assert result["summary"] == ""
    assert "failed" in result["error"].lower()


@pytest.mark.unit
def test_tool_prep_default_tier_is_l0() -> None:
    captured_kwargs: dict = {}

    def capturing_chat(messages: list, max_tokens: int = 150) -> str:
        captured_kwargs["max_tokens"] = max_tokens
        return "ok"

    tool_prep(query="q", search_fn=_fake_prep_search, chat_fn=capturing_chat)

    assert captured_kwargs["max_tokens"] == 150


# ---------------------------------------------------------------------------
# tool_timeline
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_timeline_temporal_query() -> None:
    result = tool_timeline(
        query="what happened last week",
        extract_fn=_fake_extract_temporal,
        rewrite_fn=_fake_rewrite_temporal,
    )

    assert result["is_temporal"] is True
    assert result["rewritten_query"] == "what happened 2026-04-06..2026-04-13"
    assert result["time_window"]["start"] == "2026-04-06"
    assert result["time_window"]["end"] == "2026-04-13"
    assert result["error"] == ""


@pytest.mark.unit
def test_tool_timeline_non_temporal_query() -> None:
    result = tool_timeline(query="tell me about Acme", extract_fn=_fake_extract_non_temporal)

    assert result["is_temporal"] is False
    assert result["rewritten_query"] == "tell me about Acme"
    assert result["time_window"] == {}
    assert result["error"] == ""


@pytest.mark.unit
def test_tool_timeline_preserves_original_query() -> None:
    result = tool_timeline(query="original question here", extract_fn=_fake_extract_non_temporal)

    assert result["original_query"] == "original question here"
    assert result["rewritten_query"] == "original question here"


@pytest.mark.unit
def test_tool_timeline_error_handled() -> None:
    """When extract_fn fails, timeline gracefully returns non-temporal result."""

    def failing_extract(query: str, reference_date: object = None) -> None:
        raise RuntimeError("oops")

    result = tool_timeline(query="any query", extract_fn=failing_extract)

    assert result["is_temporal"] is False
    assert result["rewritten_query"] == "any query"
    assert result["error"] == ""


@pytest.mark.unit
def test_tool_timeline_rewrite_none_returns_original() -> None:
    result = tool_timeline(
        query="last month update",
        extract_fn=_fake_extract_non_temporal,
        rewrite_fn=_fake_rewrite_none,
    )

    assert result["rewritten_query"] == "last month update"


# NOTE: the ``build_server`` defensive ImportError branch (when the optional
# ``mcp`` extra is not installed) is documented as ``# pragma: no cover`` in
# server.py. The branch is reachable only when ``pip install 'kairix[agents]'``
# has not been run; the test suite (which exercises ``build_server`` end-to-end
# in tests/integration/test_mcp_build_server.py) requires the extra.


# ---------------------------------------------------------------------------
# tool_usage_guide (TEST-5)
# ---------------------------------------------------------------------------


@pytest.fixture()
def guide_file(tmp_path: Path) -> Path:
    """Create a temporary agent-usage-guide.md."""
    guide = tmp_path / "agent-usage-guide.md"
    guide.write_text(
        "# Kairix Agent Usage Guide\n\n"
        "## Search\nHow to search the document store.\n\n"
        "## Budget\nToken budget controls cost.\nDefault budget is 3000 tokens.\n\n"
        "## Troubleshooting\nDebug tips for common issues.\n",
        encoding="utf-8",
    )
    return guide


@pytest.mark.unit
def test_tool_usage_guide_empty_topic(guide_file: Path) -> None:
    """Empty topic returns full guide content."""
    import kairix.agents.mcp.server as _mod

    server_file = Path(_mod.__file__)
    expected = server_file.parent.parent.parent / "docs" / "agent-usage-guide.md"
    if expected.exists():
        result = tool_usage_guide(topic="")
        assert result["error"] == ""
        assert len(result["content"]) > 0
    else:
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_text(guide_file.read_text(), encoding="utf-8")
        try:
            result = tool_usage_guide(topic="")
            assert result["error"] == ""
            assert "Kairix Agent Usage Guide" in result["content"]
        finally:
            expected.unlink(missing_ok=True)


@pytest.mark.unit
def test_tool_usage_guide_topic_filter(guide_file: Path) -> None:
    """Specific topic filters to relevant sections."""
    import kairix.agents.mcp.server as _mod

    server_file = Path(_mod.__file__)
    expected = server_file.parent.parent.parent / "docs" / "agent-usage-guide.md"
    if expected.exists():
        result = tool_usage_guide(topic="budget")
        assert result["error"] == ""
        assert "budget" in result["content"].lower()
    else:
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_text(guide_file.read_text(), encoding="utf-8")
        try:
            result = tool_usage_guide(topic="budget")
            assert result["error"] == ""
            assert "budget" in result["content"].lower()
        finally:
            expected.unlink(missing_ok=True)


@pytest.mark.unit
def test_tool_usage_guide_returns_error_when_explicit_guide_path_missing(tmp_path: Path) -> None:
    """Missing guide file returns error dict — uses guide_path injection (no monkeypatch)."""
    missing = tmp_path / "no-such-guide.md"
    result = tool_usage_guide(topic="anything", guide_path=missing)
    assert result["content"] == ""
    assert "Usage guide not found" in result["error"]
    assert result["topic"] == "anything"


@pytest.mark.unit
def test_tool_usage_guide_returns_full_text_when_topic_empty(tmp_path: Path) -> None:
    """Empty topic returns the entire guide content unchanged."""
    guide = tmp_path / "guide.md"
    guide.write_text("# Title\n\nFull content here.\n", encoding="utf-8")
    result = tool_usage_guide(topic="", guide_path=guide)
    assert result["error"] == ""
    assert result["content"] == "# Title\n\nFull content here.\n"


@pytest.mark.unit
def test_tool_usage_guide_returns_only_matching_section_when_topic_in_heading(tmp_path: Path) -> None:
    """A topic that matches a heading returns just that section's content."""
    guide = tmp_path / "guide.md"
    guide.write_text(
        "## Search\nUse the search tool.\n## Temporal\nDate-aware retrieval.\n## Budget\nBudget rules.\n",
        encoding="utf-8",
    )
    result = tool_usage_guide(topic="temporal", guide_path=guide)
    assert result["error"] == ""
    assert "Date-aware retrieval" in result["content"]
    assert "Budget rules" not in result["content"], "non-matching section leaked"
    assert "Use the search tool" not in result["content"], "non-matching section leaked"


@pytest.mark.unit
def test_tool_usage_guide_aggregates_multiple_matching_sections(tmp_path: Path) -> None:
    """Multiple sections matching the topic are concatenated.

    Closes coverage of the ``if in_section and current: sections.append(...)``
    branch that fires when a matching section ends at end-of-file (no trailing
    heading to flush it).
    """
    guide = tmp_path / "guide.md"
    guide.write_text(
        "## Temporal Anchors\n"
        "Anchor dates to a reference point.\n"
        "## Other\n"
        "Unrelated content.\n"
        "## Temporal Rewriting\n"
        "Rewrites queries with date filters.\n",
        encoding="utf-8",
    )
    result = tool_usage_guide(topic="temporal", guide_path=guide)
    assert "Anchor dates to a reference point" in result["content"]
    assert "Rewrites queries with date filters" in result["content"]
    assert "Unrelated content" not in result["content"]


@pytest.mark.unit
def test_tool_usage_guide_falls_back_to_keyword_match_when_no_heading_matches(tmp_path: Path) -> None:
    """When no heading matches, fall back to keyword-line search across the guide."""
    guide = tmp_path / "guide.md"
    guide.write_text(
        "# Kairix Agent Usage Guide\n\n"
        "## Overview\nKairix supports adaptive recall.\n\n"
        "## Search\nUse the search tool to recall documents.\n",
        encoding="utf-8",
    )
    result = tool_usage_guide(topic="recall", guide_path=guide)
    assert result["error"] == ""
    # Both lines containing "recall" appear in the fallback content.
    assert "adaptive recall" in result["content"]
    assert "to recall documents" in result["content"]


@pytest.mark.unit
def test_tool_usage_guide_falls_back_to_truncated_text_when_no_match(tmp_path: Path) -> None:
    """When no heading and no keyword line matches, return the first 2000 chars of the guide."""
    body = "Line that does not contain the topic at all. " * 100  # ~4500 chars
    guide = tmp_path / "guide.md"
    guide.write_text(body, encoding="utf-8")
    result = tool_usage_guide(topic="utterly-absent-keyword", guide_path=guide)
    assert result["error"] == ""
    assert len(result["content"]) <= 2000


@pytest.mark.unit
def test_tool_usage_guide_returns_error_dict_when_read_fails(tmp_path: Path) -> None:
    """A guide path that exists but can't be read (e.g. it's a directory) → error dict."""
    # A directory at the guide path: exists() returns True, read_text() raises IsADirectoryError.
    bad = tmp_path / "guide-but-actually-a-dir"
    bad.mkdir()
    result = tool_usage_guide(topic="any", guide_path=bad)
    assert result["content"] == ""
    assert "lookup failed" in result["error"]


# ---------------------------------------------------------------------------
# tool_contradict (WP7)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_contradict_returns_structure() -> None:
    fake_llm = MagicMock()
    result = tool_contradict(content="test claim", llm_backend=fake_llm, contradict_fn=_fake_contradict_empty)

    assert result["has_contradictions"] is False
    assert result["error"] == ""
    assert isinstance(result["contradictions"], list)
    assert result["content"] == "test claim"


@pytest.mark.unit
def test_tool_contradict_with_results() -> None:
    from kairix.knowledge.contradict.detector import ContradictionResult

    mock_results = [
        ContradictionResult(
            doc_path="notes/arch.md",
            score=0.85,
            reason="Architecture mismatch",
            snippet="Uses microservices",
        ),
    ]
    fake_llm = MagicMock()

    def fake_check(**kw: object) -> list:
        return mock_results

    result = tool_contradict(
        content="architecture uses monolith pattern",
        llm_backend=fake_llm,
        contradict_fn=fake_check,
    )

    assert result["has_contradictions"] is True
    assert len(result["contradictions"]) == 1
    assert result["contradictions"][0]["path"] == "notes/arch.md"
    assert result["contradictions"][0]["score"] == pytest.approx(0.85)
    assert result["error"] == ""


@pytest.mark.unit
def test_tool_contradict_error_handled() -> None:
    """On exception, returns error dict without raising."""

    def _failing_contradict(**kwargs):
        raise RuntimeError("no LLM")

    result = tool_contradict(content="anything", llm_backend=MagicMock(), contradict_fn=_failing_contradict)

    assert result["has_contradictions"] is False
    assert result["contradictions"] == []
    assert "failed" in result["error"].lower()


@pytest.mark.unit
def test_tool_contradict_default_agent() -> None:
    """Agent param is no longer passed to check_contradiction (searches all collections)."""
    captured: dict = {}

    def capturing_check(**kw: object) -> list:
        captured.update(kw)
        return []

    fake_llm = MagicMock()
    tool_contradict(content="claim", agent=None, llm_backend=fake_llm, contradict_fn=capturing_check)

    assert "agent" not in captured
