"""Step definitions for curator_cli.feature.

Drives ``kairix.agents.curator.cli.main`` with an explicit ``FakeNeo4jClient``
(``available=False``) so tests don't depend on host Neo4j env vars. Steps
are namespaced ('the curator CLI') to avoid colliding with the unit-level
curator health BDD in curator_steps.py.
"""

from __future__ import annotations

import io
import json
import shlex
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from typing import Any

import pytest
from pytest_bdd import parsers, then, when

from tests.fixtures.neo4j_mock import FakeNeo4jClient


class _UnavailableNeo4jClient(FakeNeo4jClient):
    """FakeNeo4jClient with available=False — exercises the no-Neo4j fallback."""

    available: bool = False


@dataclass
class _CuratorCliCtx:
    neo4j_client: Any
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    json_output: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def curator_cli_ctx() -> _CuratorCliCtx:
    return _CuratorCliCtx(neo4j_client=_UnavailableNeo4jClient(entities=[]))


def _run_curator(curator_cli_ctx: _CuratorCliCtx, args: list[str]) -> None:
    from kairix.agents.curator.cli import main as curator_main

    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            curator_main(args, neo4j_client=curator_cli_ctx.neo4j_client)
        curator_cli_ctx.exit_code = 0
    except SystemExit as e:  # NOSONAR — BDD test captures CLI exit code; reraising would defeat the test
        curator_cli_ctx.exit_code = int(e.code) if e.code is not None else 0
    curator_cli_ctx.stdout = out.getvalue()
    curator_cli_ctx.stderr = err.getvalue()
    if "json" in args:
        try:
            curator_cli_ctx.json_output = json.loads(curator_cli_ctx.stdout)
        except json.JSONDecodeError:
            curator_cli_ctx.json_output = {}


@when(parsers.parse("the operator runs the curator CLI with `{argv}`"))
def _run_curator_argv(curator_cli_ctx: _CuratorCliCtx, argv: str) -> None:
    _run_curator(curator_cli_ctx, shlex.split(argv))


@when("the operator runs the curator CLI with no arguments")
def _run_curator_no_args(curator_cli_ctx: _CuratorCliCtx) -> None:
    _run_curator(curator_cli_ctx, [])


@then(parsers.parse("the curator CLI exits with status {code:d}"))
def _assert_curator_exit(curator_cli_ctx: _CuratorCliCtx, code: int) -> None:
    assert curator_cli_ctx.exit_code == code, (
        f"expected exit {code}, got {curator_cli_ctx.exit_code}; "
        f"stdout={curator_cli_ctx.stdout[:200]!r} stderr={curator_cli_ctx.stderr[:200]!r}"
    )


@then("the curator help output names the health subcommand")
def _assert_help_lists_health(curator_cli_ctx: _CuratorCliCtx) -> None:
    out = curator_cli_ctx.stdout + curator_cli_ctx.stderr
    assert "health" in out, f"missing 'health' in output:\n{out}"


@then("the curator CLI stdout is parseable JSON")
def _assert_curator_json_parseable(curator_cli_ctx: _CuratorCliCtx) -> None:
    assert curator_cli_ctx.json_output, f"stdout was not parseable JSON; got {curator_cli_ctx.stdout[:300]!r}"


def _curator_json_value(curator_cli_ctx: _CuratorCliCtx, field_name: str) -> Any:
    assert field_name in curator_cli_ctx.json_output, (
        f"missing {field_name!r} in JSON output: {curator_cli_ctx.json_output}"
    )
    return curator_cli_ctx.json_output[field_name]


@then(parsers.parse('the curator JSON "{field_name}" field equals true'))
def _assert_curator_json_true(curator_cli_ctx: _CuratorCliCtx, field_name: str) -> None:
    value = _curator_json_value(curator_cli_ctx, field_name)
    assert value is True, f"expected {field_name}=true; got {value!r} (type {type(value).__name__})"


@then(parsers.parse('the curator JSON "{field_name}" field equals false'))
def _assert_curator_json_false(curator_cli_ctx: _CuratorCliCtx, field_name: str) -> None:
    value = _curator_json_value(curator_cli_ctx, field_name)
    assert value is False, f"expected {field_name}=false; got {value!r} (type {type(value).__name__})"


@then(parsers.parse('the curator JSON "{field_name}" field equals {value:d}'))
def _assert_curator_json_int(curator_cli_ctx: _CuratorCliCtx, field_name: str, value: int) -> None:
    actual = _curator_json_value(curator_cli_ctx, field_name)
    assert actual == value, f"expected {field_name}={value}; got {actual!r}"
    assert isinstance(actual, int) and not isinstance(actual, bool), (
        f"{field_name} must be a plain int, got {type(actual).__name__}"
    )
