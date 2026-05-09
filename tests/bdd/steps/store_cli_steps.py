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
    assert store_cli_ctx.document_root is not None
    # The crawler looks for entity-shaped Markdown docs. A minimal one has
    # frontmatter with a 'type' field; the crawler's exact contract is
    # documented in kairix.knowledge.store.crawler — for the BDD we only
    # need *some* doc to exist so the dry-run reports a non-zero count.
    doc = store_cli_ctx.document_root / "01-People" / "Jordan-Blake.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntype: person\nname: Jordan Blake\n---\n\nEngineer at Three Cubes.\n")


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
    except SystemExit as e:
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


@then("the output reports the entity counts found")
def _assert_counts(store_cli_ctx: _StoreCliCtx) -> None:
    out = store_cli_ctx.stdout
    # Production crawler reports four entity types; only check the labels are present.
    for label in ("Organisations:", "Persons:", "Outcomes:", "Edges:"):
        assert label in out, f"missing entity-count label {label!r} in output:\n{out}"


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
