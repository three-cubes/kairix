"""Unit tests for ``kairix.core.search.cli`` pure helpers.

Phase 2 of #168 made the CLI a thin adapter — argv parsing + result
formatting only. The use case logic lives in
``kairix.use_cases.search.run_search`` (covered in
``tests/use_cases/test_search.py``). These tests pin the formatters
that turn a ``SearchOutput`` into stdout and the JSON envelope.
"""

from __future__ import annotations

import json

import pytest

from kairix.core.search.cli import build_parser, format_text, to_json_envelope
from kairix.use_cases.search import SearchHit, SearchOutput

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_build_parser_accepts_query_only() -> None:
    args = build_parser().parse_args(["q"])
    assert args.query == "q"
    assert args.agent is None
    assert args.scope == "shared+agent"
    assert args.budget == 3000
    assert args.limit == 10
    assert args.as_json is False
    assert args.include_entity_card is True


def test_build_parser_accepts_all_flags() -> None:
    args = build_parser().parse_args(
        [
            "q",
            "--agent",
            "builder",
            "--scope",
            "all-agents",
            "--budget",
            "5000",
            "--limit",
            "25",
            "--json",
            "--no-entity-card",
        ]
    )
    assert args.agent == "builder"
    assert args.scope == "all-agents"
    assert args.budget == 5000
    assert args.limit == 25
    assert args.as_json is True
    assert args.include_entity_card is False


def test_build_parser_rejects_unknown_scope() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["q", "--scope", "bogus"])


# ---------------------------------------------------------------------------
# format_text
# ---------------------------------------------------------------------------


def _hit(**kwargs: object) -> SearchHit:
    """Build a SearchHit with sensible test defaults."""
    return SearchHit(
        path=kwargs.get("path", "/p"),  # type: ignore[arg-type]  # narrow to str at use site
        title=kwargs.get("title", ""),  # type: ignore[arg-type]  # narrow to str at use site
        snippet=kwargs.get("snippet", ""),  # type: ignore[arg-type]  # narrow to str at use site
        score=float(kwargs.get("score", 0.0)),  # type: ignore[arg-type]  # narrow to float at use site
        tier=kwargs.get("tier", ""),  # type: ignore[arg-type]  # narrow to str at use site
        collection=kwargs.get("collection", ""),  # type: ignore[arg-type]  # narrow to str at use site
    )


def test_format_text_renders_query_intent_and_diagnostics() -> None:
    out = SearchOutput(
        query="my query",
        intent="semantic",
        results=[],
        bm25_count=4,
        vec_count=6,
        fused_count=8,
        total_tokens=100,
        latency_ms=42.0,
    )
    text = format_text(out)
    assert "Query: my query" in text
    assert "Intent: semantic" in text
    assert "BM25=4" in text
    assert "vec=6" in text
    assert "100 tokens" in text
    assert "42ms" in text


def test_format_text_marks_vec_failed() -> None:
    out = SearchOutput(query="q", intent="semantic", vec_failed=True)
    assert "vec_failed=True" in format_text(out)


def test_format_text_renders_each_hit_with_path_score_collection() -> None:
    hit = _hit(path="docs/notes.md", title="Notes", snippet="hello world", score=0.812, tier="L1", collection="shared")
    out = SearchOutput(query="q", intent="semantic", results=[hit])
    text = format_text(out)
    assert "1. [L1] Notes" in text
    assert "docs/notes.md" in text
    assert "hello world" in text
    assert "score=0.8120" in text
    assert "collection=shared" in text


def test_format_text_truncates_long_snippet_with_ellipsis() -> None:
    hit = _hit(path="/p", snippet="x" * 250)
    out = SearchOutput(query="q", intent="semantic", results=[hit])
    text = format_text(out)
    assert "x" * 200 + "…" in text


def test_format_text_falls_back_to_basename_when_title_empty() -> None:
    hit = _hit(path="docs/foo.md", title="", tier="L0")
    out = SearchOutput(query="q", intent="semantic", results=[hit])
    text = format_text(out)
    assert "1. [L0] foo.md" in text


def test_format_text_says_no_results_when_empty() -> None:
    out = SearchOutput(query="q", intent="semantic", results=[])
    assert "No results found." in format_text(out)


def test_format_text_short_circuits_on_error() -> None:
    out = SearchOutput(query="q", intent="", error="ValueError: boom")
    text = format_text(out)
    assert "Error: ValueError: boom" in text
    # Diagnostics line not emitted on the error path
    assert "BM25=" not in text


# ---------------------------------------------------------------------------
# to_json_envelope
# ---------------------------------------------------------------------------


