"""Step definitions for search_cli.feature.

Drives the search CLI through ``run_search`` + ``SearchDeps`` injection,
then renders via the CLI's pure formatters (``format_text`` and
``to_json_envelope``). Captures stdout + exit code so the assertions
pin operator-visible behaviour without touching a real search pipeline.

Phase 2 of #168 unified the CLI and MCP under ``run_search``; the BDD
scenarios at the operator surface stay the same — only the test glue
shifted from a fake pipeline to a fake ``search_fn``.
"""

from __future__ import annotations

import io
import json
import shlex
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.search.cli import build_parser, format_text, to_json_envelope
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchResult
from kairix.core.search.scope import Scope
from kairix.use_cases.search import SearchDeps, run_search


@dataclass
class _CliCtx:
    search_fn: Any | None = None
    classified_intent: QueryIntent = QueryIntent.SEMANTIC
    exit_code: int = 0
    stdout: str = ""
    json_output: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def cli_ctx() -> _CliCtx:
    return _CliCtx()


def _make_search_fn(
    *,
    result_paths: list[str],
    intent: QueryIntent,
    error: str = "",
) -> Any:
    """Build a search_fn that returns a SearchResult mirroring production shape."""

    def fn(**kwargs: Any) -> SearchResult:
        budgeted: list[Any] = []
        for path in result_paths:
            budgeted.append(
                SimpleNamespace(
                    result=SimpleNamespace(
                        path=path,
                        title=path.rsplit("/", 1)[-1],
                        collection="vault",
                        boosted_score=1.0,
                        snippet=f"snippet for {path}",
                    ),
                    tier="L2",
                    content=f"snippet for {path}",
                    token_estimate=10,
                )
            )
        return SearchResult(
            query=str(kwargs.get("query", "")),
            intent=intent,
            results=budgeted,
            bm25_count=len(result_paths),
            vec_count=0,
            fused_count=len(result_paths),
            error=error,
        )

    return fn


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('a fake pipeline that returns one result for "{query}"'))
def _given_one_result(cli_ctx: _CliCtx, query: str) -> None:
    cli_ctx.search_fn = _make_search_fn(
        result_paths=[f"vault/{query}.md"],
        intent=cli_ctx.classified_intent,
    )


@given(parsers.parse('a fake pipeline that returns {n:d} results for "{query}"'))
def _given_n_results(cli_ctx: _CliCtx, n: int, query: str) -> None:
    cli_ctx.search_fn = _make_search_fn(
        result_paths=[f"vault/{query}-{i}.md" for i in range(n)],
        intent=cli_ctx.classified_intent,
    )


@given(parsers.parse('a fake pipeline that returns an error for "{query}"'))
def _given_error(cli_ctx: _CliCtx, query: str) -> None:
    cli_ctx.search_fn = _make_search_fn(
        result_paths=[],
        intent=cli_ctx.classified_intent,
        error="Neo4j unavailable — set KAIRIX_NEO4J_URI or run with KAIRIX_NEO4J_OFFLINE=1",
    )


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse("the operator runs `kairix search {argv}`"))
def _run_search_cli(cli_ctx: _CliCtx, argv: str) -> None:
    """Invoke the CLI's argv → run_search → stdout pipeline with stubbed deps."""
    assert cli_ctx.search_fn is not None
    args = build_parser().parse_args(shlex.split(argv))

    # Force the use case to skip its own classify branch and use whatever the
    # scenario said the intent should be.
    deps = SearchDeps(
        search_fn=cli_ctx.search_fn,
        classify_fn=lambda q: cli_ctx.classified_intent,
        entity_card_fn=lambda name: None,
    )

    out = run_search(
        args.query,
        agent=args.agent,
        scope=Scope.parse(args.scope),
        budget=args.budget,
        limit=args.limit,
        include_entity_card=args.include_entity_card,
        deps=deps,
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        if args.as_json:
            print(json.dumps(to_json_envelope(out), indent=2))
        else:
            print(format_text(out))

    cli_ctx.exit_code = 1 if out.error else 0
    cli_ctx.stdout = buf.getvalue()
    if args.as_json:
        try:
            cli_ctx.json_output = json.loads(cli_ctx.stdout)
        except json.JSONDecodeError:
            cli_ctx.json_output = {}


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse("the CLI exits with status {code:d}"))
def _assert_exit_code(cli_ctx: _CliCtx, code: int) -> None:
    assert cli_ctx.exit_code == code, f"expected exit {code}, got {cli_ctx.exit_code}; stdout={cli_ctx.stdout[:300]!r}"


@then("the human-readable output names the classified intent")
def _assert_human_intent(cli_ctx: _CliCtx) -> None:
    assert "Intent:" in cli_ctx.stdout, f"missing 'Intent:' line; got {cli_ctx.stdout!r}"
    assert cli_ctx.classified_intent.value in cli_ctx.stdout


@then("the human-readable output includes the result path")
def _assert_human_path(cli_ctx: _CliCtx) -> None:
    assert "vault/" in cli_ctx.stdout, f"expected a result path in output; got {cli_ctx.stdout!r}"


@then("stdout is parseable JSON")
def _assert_json_parses(cli_ctx: _CliCtx) -> None:
    assert cli_ctx.json_output, f"stdout was not valid JSON; got {cli_ctx.stdout[:300]!r}"


@then(parsers.parse('the JSON has a "results" array of length {n:d}'))
def _assert_results_len(cli_ctx: _CliCtx, n: int) -> None:
    results = cli_ctx.json_output.get("results")
    assert isinstance(results, list), f"expected list at 'results', got {type(results).__name__}"
    assert len(results) == n, f"expected {n} results, got {len(results)}"


@then(parsers.parse('the JSON\'s "results" array has length {n:d}'))
def _assert_results_len_alt(cli_ctx: _CliCtx, n: int) -> None:
    _assert_results_len(cli_ctx, n)


@then("the JSON's top-level \"intent\" equals the pipeline's classified intent")
def _assert_json_intent(cli_ctx: _CliCtx) -> None:
    assert cli_ctx.json_output.get("intent") == cli_ctx.classified_intent.value


@then('the JSON has an "error" field with operator-actionable text')
def _assert_error_field(cli_ctx: _CliCtx) -> None:
    err = cli_ctx.json_output.get("error", "")
    assert err, f"expected non-empty 'error' field; got {cli_ctx.json_output!r}"
    assert any(token in err for token in ("KAIRIX_", "config", "Neo4j", "Azure")), (
        f"error message not operator-actionable: {err!r}"
    )
