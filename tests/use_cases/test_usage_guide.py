"""Unit tests for ``kairix.use_cases.usage_guide.run_usage_guide`` + helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.use_cases.usage_guide import (
    UsageGuideDeps,
    UsageGuideOutput,
    extract_topic_sections,
    run_usage_guide,
    usage_guide_output_to_envelope,
)

pytestmark = pytest.mark.unit


_SAMPLE = """# Kairix Agent Usage Guide

Welcome.

## Search
How to search the document store.

## Budget
Token budget controls cost.
Default budget is 3000 tokens.

## Troubleshooting
Debug tips for common issues.
"""


# ---------------------------------------------------------------------------
# extract_topic_sections — pure helper
# ---------------------------------------------------------------------------


def test_extract_returns_section_when_heading_matches() -> None:
    out = extract_topic_sections(_SAMPLE, "budget")
    assert "Default budget is 3000 tokens." in out
    # Other sections excluded
    assert "search the document store" not in out


def test_extract_concatenates_multiple_matching_sections() -> None:
    text = """## Foo budget
Body 1

## Bar
Body 2

### Budget detail
Body 3
"""
    out = extract_topic_sections(text, "budget")
    assert "Body 1" in out
    assert "Body 3" in out
    assert "Body 2" not in out


def test_extract_falls_back_to_keyword_lines_when_no_heading_matches() -> None:
    out = extract_topic_sections(_SAMPLE, "welcome")
    # 'Welcome.' matches no heading but the line itself contains the keyword.
    assert "Welcome." in out


def test_extract_falls_back_to_first_2000_chars_when_no_match() -> None:
    text = "x" * 3000
    out = extract_topic_sections(text, "no-such-thing")
    assert len(out) == 2000


# ---------------------------------------------------------------------------
# run_usage_guide
# ---------------------------------------------------------------------------


def test_full_guide_returned_when_topic_empty(tmp_path: Path) -> None:
    guide = tmp_path / "g.md"
    guide.write_text(_SAMPLE, encoding="utf-8")
    deps = UsageGuideDeps(resolve_guide_fn=lambda p: guide)

    out = run_usage_guide("", deps=deps)
    assert out.error == ""
    assert out.topic == ""
    assert "Kairix Agent Usage Guide" in out.content


def test_topic_filter_returns_section(tmp_path: Path) -> None:
    guide = tmp_path / "g.md"
    guide.write_text(_SAMPLE, encoding="utf-8")
    deps = UsageGuideDeps(resolve_guide_fn=lambda p: guide)

    out = run_usage_guide("budget", deps=deps)
    assert out.error == ""
    assert out.topic == "budget"
    assert "Default budget is 3000 tokens." in out.content
    assert "search the document store" not in out.content


def test_missing_guide_file_returns_operator_actionable_error(tmp_path: Path) -> None:
    deps = UsageGuideDeps(resolve_guide_fn=lambda p: tmp_path / "no-such.md")
    out = run_usage_guide(deps=deps)
    assert out.error.startswith("UsageGuideNotFound:")
    assert "kairix onboard guide" in out.error
    assert out.content == ""


def test_caller_can_pass_explicit_guide_path(tmp_path: Path) -> None:
    """When the caller passes guide_path, the deps' resolve sees it."""
    guide = tmp_path / "explicit.md"
    guide.write_text("explicit content", encoding="utf-8")

    captured: dict = {}

    def _resolver(p: Path | None) -> Path:
        captured["received"] = p
        return p or guide

    deps = UsageGuideDeps(resolve_guide_fn=_resolver)
    run_usage_guide(guide_path=guide, deps=deps)
    assert captured["received"] == guide


def test_resolver_failure_yields_error_envelope() -> None:
    def _boom(p: Path | None) -> Path:
        raise RuntimeError("filesystem broken")

    deps = UsageGuideDeps(resolve_guide_fn=_boom)
    out = run_usage_guide(deps=deps)
    assert out.error.startswith("RuntimeError:")


# ---------------------------------------------------------------------------
# Envelope projection
# ---------------------------------------------------------------------------


def test_envelope_includes_all_fields() -> None:
    out = UsageGuideOutput(topic="t", content="c")
    env = usage_guide_output_to_envelope(out)
    assert env == {"topic": "t", "content": "c", "error": ""}
