"""Step definitions for store_cli.feature.

Drives ``kairix.knowledge.store.cli.main`` with an explicit ``FakeNeo4jClient``
(``available=False``, no entities) so tests do not depend on the host's
``KAIRIX_NEO4J_*`` environment. The document-root flows through the
``--document-root`` CLI flag — TMP in the scenario argv resolves to the
fixture's tmp_path-rooted vault.
"""

from __future__ import annotations

import io
import json
import shlex
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from tests.fixtures.neo4j_mock import FakeNeo4jClient


class _UnavailableNeo4jClient(FakeNeo4jClient):
    """FakeNeo4jClient with available=False — exercises the no-Neo4j fallback."""

    available: bool = False


@dataclass
class _StoreCliCtx:
    document_root: Path
    neo4j_client: Any
    exit_code: int = 0
    stdout: str = ""
    json_output: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def store_cli_ctx(tmp_path: Path) -> _StoreCliCtx:
    docroot = tmp_path / "vault"
    docroot.mkdir()
    return _StoreCliCtx(document_root=docroot, neo4j_client=_UnavailableNeo4jClient(entities=[]))


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("a document store with one entity-shaped document")
def _given_one_doc(store_cli_ctx: _StoreCliCtx) -> None:
    # Crawler contract (kairix.knowledge.store.crawler): person nodes are
    # discovered from .md files inside any directory named "People-Notes"
    # under the document root. Frontmatter is optional — name falls back
    # to the filename stem.
    doc = store_cli_ctx.document_root / "Network" / "People-Notes" / "Jordan-Blake.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\nname: Jordan Blake\nrole: Engineer\n---\n\nEngineer at Three Cubes.\n")


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


def _invoke_store(store_cli_ctx: _StoreCliCtx, args: list[str]) -> None:
    from kairix.knowledge.store.cli import main as store_main

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            store_main(args, neo4j_client=store_cli_ctx.neo4j_client)
        store_cli_ctx.exit_code = 0
    except SystemExit as e:  # NOSONAR — BDD test captures CLI exit code; reraising would defeat the test
        store_cli_ctx.exit_code = int(e.code) if e.code is not None else 0
    store_cli_ctx.stdout = buf.getvalue()
    if "--json" in args:
        try:
            store_cli_ctx.json_output = json.loads(store_cli_ctx.stdout)
        except json.JSONDecodeError:
            store_cli_ctx.json_output = {}


@when(parsers.parse("the operator runs the store CLI with `{argv}`"))
def _run_store_cli(store_cli_ctx: _StoreCliCtx, argv: str) -> None:
    # Substitute TMP placeholder for tmp_path-derived doc root.
    if store_cli_ctx.document_root is not None:
        argv = argv.replace("TMP", str(store_cli_ctx.document_root))
    args = shlex.split(argv) if argv else []
    _invoke_store(store_cli_ctx, args)


@when("the operator runs the store CLI without any subcommand")
def _run_store_cli_no_subcommand(store_cli_ctx: _StoreCliCtx) -> None:
    _invoke_store(store_cli_ctx, [])


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse("the store CLI exits with status {code:d}"))
def _assert_store_exit(store_cli_ctx: _StoreCliCtx, code: int) -> None:
    assert store_cli_ctx.exit_code == code, (
        f"expected exit {code}, got {store_cli_ctx.exit_code}; stdout={store_cli_ctx.stdout[:300]!r}"
    )


@then("the output is in dry-run mode")
def _assert_dry_run(store_cli_ctx: _StoreCliCtx) -> None:
    assert "[DRY RUN]" in store_cli_ctx.stdout, f"expected dry-run banner; got {store_cli_ctx.stdout!r}"


def _parse_count_after_label(stdout: str, label: str) -> int:
    """Find a 'Label:  N found' line and return N. Asserts the line exists."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(label):
            tail = stripped[len(label) :].strip()
            # First whitespace-separated token after the label is the integer count.
            return int(tail.split()[0])
    raise AssertionError(f"missing line starting with {label!r} in output:\n{stdout}")


@then(parsers.parse("the crawl reports {n:d} person found"))
@then(parsers.parse("the crawl reports {n:d} persons found"))
def _assert_persons_count(store_cli_ctx: _StoreCliCtx, n: int) -> None:
    actual = _parse_count_after_label(store_cli_ctx.stdout, "Persons:")
    assert actual == n, f"expected {n} persons, got {actual}; stdout={store_cli_ctx.stdout!r}"


@then(parsers.parse("the crawl reports {n:d} organisation found"))
@then(parsers.parse("the crawl reports {n:d} organisations found"))
def _assert_orgs_count(store_cli_ctx: _StoreCliCtx, n: int) -> None:
    actual = _parse_count_after_label(store_cli_ctx.stdout, "Organisations:")
    assert actual == n, f"expected {n} organisations, got {actual}; stdout={store_cli_ctx.stdout!r}"


@then(parsers.parse('the store JSON "{field_name}" field equals false'))
def _assert_store_json_false(store_cli_ctx: _StoreCliCtx, field_name: str) -> None:
    assert field_name in store_cli_ctx.json_output, f"missing {field_name!r}: {store_cli_ctx.json_output}"
    value = store_cli_ctx.json_output[field_name]
    assert value is False, f"expected {field_name}=false; got {value!r} (type {type(value).__name__})"


@then(parsers.parse('the store JSON "{field_name}" field equals {value:d}'))
def _assert_store_json_int(store_cli_ctx: _StoreCliCtx, field_name: str, value: int) -> None:
    assert field_name in store_cli_ctx.json_output, f"missing {field_name!r}: {store_cli_ctx.json_output}"
    actual = store_cli_ctx.json_output[field_name]
    assert actual == value, f"expected {field_name}={value}; got {actual!r}"
    assert isinstance(actual, int) and not isinstance(actual, bool), (
        f"{field_name} must be a plain int, got {type(actual).__name__}"
    )


@then(parsers.parse('the store JSON "{field_name}" field contains "{needle}"'))
def _assert_store_json_list_contains(store_cli_ctx: _StoreCliCtx, field_name: str, needle: str) -> None:
    assert field_name in store_cli_ctx.json_output, f"missing {field_name!r}: {store_cli_ctx.json_output}"
    items = store_cli_ctx.json_output[field_name]
    assert isinstance(items, list), f"{field_name} must be a list, got {type(items).__name__}"
    assert any(needle in str(item) for item in items), f"no item in {field_name} contained {needle!r}; got {items!r}"


@then("the store CLI stdout is parseable JSON")
def _assert_store_json_parseable(store_cli_ctx: _StoreCliCtx) -> None:
    assert store_cli_ctx.json_output, f"stdout was not parseable JSON; got {store_cli_ctx.stdout!r}"


@then(parsers.re(r'the JSON has an? "(?P<field_name>[^"]+)" field'))
def _assert_json_has_field(store_cli_ctx: _StoreCliCtx, field_name: str) -> None:
    assert field_name in store_cli_ctx.json_output, (
        f"missing {field_name!r} in JSON output: {store_cli_ctx.json_output}"
    )


@then("the output names every store subcommand")
def _assert_help_lists_subcommands(store_cli_ctx: _StoreCliCtx) -> None:
    out = store_cli_ctx.stdout
    for sub in ("crawl", "health"):
        assert sub in out, f"missing subcommand {sub!r} in help: {out!r}"
