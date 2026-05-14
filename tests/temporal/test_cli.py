"""Unit tests for ``kairix.core.temporal.cli``.

The CLI is a thin adapter — its job is argv parsing + result formatting.
Logic belongs to ``run_timeline``. These tests drive each pure helper
(``build_parser``, ``parse_iso_or_die``, ``format_header``,
``format_results``) directly, so coverage isn't gated on a populated
temporal index.

The success-path "argv → use case → stdout" smoke is covered by the
existing BDD scenarios in ``tests/bdd/test_timeline_cli.py``.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr

import pytest

from kairix.core.temporal.cli import (
    build_parser,
    format_header,
    format_results,
    parse_iso_or_die,
)
from kairix.use_cases.timeline import TimelineHit, TimelineResult


@pytest.mark.unit
def test_build_parser_accepts_query_only() -> None:
    p = build_parser()
    args = p.parse_args(["what happened last week"])
    assert args.query == "what happened last week"
    assert args.since is None
    assert args.until is None
    assert args.limit == 10
    assert args.chunk_type == "all"


@pytest.mark.unit
def test_build_parser_accepts_all_flags() -> None:
    p = build_parser()
    args = p.parse_args(
        [
            "topic",
            "--since",
            "2026-04-01",
            "--until",
            "2026-04-30",
            "--limit",
            "25",
            "--type",
            "board_card",
        ]
    )
    assert args.since == "2026-04-01"
    assert args.until == "2026-04-30"
    assert args.limit == 25
    assert args.chunk_type == "board_card"


@pytest.mark.unit
def test_parse_iso_or_die_returns_none_for_missing_value() -> None:
    assert parse_iso_or_die(None, "--since") is None
    assert parse_iso_or_die("", "--since") is None


@pytest.mark.unit
def test_parse_iso_or_die_parses_valid_iso() -> None:
    from datetime import date

    assert parse_iso_or_die("2026-04-15", "--since") == date(2026, 4, 15)


@pytest.mark.unit
def test_parse_iso_or_die_exits_on_invalid_iso(capsys: pytest.CaptureFixture[str]) -> None:
    err_buf = io.StringIO()
    with pytest.raises(SystemExit) as excinfo, redirect_stderr(err_buf):
        parse_iso_or_die("not-a-date", "--since")
    assert excinfo.value.code == 1
    assert "invalid --since date" in err_buf.getvalue()
    assert "not-a-date" in err_buf.getvalue()


@pytest.mark.unit
def test_format_header_with_temporal_window() -> None:
    result = TimelineResult(
        original_query="last week kairix",
        rewritten_query="last week kairix 2026-05-01..2026-05-07",
        is_temporal=True,
        fell_back=False,
        time_window={"start": "2026-05-01", "end": "2026-05-07"},
    )
    header = format_header(result, limit=10)
    assert "Query:    last week kairix" in header
    assert "Rewritten: last week kairix 2026-05-01..2026-05-07" in header
    assert "Window:   2026-05-01 → 2026-05-07" in header
    assert "Limit:    10" in header
    assert "Note:" not in header  # not fell_back


@pytest.mark.unit
def test_format_header_with_open_window_renders_earliest_latest() -> None:
    result = TimelineResult(
        original_query="q",
        rewritten_query="q",
        is_temporal=True,
        fell_back=False,
        time_window={"start": "", "end": "2026-05-07"},
    )
    header = format_header(result, limit=10)
    assert "Window:   earliest → 2026-05-07" in header

    result2 = TimelineResult(
        original_query="q",
        rewritten_query="q",
        is_temporal=True,
        fell_back=False,
        time_window={"start": "2026-05-01", "end": ""},
    )
    assert "Window:   2026-05-01 → latest" in format_header(result2, limit=10)


@pytest.mark.unit
def test_format_header_no_window_says_no_date_filter() -> None:
    result = TimelineResult(
        original_query="q",
        rewritten_query="q",
        is_temporal=False,
        fell_back=True,
        time_window={},
    )
    header = format_header(result, limit=5)
    assert "Window:   (no date filter — showing all)" in header
    assert "Limit:    5" in header
    assert "Note:     primary temporal index empty — showing search-pipeline fallback" in header


@pytest.mark.unit
def test_format_results_empty_returns_no_results_message() -> None:
    result = TimelineResult(original_query="q", rewritten_query="q", is_temporal=False, fell_back=True, time_window={})
    assert format_results(result) == "No results found."


@pytest.mark.unit
def test_format_results_renders_each_hit_with_source_and_preview() -> None:
    result = TimelineResult(
        original_query="q",
        rewritten_query="q",
        is_temporal=True,
        fell_back=False,
        time_window={"start": "2026-04-01", "end": "2026-04-30"},
        results=[
            TimelineHit(
                path="boards/sprint.md",
                title="Done",
                snippet="card body that describes some work",
                score=2.7,
                date="2026-04-15",
                chunk_type="board_card",
            ),
        ],
    )
    rendered = format_results(result)
    assert "Found 1 result(s):" in rendered
    assert "[1] 2026-04-15  board_card  Done" in rendered
    assert "Source: boards/sprint.md" in rendered
    assert "card body that describes some work" in rendered


@pytest.mark.unit
def test_format_results_truncates_long_snippet_with_ellipsis() -> None:
    long_snippet = "x" * 250
    result = TimelineResult(
        original_query="q",
        rewritten_query="q",
        is_temporal=False,
        fell_back=True,
        time_window={},
        results=[TimelineHit(path="/p", title="", snippet=long_snippet, score=0.0)],
    )
    rendered = format_results(result)
    # 200-char preview + ellipsis
    assert "x" * 200 + "…" in rendered
    # Empty title leaves the trailing whitespace stripped via .rstrip()
    assert "[1] undated  search" in rendered


@pytest.mark.unit
def test_format_results_collapses_newlines_in_preview() -> None:
    snippet_with_newlines = "line one\nline two\nline three"
    result = TimelineResult(
        original_query="q",
        rewritten_query="q",
        is_temporal=False,
        fell_back=True,
        time_window={},
        results=[TimelineHit(path="/p", title="t", snippet=snippet_with_newlines, score=0.0)],
    )
    rendered = format_results(result)
    assert "line one line two line three" in rendered
    # No raw newlines from the snippet itself appear in the line
    assert "line one\n     line two" not in rendered


# ---------------------------------------------------------------------------
# main() — drives the adapter end-to-end with run_timeline swapped to a stub.
# ---------------------------------------------------------------------------


def _run_main(argv: list[str]) -> tuple[str, str, int]:
    """Drive ``kairix.core.temporal.cli.main(argv)`` and capture stdio + exit."""
    from contextlib import redirect_stdout

    from kairix.core.temporal.cli import main as timeline_main

    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            timeline_main(argv)
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    return out.getvalue(), err.getvalue(), code


@pytest.mark.unit
def test_main_renders_header_and_results_when_use_case_returns_hits(monkeypatch) -> None:
    """main() prints header + results when run_timeline returns a non-empty TimelineResult."""
    import kairix.use_cases.timeline as use_case

    def _fake_run_timeline(query: str, **kw) -> TimelineResult:
        return TimelineResult(
            original_query=query,
            rewritten_query=query + " rewritten",
            is_temporal=True,
            fell_back=False,
            time_window={"start": "2026-04-01", "end": "2026-04-30"},
            results=[
                TimelineHit(
                    path="boards/sprint.md",
                    title="Done card",
                    snippet="some work",
                    score=1.0,
                    date="2026-04-15",
                    chunk_type="board_card",
                )
            ],
        )

    monkeypatch.setattr(use_case, "run_timeline", _fake_run_timeline)

    stdout, _stderr, code = _run_main(["any query"])
    assert code == 0
    assert "Query:    any query" in stdout
    assert "Found 1 result(s):" in stdout
    assert "Done card" in stdout


@pytest.mark.unit
def test_main_exits_1_when_use_case_reports_error(monkeypatch) -> None:
    """main() exits 1 and prints the error to stderr when use case returns an error."""
    import kairix.use_cases.timeline as use_case

    def _fake_run_timeline(query: str, **kw) -> TimelineResult:
        return TimelineResult(
            original_query=query,
            rewritten_query=query,
            is_temporal=False,
            fell_back=True,
            time_window={},
            error="index missing",
        )

    monkeypatch.setattr(use_case, "run_timeline", _fake_run_timeline)
    _stdout, stderr, code = _run_main(["topic"])
    assert code == 1
    assert "error: index missing" in stderr


@pytest.mark.unit
def test_main_passes_overrides_to_use_case(monkeypatch) -> None:
    """The --since / --until / --type / --limit flags reach run_timeline."""
    from datetime import date as _date

    import kairix.use_cases.timeline as use_case

    captured: dict = {}

    def _fake_run_timeline(query: str, **kw) -> TimelineResult:
        captured["query"] = query
        captured["since"] = kw.get("since")
        captured["until"] = kw.get("until")
        captured["chunk_types"] = kw.get("chunk_types")
        captured["limit"] = kw.get("limit")
        return TimelineResult(
            original_query=query,
            rewritten_query=query,
            is_temporal=True,
            fell_back=False,
            time_window={},
        )

    monkeypatch.setattr(use_case, "run_timeline", _fake_run_timeline)
    _stdout, _stderr, code = _run_main(
        ["topic", "--since", "2026-04-01", "--until", "2026-04-30", "--type", "board_card", "--limit", "5"]
    )
    assert code == 0
    assert captured == {
        "query": "topic",
        "since": _date(2026, 4, 1),
        "until": _date(2026, 4, 30),
        "chunk_types": ["board_card"],
        "limit": 5,
    }


@pytest.mark.unit
def test_main_passes_none_chunk_types_when_type_all(monkeypatch) -> None:
    """--type=all (default) translates to chunk_types=None."""
    import kairix.use_cases.timeline as use_case

    seen: dict = {}

    def _fake_run_timeline(query: str, **kw) -> TimelineResult:
        seen["chunk_types"] = kw.get("chunk_types")
        return TimelineResult(
            original_query=query, rewritten_query=query, is_temporal=False, fell_back=True, time_window={}
        )

    monkeypatch.setattr(use_case, "run_timeline", _fake_run_timeline)
    _run_main(["topic"])
    assert seen["chunk_types"] is None
