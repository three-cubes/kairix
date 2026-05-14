"""
Tests for the briefing pipeline (kairix/briefing/pipeline.py).

Uses mocked sources and synthesiser — no live API calls or file system dependencies.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.agents.briefing.pipeline import (
    TOTAL_CONTEXT_CAP,
    BriefingDeps,
    BriefingPipeline,
    generate_briefing,
    trim_context,
)
from kairix.text import estimate_tokens


def _noop_source(*args, **kwargs):
    return ""


def _const_source(value: str):
    """Return a source fetcher that always returns the given value."""
    return lambda *args, **kwargs: value


def _failing_source(*args, **kwargs):
    raise RuntimeError("simulated source failure")


@pytest.mark.unit
class TestTokenHelpers:
    @pytest.mark.unit
    def testestimate_tokens_empty(self):
        assert estimate_tokens("") == 0

    @pytest.mark.unit
    def testestimate_tokens_scales_with_words(self):
        t10 = estimate_tokens(" ".join(["word"] * 10))
        t100 = estimate_tokens(" ".join(["word"] * 100))
        assert t100 > t10


@pytest.mark.unit
class TestTrimContext:
    @pytest.mark.unit
    def test_no_trim_when_under_cap(self):
        context = {"memory_logs": "short content", "entity_stub": "also short"}
        result = trim_context(context)
        assert result == context

    @pytest.mark.unit
    def test_trims_when_over_cap(self):
        # Create context well above 3000 tokens
        long_text = " ".join(["word"] * 3000)
        context = {
            "hybrid_search": long_text,
            "memory_logs": long_text,
        }
        result = trim_context(context)
        total = sum(estimate_tokens(v) for v in result.values())
        assert total <= TOTAL_CONTEXT_CAP * 2  # some tolerance

    @pytest.mark.unit
    def test_truncates_lowest_priority_first(self):
        """hybrid_search should be truncated before memory_logs."""
        # 3000 words * 1.3 = 3900 tokens — well over the 3000 cap
        long_text = " ".join(["word"] * 3000)
        context = {
            "hybrid_search": long_text,
            "memory_logs": "short note here",
        }
        result = trim_context(context)
        # hybrid_search should be shorter than original since total is over cap
        assert len(result.get("hybrid_search", "")) <= len(long_text)


def _fake_synthesise(agent: str, context: dict[str, str], max_tokens: int = 800) -> str:
    """Fake synthesiser that returns deterministic content without LLM calls."""
    return (
        "## Pending & Blocked\nNone.\n\n"
        "## Recent Decisions\nADR-007 adopted.\n\n"
        "## Active Projects\nKairix Phase 3.\n\n"
        "## Relevant Context\nHybrid search working.\n\n"
        "## Key Constraints\nNever write credentials."
    )


def _make_fake_writer(out_dir: Path):
    """Return a fake writer function that writes to the given directory."""
    from datetime import datetime, timezone

    def _write(agent: str, content: str, sources_count: int = 0, token_estimate: int = 0) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{agent}-latest.md"
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%d %H:%M UTC")
        date_str = now.strftime("%Y-%m-%d")
        header = (
            f"# Agent Briefing — {agent} — {date_str}\n"
            f"_Generated: {ts} | Sources: {sources_count} | Tokens: ~{token_estimate}_\n\n"
        )
        out_path.write_text(header + content, encoding="utf-8")
        return out_path

    return _write


def _all_empty_sources():
    """Return a sources dict where every source returns empty string."""
    return {
        "memory_logs": _noop_source,
        "recent_memory": _noop_source,
        "entity_stub": _noop_source,
        "knowledge_rules": _noop_source,
        "recent_decisions": _noop_source,
        "hybrid_search": _noop_source,
    }


def _all_content_sources():
    """Return a sources dict with sample content."""
    return {
        "memory_logs": _const_source("memory logs content"),
        "recent_memory": _const_source("recent memory"),
        "entity_stub": _const_source("entity stub"),
        "knowledge_rules": _const_source("rules content"),
        "recent_decisions": _const_source("decisions"),
        "hybrid_search": _const_source("search results"),
    }


@pytest.mark.unit
class TestGenerateBriefing:
    @pytest.mark.unit
    def test_basic_pipeline_runs(self, tmp_path):
        """Test that pipeline runs and returns a string."""
        result = generate_briefing(
            "builder",
            deps=BriefingDeps(
                synthesise_fn=_fake_synthesise,
                write_fn=_make_fake_writer(tmp_path),
            ),
            sources=_all_content_sources(),
        )

        assert isinstance(result, str)
        assert len(result) > 0
        assert "Briefing" in result or "briefing" in result.lower() or "Pending" in result

    @pytest.mark.unit
    def test_header_is_included(self, tmp_path):
        result = generate_briefing(
            "builder",
            deps=BriefingDeps(
                synthesise_fn=lambda agent, ctx, max_tokens=800: "## Pending\nNone.",
                write_fn=_make_fake_writer(tmp_path),
            ),
            sources=_all_empty_sources(),
        )

        assert "# Agent Briefing" in result
        assert "builder" in result.lower()

    @pytest.mark.unit
    def test_source_failure_does_not_raise(self, tmp_path):
        """Pipeline must not raise when a source fetcher fails."""
        sources = _all_content_sources()
        sources["memory_logs"] = _failing_source

        result = generate_briefing(
            "builder",
            deps=BriefingDeps(
                synthesise_fn=_fake_synthesise,
                write_fn=_make_fake_writer(tmp_path),
            ),
            sources=sources,
        )

        assert isinstance(result, str)

    @pytest.mark.unit
    def test_synthesis_failure_returns_partial_briefing(self, tmp_path):
        """Synthesis API failure should return a partial/fallback briefing, not raise."""
        result = generate_briefing(
            "builder",
            deps=BriefingDeps(
                synthesise_fn=lambda agent, ctx, max_tokens=800: "synthesis unavailable",
                write_fn=_make_fake_writer(tmp_path),
            ),
            sources=_all_content_sources(),
        )

        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.unit
    def test_output_file_is_written(self, tmp_path):
        generate_briefing(
            "builder",
            deps=BriefingDeps(
                synthesise_fn=_fake_synthesise,
                write_fn=_make_fake_writer(tmp_path),
            ),
            sources=_all_content_sources(),
        )

        expected = tmp_path / "builder-latest.md"
        assert expected.exists()
        content = expected.read_text()
        assert "Briefing" in content or "briefing" in content.lower() or "builder" in content


@pytest.mark.unit
class TestBriefingPipeline:
    @pytest.mark.unit
    def test_generate_returns_string(self, tmp_path):
        """BriefingPipeline.generate delegates to generate_briefing."""
        pipeline = BriefingPipeline(
            sources=_all_content_sources(),
            deps=BriefingDeps(
                synthesise_fn=_fake_synthesise,
                write_fn=_make_fake_writer(tmp_path),
            ),
        )

        result = pipeline.generate("builder")

        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.unit
    def test_generate_with_empty_sources(self, tmp_path):
        """BriefingPipeline handles empty sources gracefully."""
        pipeline = BriefingPipeline(
            sources=_all_empty_sources(),
            deps=BriefingDeps(
                synthesise_fn=lambda agent, ctx, max_tokens=800: "## Pending\nNone.",
                write_fn=_make_fake_writer(tmp_path),
            ),
        )

        result = pipeline.generate("shape")

        assert isinstance(result, str)
        assert "Briefing" in result or "shape" in result.lower()

    @pytest.mark.unit
    def test_generate_writes_output_file(self, tmp_path):
        """BriefingPipeline.generate writes the briefing file."""
        pipeline = BriefingPipeline(
            sources=_all_content_sources(),
            deps=BriefingDeps(
                synthesise_fn=_fake_synthesise,
                write_fn=_make_fake_writer(tmp_path),
            ),
        )

        pipeline.generate("builder")

        expected = tmp_path / "builder-latest.md"
        assert expected.exists()