def test_to_json_envelope_serialises_all_fields() -> None:
    hit = _hit(path="p", title="t", snippet="s", score=0.5, tier="L1", collection="c")
    out = SearchOutput(
        query="q",
        intent="semantic",
        results=[hit],
        bm25_count=1,
        vec_count=2,
        fused_count=3,
        vec_failed=False,
        total_tokens=10,
        latency_ms=15.4567,
    )
    env = to_json_envelope(out)
    assert env["query"] == "q"
    assert env["intent"] == "semantic"
    assert env["bm25_count"] == 1
    assert env["vec_count"] == 2
    assert env["fused_count"] == 3
    assert env["vec_failed"] is False
    assert env["total_tokens"] == 10
    assert env["latency_ms"] == pytest.approx(15.5)
    assert "error" not in env  # only present on the error path
    assert env["results"] == [
        {"path": "p", "title": "t", "collection": "c", "score": 0.5, "tier": "L1", "snippet": "s"}
    ]
    # Round-trip via json to confirm it's serialisable.
    assert json.loads(json.dumps(env)) == env


def test_to_json_envelope_includes_error_field_when_set() -> None:
    out = SearchOutput(query="q", intent="", error="ConnectionError: KAIRIX_NEO4J_URI not reachable")
    env = to_json_envelope(out)
    assert env["error"].startswith("ConnectionError")


# ---------------------------------------------------------------------------
# main() — exercised through the public argv surface
#
# ``main`` is the thin CLI adapter that parses argv, calls
# ``run_search`` for real (the use case is independently tested in
# ``tests/use_cases/test_search.py``), prints, and exits. The CLI is
# designed to degrade gracefully when production deps (Neo4j, Azure,
# usearch) are unavailable — search returns empty results and stdout
# still rendered, no exception. These tests pin that contract.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_text_mode_prints_query_and_intent_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI prints ``Query:`` / ``Intent:`` lines on every call,
    including failure modes.

    In the test env there's no ``provider:`` in ``kairix.config.yaml``,
    so the factory raises ``ValueError`` at pipeline-build time. The
    ``run_search`` use-case catches that, returns a populated
    ``SearchOutput.error``, and the CLI prints its text envelope then
    exits 1. We capture the SystemExit and assert on the printed lines.

    Sabotage: deleting ``print(format_text(out))`` in main() causes
    capsys.readouterr().out to be empty and the "Query:" assertion to fail.
    """
    main_module = __import__("kairix.core.search.cli", fromlist=["main"])
    with pytest.raises(SystemExit) as exc_info:
        main_module.main(["my unit test query"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Query: my unit test query" in captured.out
    assert "Intent:" in captured.out


@pytest.mark.unit
def test_main_json_mode_emits_parseable_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` mode prints a JSON envelope on every call, including
    failure modes. See ``test_main_text_mode_prints_query_and_intent_lines``
    for the why-it-exits-1 rationale.

    Sabotage: swapping to_json_envelope for format_text in the --json branch
    makes captured.out non-JSON and json.loads raises ValueError → test fails.
    """
    main_module = __import__("kairix.core.search.cli", fromlist=["main"])
    with pytest.raises(SystemExit) as exc_info:
        main_module.main(["another query", "--json"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["query"] == "another query"
    assert "results" in payload
    assert "bm25_count" in payload


@pytest.mark.unit
def test_main_exits_nonzero_when_search_output_has_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Sabotage: removing the `if out.error: sys.exit(1)` branch makes SystemExit
    # not fire and the pytest.raises block fails (no exception caught).
    #
    # We trigger an error by passing scope=all-agents which raises
    # NotImplementedError inside run_search (DefaultCollectionResolver).
    main_module = __import__("kairix.core.search.cli", fromlist=["main"])
    with pytest.raises(SystemExit) as exc_info:
        main_module.main(["query", "--scope", "all-agents", "--agent", "shape"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Error:" in captured.out


@pytest.mark.unit
def test_main_module_guard_invokes_main(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ``if __name__ == "__main__"`` guard wires ``main()`` so
    ``python -m kairix.core.search.cli`` works.

    In the test env there's no ``provider:`` in ``kairix.config.yaml``,
    so the CLI exits 1 via SystemExit after printing the error
    envelope. We capture the SystemExit and assert the "Query:" line
    still made it to stdout.

    Sabotage: removing ``main()`` under ``if __name__ == "__main__"``
    makes runpy.run_module return without printing the "Query:" line,
    so the captured.out assert fails.

    runpy executes the module as __main__ which triggers the guard line.
    We patch sys.argv (NOT a KAIRIX_ env var, so F2-compliant) to feed argv.
    """
    import runpy
    import sys as _sys

    saved_argv = _sys.argv
    _sys.argv = ["kairix-search", "guarded module run"]
    try:
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("kairix.core.search.cli", run_name="__main__")
        assert exc_info.value.code == 1
    finally:
        _sys.argv = saved_argv
    captured = capsys.readouterr()
    assert "Query: guarded module run" in captured.out
