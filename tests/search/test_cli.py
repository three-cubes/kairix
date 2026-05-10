"""Tests for kairix.core.search.cli — search CLI entry point.

Drives the public ``main(argv, *, pipeline=...)`` only; argument parsing
and result formatting are exercised via the operator-visible CLI surface
(stdout, exit code, the args the pipeline receives).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from kairix.core.search.cli import main
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchResult

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Local fakes — minimal CLI-shape stand-ins for SearchPipeline + result rows.
#
# These don't belong in tests/fakes.py (yet): the production SearchPipeline
# already has a canonical Fake there for the search module, but the CLI's
# `result.result.title/snippet/...` rendering shape is its own surface and
# the test needs row-level control. Keep them close to the test.
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    path: str = "docs/test.md"
    title: str = "Test Doc"
    snippet: str = "A test snippet"
    boosted_score: float = 0.95
    collection: str = "shared"


@dataclass
class _BudgetedRow:
    result: _Row
    tier: str = "T1"
    content: str = "snippet content"


class _RecordingPipeline:
    """SearchPipeline-shaped fake that records the kwargs it was called with.

    Tests assert on ``last_call`` to verify CLI flags reach the pipeline
    boundary, and on ``self._result`` to control what the CLI renders.
    """

    def __init__(self, result: SearchResult | None = None) -> None:
        self._result = result or SearchResult(
            query="test",
            intent=QueryIntent.SEMANTIC,
            results=[_BudgetedRow(result=_Row())],
            bm25_count=1,
            vec_count=1,
            fused_count=1,
            total_tokens=100,
            latency_ms=5.0,
        )
        self.last_call: dict | None = None

    def search(self, *, query: str, budget: int, scope: str, agent: str | None) -> SearchResult:
        self.last_call = {"query": query, "budget": budget, "scope": scope, "agent": agent}
        self._result.query = query
        return self._result


# ---------------------------------------------------------------------------
# Argument parsing — exercised through main() via the recording pipeline.
# ---------------------------------------------------------------------------


def test_main_query_is_positional_and_required() -> None:
    """Running with no argv exits 2 (argparse usage error)."""
    pipeline = _RecordingPipeline()
    with pytest.raises(SystemExit) as exc:
        main([], pipeline=pipeline)
    assert exc.value.code == 2
    # Pipeline must not have been invoked when argparse rejects argv.
    assert pipeline.last_call is None


def test_main_default_flag_values_reach_pipeline() -> None:
    """A bare query uses scope='shared+agent', budget=3000, agent=None."""
    pipeline = _RecordingPipeline()
    main(["hello world"], pipeline=pipeline)
    assert pipeline.last_call == {
        "query": "hello world",
        "scope": "shared+agent",
        "budget": 3000,
        "agent": None,
    }


def test_main_passes_all_flags_to_pipeline() -> None:
    """--agent, --scope, --budget all flow through to the pipeline call."""
    pipeline = _RecordingPipeline()
    main(
        ["q", "--agent", "builder", "--scope", "agent", "--budget", "500"],
        pipeline=pipeline,
    )
    assert pipeline.last_call == {
        "query": "q",
        "agent": "builder",
        "scope": "agent",
        "budget": 500,
    }


def test_main_limit_truncates_rendered_results(capsys: pytest.CaptureFixture[str]) -> None:
    """--limit N caps how many rows the CLI renders, regardless of pipeline output."""
    many_rows = SearchResult(
        query="q",
        intent=QueryIntent.SEMANTIC,
        results=[_BudgetedRow(result=_Row(path=f"docs/r{i}.md", title=f"Doc {i}")) for i in range(5)],
    )
    pipeline = _RecordingPipeline(result=many_rows)
    main(["q", "--limit", "2"], pipeline=pipeline)
    out = capsys.readouterr().out
    assert "Doc 0" in out
    assert "Doc 1" in out
    assert "Doc 2" not in out, f"--limit 2 should have truncated row 2; got:\n{out}"


# ---------------------------------------------------------------------------
# Output formatting — exercised through main()'s stdout.
# ---------------------------------------------------------------------------


def test_main_no_results_message_shown(capsys: pytest.CaptureFixture[str]) -> None:
    """When the pipeline returns zero rows the CLI tells the operator so."""
    empty = SearchResult(query="test", intent=QueryIntent.SEMANTIC)
    main(["test"], pipeline=_RecordingPipeline(result=empty))
    out = capsys.readouterr().out
    assert "No results found" in out
    assert "test" in out


def test_main_text_output_includes_query(capsys: pytest.CaptureFixture[str]) -> None:
    """Text-mode output echoes the query so operators can confirm what ran."""
    main(["my query"], pipeline=_RecordingPipeline())
    out = capsys.readouterr().out
    assert "my query" in out


def test_main_json_output_is_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    """--json emits a single parseable JSON document with query + results keys."""
    main(["my query", "--json"], pipeline=_RecordingPipeline())
    data = json.loads(capsys.readouterr().out)
    assert data["query"] == "my query"
    assert "results" in data


def test_main_exits_1_on_error_and_prints_error_message(capsys: pytest.CaptureFixture[str]) -> None:
    """A SearchResult with .error set exits 1 and surfaces the message to stdout."""
    error_result = SearchResult(query="test", intent=QueryIntent.SEMANTIC, error="something broke")
    pipeline = _RecordingPipeline(result=error_result)
    with pytest.raises(SystemExit) as exc:
        main(["test"], pipeline=pipeline)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Error: something broke" in out
