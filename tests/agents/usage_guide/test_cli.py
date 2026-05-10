"""Unit tests for ``kairix.agents.usage_guide.cli``."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from kairix.agents.usage_guide.cli import build_parser, format_text, main
from kairix.use_cases.usage_guide import UsageGuideDeps, UsageGuideOutput

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_build_parser_no_args() -> None:
    args = build_parser().parse_args([])
    assert args.topic == ""
    assert args.guide_path is None
    assert args.as_json is False


def test_build_parser_topic_and_guide_path(tmp_path: Path) -> None:
    args = build_parser().parse_args(["budget", "--guide-path", str(tmp_path / "g.md"), "--json"])
    assert args.topic == "budget"
    assert args.guide_path == tmp_path / "g.md"
    assert args.as_json is True


# ---------------------------------------------------------------------------
# format_text
# ---------------------------------------------------------------------------


def test_format_text_full_guide_returns_content_unchanged() -> None:
    out = UsageGuideOutput(topic="", content="Full guide markdown.")
    assert format_text(out) == "Full guide markdown."


def test_format_text_with_topic_adds_heading() -> None:
    out = UsageGuideOutput(topic="budget", content="Budget section.")
    rendered = format_text(out)
    assert "Usage guide — topic: budget" in rendered
    assert "==" in rendered  # underline of header
    assert "Budget section." in rendered


def test_format_text_short_circuits_on_error() -> None:
    out = UsageGuideOutput(error="Usage guide not found.")
    assert format_text(out).startswith("error:")


# ---------------------------------------------------------------------------
# main orchestrator — driven via UsageGuideDeps.
# ---------------------------------------------------------------------------


def _capture(argv: list[str], deps: UsageGuideDeps) -> tuple[int, str]:
    out_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(io.StringIO()):
        rc = main(argv, deps=deps)
    return rc, out_buf.getvalue()


def test_main_full_guide_text_output(tmp_path: Path) -> None:
    guide = tmp_path / "g.md"
    guide.write_text("# Title\n\n## Section\nBody.\n", encoding="utf-8")
    deps = UsageGuideDeps(resolve_guide_fn=lambda p: guide)
    rc, stdout = _capture([], deps)
    assert rc == 0
    assert "# Title" in stdout
    assert "Body." in stdout


def test_main_topic_filter_text_output(tmp_path: Path) -> None:
    guide = tmp_path / "g.md"
    guide.write_text("## Search\nA.\n\n## Budget\nB.\n", encoding="utf-8")
    deps = UsageGuideDeps(resolve_guide_fn=lambda p: guide)
    rc, stdout = _capture(["budget"], deps)
    assert rc == 0
    assert "topic: budget" in stdout
    assert "B." in stdout
    assert "A." not in stdout


def test_main_json_format_emits_envelope(tmp_path: Path) -> None:
    guide = tmp_path / "g.md"
    guide.write_text("hello", encoding="utf-8")
    deps = UsageGuideDeps(resolve_guide_fn=lambda p: guide)
    rc, stdout = _capture(["--json"], deps)
    assert rc == 0
    payload = json.loads(stdout)
    assert payload["content"] == "hello"
    assert payload["topic"] == ""


def test_main_missing_guide_returns_one(tmp_path: Path) -> None:
    deps = UsageGuideDeps(resolve_guide_fn=lambda p: tmp_path / "missing.md")
    rc, stdout = _capture([], deps)
    assert rc == 1
    assert "error:" in stdout
