"""Step definitions for search_cli.feature.

Drives ``kairix.core.search.cli.main`` via its ``pipeline=`` injection seam
with a minimal Protocol-shaped fake — no monkeypatching. Captures stdout
+ exit code so the assertions can pin operator-visible CLI behaviour.
"""

from __future__ import annotations

import io
import json
import shlex
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.search.cli import main as search_cli_main
from kairix.core.search.intent import QueryIntent
from kairix.core.search.pipeline import SearchResult


@dataclass
class _CliCtx:
    pipeline: Any | None = None
    classified_intent: QueryIntent = QueryIntent.SEMANTIC
    exit_code: int = 0
    stdout: str = ""
    json_output: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def cli_ctx() -> _CliCtx:
    return _CliCtx()


# ---------------------------------------------------------------------------
# Test pipeline: implements .search(query, ...) → SearchResult
# Protocol-compliant in shape; not a kairix-domain Protocol (the pipeline
# entry point is a single dispatch — not a Protocol that warrants a
# canonical fake). Inline by exception, per the 'external-library /
# CLI-glue inline-stub is OK' rule.
# ---------------------------------------------------------------------------


class _ScriptedPipeline:
    """Minimal pipeline shape that ``kairix.core.search.cli.main`` consumes."""

    def __init__(
        self,
        *,
        result_paths: list[str] | None = None,
        intent: QueryIntent = QueryIntent.SEMANTIC,
        error: str = "",
    ) -> None:
        self._result_paths = list(result_paths or [])
        self._intent = intent
        self._error = error

    def search(self, query: str, **_kwargs: Any) -> SearchResult:
        budgeted: list[Any] = []
        for path in self._result_paths:
            from types import SimpleNamespace

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
                )
            )
        return SearchResult(
            query=query,
            intent=self._intent,
            results=budgeted,
            bm25_count=len(self._result_paths),
            vec_count=0,
            fused_count=len(self._result_paths),
            error=self._error,
        )


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('a fake pipeline that returns one result for "{query}"'))
def _given_one_result(cli_ctx: _CliCtx, query: str) -> None:
    cli_ctx.pipeline = _ScriptedPipeline(
        result_paths=[f"vault/{query}.md"],
        intent=cli_ctx.classified_intent,
    )


@given(parsers.parse('a fake pipeline that returns {n:d} results for "{query}"'))
def _given_n_results(cli_ctx: _CliCtx, n: int, query: str) -> None:
    cli_ctx.pipeline = _ScriptedPipeline(
        result_paths=[f"vault/{query}-{i}.md" for i in range(n)],
        intent=cli_ctx.classified_intent,
    )


@given(parsers.parse('a fake pipeline that returns an error for "{query}"'))
def _given_error(cli_ctx: _CliCtx, query: str) -> None:
    cli_ctx.pipeline = _ScriptedPipeline(
        result_paths=[],
        intent=cli_ctx.classified_intent,
        error="Neo4j unavailable — set KAIRIX_NEO4J_URI or run with KAIRIX_NEO4J_OFFLINE=1",
    )


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse("the operator runs `kairix search {argv}`"))
def _run_search_cli(cli_ctx: _CliCtx, argv: str) -> None:
    """Invoke the CLI's main() with captured stdout + exit code."""
    assert cli_ctx.pipeline is not None
    args = shlex.split(argv)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            search_cli_main(args, pipeline=cli_ctx.pipeline)
        cli_ctx.exit_code = 0
    except SystemExit as e:
        cli_ctx.exit_code = int(e.code) if e.code is not None else 0
    cli_ctx.stdout = buf.getvalue()
    if "--json" in args:
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
    # Operator-actionable means it names a config knob or env var the operator can fix.
    assert any(token in err for token in ("KAIRIX_", "config", "Neo4j", "Azure")), (
        f"error message not operator-actionable: {err!r}"
    )
