"""Step definitions for entity_audit.feature (#260, #261).

Drives ``kairix entity audit`` and ``kairix entity purge`` end-to-end
through the production CLI ``main()``, injecting a recording fake for
the Neo4j client so no real graph is touched. The fake's call recorder
is what the "no Cypher calls" assertion reads.
"""

from __future__ import annotations

import io
import json
import shlex
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when


class _AuditRecordingNeo4j:
    """Recording fake — captures every Cypher call so steps can assert no writes."""

    available: bool = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return []


@dataclass
class _AuditCtx:
    tmp_dir: Path
    neo4j: _AuditRecordingNeo4j = field(default_factory=_AuditRecordingNeo4j)
    audit_report_path: Path | None = None
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def audit_ctx(tmp_path: Path) -> _AuditCtx:
    return _AuditCtx(tmp_dir=tmp_path)


def _run_entity_cli(ctx: _AuditCtx, args: list[str]) -> None:
    from kairix.knowledge.entities.cli import main as entity_main

    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = entity_main(args, neo4j_client=ctx.neo4j)
        ctx.exit_code = rc if rc is not None else 0
    except SystemExit as e:  # NOSONAR — BDD test captures CLI exit code; reraising defeats the test
        ctx.exit_code = int(e.code) if e.code is not None else 0
    ctx.stdout = out.getvalue()
    ctx.stderr = err.getvalue()


@given("an entity graph with no audit findings")
def _empty_graph(audit_ctx: _AuditCtx) -> None:
    # The recording fake returns [] from .cypher() — modelling a clean graph.
    audit_ctx.neo4j = _AuditRecordingNeo4j()


@given("an audit report file with one entity row")
def _audit_report_with_one_row(audit_ctx: _AuditCtx) -> None:
    payload = {
        "mode": "all",
        "generated_at": "2026-05-14T00:00:00Z",
        "total": 1,
        "rows": [
            {
                "id": "ghost-1",
                "name": "Ghost One",
                "type": "Concept",
                "mode": "junk",
                "reason": "no vault_path and no summary",
            }
        ],
    }
    path = audit_ctx.tmp_dir / "audit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    audit_ctx.audit_report_path = path


@when(parsers.parse("the operator runs `kairix entity audit {extra}`"))
def _run_audit(audit_ctx: _AuditCtx, extra: str) -> None:
    _run_entity_cli(audit_ctx, ["audit", *shlex.split(extra)])


@when(parsers.parse("the operator runs purge with `{flag}` against the audit report"))
def _run_purge(audit_ctx: _AuditCtx, flag: str) -> None:
    assert audit_ctx.audit_report_path is not None, "audit report fixture must be set"
    _run_entity_cli(
        audit_ctx,
        ["purge", "--audit-report", str(audit_ctx.audit_report_path), *shlex.split(flag)],
    )


@then(parsers.parse("the audit CLI exits with status {code:d}"))
def _assert_audit_exit(audit_ctx: _AuditCtx, code: int) -> None:
    assert audit_ctx.exit_code == code, (
        f"expected exit {code}, got {audit_ctx.exit_code}; "
        f"stdout={audit_ctx.stdout[:300]!r} stderr={audit_ctx.stderr[:300]!r}"
    )


@then("the audit JSON output names mode, generated_at, total, and rows")
def _assert_audit_json_shape(audit_ctx: _AuditCtx) -> None:
    payload = json.loads(audit_ctx.stdout)
    for key in ("mode", "generated_at", "total", "rows"):
        assert key in payload, f"audit JSON missing key {key!r}: {payload!r}"


@then("the purge output names DRY-RUN and the row id")
def _assert_purge_dry_run_output(audit_ctx: _AuditCtx) -> None:
    out = audit_ctx.stdout
    assert "DRY-RUN" in out, f"expected DRY-RUN in output: {out!r}"
    assert "ghost-1" in out, f"expected row id in output: {out!r}"


@then("the graph receives no Cypher calls")
def _assert_no_cypher(audit_ctx: _AuditCtx) -> None:
    assert audit_ctx.neo4j.calls == [], f"expected zero Cypher calls during dry-run, got {len(audit_ctx.neo4j.calls)}"
