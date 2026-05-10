"""Unit tests for ``kairix.agents.prep.cli`` pure helpers + main orchestrator."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.agents.prep.cli import build_parser, format_text, main
from kairix.use_cases.prep import PrepDeps, PrepOutput

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_build_parser_minimal_invocation() -> None:
    args = build_parser().parse_args(["a topic"])
    assert args.query == "a topic"
    assert args.tier == "l0"
    assert args.agent is None
    assert args.scope == "shared+agent"
    assert args.as_json is False


def test_build_parser_accepts_all_flags() -> None:
    args = build_parser().parse_args(["q", "--tier", "l1", "--agent", "builder", "--scope", "agent", "--json"])
    assert args.tier == "l1"
    assert args.agent == "builder"
    assert args.scope == "agent"
    assert args.as_json is True


def test_build_parser_rejects_invalid_tier() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["q", "--tier", "l5"])


# ---------------------------------------------------------------------------
# format_text
# ---------------------------------------------------------------------------


def test_format_text_with_summary_and_sources() -> None:
    out = PrepOutput(
        query="q",
        tier="l0",
        summary="brief summary",
        tokens=12,
        sources=["doc-a", "doc-b"],
    )
    text = format_text(out)
    assert "Query: q" in text
    assert "Tier:  l0" in text
    assert "brief summary" in text
    assert "Sources:" in text
    assert "- doc-a" in text


def test_format_text_no_sources_omits_section() -> None:
    out = PrepOutput(query="q", tier="l0", summary="s")
    text = format_text(out)
    assert "Sources:" not in text


def test_format_text_short_circuits_on_error() -> None:
    out = PrepOutput(query="q", tier="l0", error="ConnectionError: KAIRIX_NEO4J_URI down")
    text = format_text(out)
    assert text.startswith("error:")
    assert "ConnectionError" in text


# ---------------------------------------------------------------------------
# main orchestrator — driven through PrepDeps
# ---------------------------------------------------------------------------


@dataclass
class _FakeInner:
    title: str = ""
    path: str = ""


@dataclass
class _FakeBudgeted:
    result: _FakeInner
    content: str = ""


@dataclass
class _FakeSearchResult:
    results: list[_FakeBudgeted] = field(default_factory=list)


def _capture_main(argv: list[str], deps: PrepDeps) -> tuple[int, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    rc = main(argv, deps=deps)
    return rc, out_buf.getvalue() + err_buf.getvalue()


def _capture(argv: list[str], deps: PrepDeps) -> tuple[int, str]:
    out_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(io.StringIO()):
        rc = main(argv, deps=deps)
    return rc, out_buf.getvalue()


def test_main_text_format_prints_summary_and_returns_zero() -> None:
    sr = _FakeSearchResult(results=[_FakeBudgeted(result=_FakeInner(title="doc-a"), content="alpha")])
    deps = PrepDeps(
        search_fn=lambda **kw: sr,
        chat_fn=lambda **kw: "alpha summary",
    )
    rc, stdout = _capture(["topic"], deps)
    assert rc == 0
    assert "alpha summary" in stdout
    assert "doc-a" in stdout


def test_main_json_format_emits_envelope() -> None:
    sr = _FakeSearchResult(results=[_FakeBudgeted(result=_FakeInner(title="d"), content="s")])
    deps = PrepDeps(search_fn=lambda **kw: sr, chat_fn=lambda **kw: "summary")
    rc, stdout = _capture(["q", "--json"], deps)
    assert rc == 0
    payload = json.loads(stdout)
    assert payload["query"] == "q"
    assert payload["tier"] == "l0"
    assert payload["summary"] == "summary"


def test_main_use_case_error_exits_one() -> None:
    def _boom(**kw: Any) -> Any:
        raise RuntimeError("Neo4j down")

    deps = PrepDeps(search_fn=_boom, chat_fn=lambda **kw: "")
    rc, stdout = _capture(["q"], deps)
    assert rc == 1
    assert "error:" in stdout
    assert "RuntimeError" in stdout


def test_main_passes_tier_and_scope_to_use_case() -> None:
    captured: dict = {}

    def _search(**kwargs: Any) -> _FakeSearchResult:
        captured.update(kwargs)
        return _FakeSearchResult()

    deps = PrepDeps(search_fn=_search, chat_fn=lambda **kw: "")
    _capture(["q", "--tier", "l1", "--scope", "agent"], deps)
    assert captured["budget"] == 3000  # l1 budget
    # Scope is parsed to Scope.AGENT
    from kairix.core.search.scope import Scope

    assert captured["scope"] is Scope.AGENT
