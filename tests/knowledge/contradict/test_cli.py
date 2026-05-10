"""Unit tests for ``kairix.knowledge.contradict.cli`` pure helpers.

Phase 2 of #168 made the CLI a thin adapter — argv parsing + result
formatting only. The use case logic lives in
``kairix.use_cases.contradict.run_contradict`` (covered in
``tests/use_cases/test_contradict.py``). These tests pin the formatters.
"""

from __future__ import annotations

import json

import pytest

from kairix.knowledge.contradict.cli import build_parser, format_text, to_json_envelope
from kairix.use_cases.contradict import ContradictionHit, ContradictOutput

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_build_parser_check_subcommand_accepts_content() -> None:
    args = build_parser().parse_args(["check", "some claim"])
    assert args.subcommand == "check"
    assert args.content == "some claim"
    assert args.top_k == 5
    assert args.threshold == pytest.approx(0.45)
    assert args.top_claims == 3
    assert args.format == "text"
    assert args.agent == "shared"


def test_build_parser_check_accepts_all_flags() -> None:
    args = build_parser().parse_args(
        [
            "check",
            "claim",
            "--top-k",
            "8",
            "--threshold",
            "0.7",
            "--top-claims",
            "5",
            "--format",
            "json",
            "--agent",
            "builder",
        ]
    )
    assert args.top_k == 8
    assert args.threshold == pytest.approx(0.7)
    assert args.top_claims == 5
    assert args.format == "json"
    assert args.agent == "builder"


def test_build_parser_rejects_unknown_format() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["check", "claim", "--format", "yaml"])


# ---------------------------------------------------------------------------
# format_text
# ---------------------------------------------------------------------------


def test_format_text_no_contradictions_renders_default_message() -> None:
    out = ContradictOutput(content="claim")
    text = format_text(out, top_k=5, threshold=0.45)
    assert "No contradictions found" in text
    assert "top_k=5" in text
    assert "threshold=0.45" in text


def test_format_text_renders_each_hit_with_category_score_path() -> None:
    out = ContradictOutput(
        content="System uses A",
        contradictions=[
            ContradictionHit(
                path="docs/old.md",
                score=0.78,
                reason="contradicts X",
                snippet="The system uses option B." * 5,
                category="status_mismatch",
                claim="System uses A",
            ),
        ],
        has_contradictions=True,
    )
    text = format_text(out, top_k=5, threshold=0.45)
    assert "1 contradiction(s) found" in text
    assert "Category: status_mismatch" in text
    assert "Score: 0.78" in text
    assert "Path: docs/old.md" in text
    assert "Reason: contradicts X" in text
    assert "Snippet:" in text
    assert "..." in text  # snippet truncated at 120 chars


def test_format_text_short_circuits_on_error() -> None:
    out = ContradictOutput(content="c", error="ConnectionError: no Neo4j")
    text = format_text(out, top_k=5, threshold=0.45)
    assert text.startswith("error:")
    assert "ConnectionError" in text


# ---------------------------------------------------------------------------
# to_json_envelope
# ---------------------------------------------------------------------------


def test_to_json_envelope_returns_array_of_hit_dicts() -> None:
    out = ContradictOutput(
        content="c",
        contradictions=[
            ContradictionHit(
                path="docs/old.md",
                score=0.785432,
                reason="contradicts X",
                snippet="snippet",
                category="overstatement",
                claim="C",
            ),
        ],
        has_contradictions=True,
    )
    payload = to_json_envelope(out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    assert row["doc_path"] == "docs/old.md"
    assert row["score"] == pytest.approx(0.7854)  # rounded to 4 decimals
    assert row["reason"] == "contradicts X"
    assert row["category"] == "overstatement"
    assert row["claim"] == "C"
    # Round-trip via json to confirm serialisable.
    assert json.loads(json.dumps(payload)) == payload


def test_to_json_envelope_empty_returns_empty_array() -> None:
    out = ContradictOutput(content="c")
    assert to_json_envelope(out) == []
