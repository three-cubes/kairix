"""Unit tests for ``kairix.agents.research.cli``."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

import pytest

from kairix.agents.research.cli import build_parser, format_text, main
from kairix.use_cases.research import ResearchDeps, ResearchOutput

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_build_parser_minimal_invocation() -> None:
    args = build_parser().parse_args(["a question"])
    assert args.query == "a question"
    assert args.max_turns == 4
    assert args.as_json is False


def test_build_parser_accepts_max_turns_and_json() -> None:
    args = build_parser().parse_args(["q", "--max-turns", "8", "--json"])
    assert args.max_turns == 8
    assert args.as_json is True


# ---------------------------------------------------------------------------
# format_text
# ---------------------------------------------------------------------------


def test_format_text_renders_synthesis_confidence_turns() -> None:
    out = ResearchOutput(query="q", synthesis="answer", turns=3, confidence=0.75)
    text = format_text(out)
    assert "Query:      q" in text
    assert "Turns:      3" in text
    assert "Confidence: 0.75" in text
    assert "answer" in text


def test_format_text_renders_gaps_and_chunks() -> None:
    out = ResearchOutput(
        query="q",
        synthesis="answer",
        gaps=["unknown about X"],
        retrieved_chunks=[{"path": "/c1"}, {"path": "/c2"}],
        turns=1,
        confidence=0.4,
    )
    text = format_text(out)
    assert "unknown about X" in text
    assert "/c1" in text


def test_format_text_chunks_truncated_at_5() -> None:
    out = ResearchOutput(
        query="q",
        synthesis="s",
        retrieved_chunks=[{"path": f"/c{i}"} for i in range(8)],
    )
    text = format_text(out)
    assert "/c0" in text
    assert "/c4" in text
    assert "3 more" in text  # 8 total - 5 shown = 3 more


def test_format_text_no_synthesis_shows_placeholder() -> None:
    out = ResearchOutput(query="q", synthesis="")
    assert "(no synthesis returned)" in format_text(out)


def test_format_text_short_circuits_on_error() -> None:
    out = ResearchOutput(query="q", error="ConnectionError: no LLM")
    text = format_text(out)
    assert text.startswith("error:")
    assert "ConnectionError" in text


# ---------------------------------------------------------------------------
# main orchestrator — driven via ResearchDeps.
# ---------------------------------------------------------------------------


def _capture(argv: list[str], deps: ResearchDeps) -> tuple[int, str]:
    out_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(io.StringIO()):
        rc = main(argv, deps=deps)
    return rc, out_buf.getvalue()


def test_main_text_format_prints_synthesis() -> None:
    deps = ResearchDeps(
        research_fn=lambda **kw: {
            "query": kw["query"],
            "synthesis": "the answer",
            "turns": 2,
            "confidence": 0.7,
        }
    )
    rc, stdout = _capture(["my question"], deps)
    assert rc == 0
    assert "the answer" in stdout
    assert "Confidence: 0.70" in stdout


def test_main_json_format_emits_envelope() -> None:
    deps = ResearchDeps(research_fn=lambda **kw: {"query": kw["query"], "synthesis": "x"})
    rc, stdout = _capture(["q", "--json"], deps)
    assert rc == 0
    payload = json.loads(stdout)
    assert payload["query"] == "q"
    assert payload["synthesis"] == "x"


def test_main_use_case_error_exits_one() -> None:
    def _boom(**kw: Any) -> Any:
        raise RuntimeError("LLM down")

    deps = ResearchDeps(research_fn=_boom)
    rc, stdout = _capture(["q"], deps)
    assert rc == 1
    assert "error:" in stdout


def test_main_passes_max_turns_to_use_case() -> None:
    captured: dict = {}

    def _research(**kwargs: Any) -> dict:
        captured.update(kwargs)
        return {}

    deps = ResearchDeps(research_fn=_research)
    _capture(["q", "--max-turns", "7"], deps)
    assert captured["max_turns"] == 7
