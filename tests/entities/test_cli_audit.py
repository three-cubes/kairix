"""CLI smoke tests for ``kairix entity audit`` and ``kairix entity purge`` (#260, #261).

These tests exercise the argparse wiring + the cmd_audit/cmd_purge adapters
without touching real Neo4j. The use-case logic is covered by
``tests/use_cases/test_entity_audit.py`` and ``test_entity_purge.py``.

Sabotage-proof anchors:

- ``test_audit_subcommand_is_registered`` — drop the ``audit`` sub-parser
  block and argparse raises a SystemExit.
- ``test_audit_writes_output_file_when_requested`` — remove the
  ``args.output`` branch in ``cmd_audit`` and the file is never written.
- ``test_purge_requires_dry_run_or_execute`` — remove the
  ``required=True`` on the mutually-exclusive group and argparse no
  longer rejects.
- ``test_purge_dry_run_does_not_invoke_neo4j`` — wire the
  default-loader path and assert the recording client saw no calls.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from kairix.knowledge.entities.cli import (
    build_parser,
    cmd_audit,
    cmd_purge,
    main,
)
from kairix.use_cases.entity_audit import EntityAuditDeps
from kairix.use_cases.entity_purge import EntityPurgeDeps

pytestmark = pytest.mark.unit


class _ScriptedNeo4j:
    def __init__(self, *, rows: list[dict[str, Any]] | None = None, available: bool = True) -> None:
        self._rows = list(rows or [])
        self.available = available
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return list(self._rows)


def test_audit_subcommand_is_registered() -> None:
    """Sabotage anchor: drop the ``audit`` block and parse_args raises SystemExit."""
    parser = build_parser()
    args = parser.parse_args(["audit"])
    assert args.command == "audit"
    assert args.mode == "all"
    assert args.format == "text"


def test_audit_accepts_mode_and_format_and_output() -> None:
    parser = build_parser()
    args = parser.parse_args(["audit", "--mode", "junk", "--format", "json", "--output", "/tmp/x.json"])
    assert args.mode == "junk"
    assert args.format == "json"
    assert args.output == "/tmp/x.json"


def test_audit_rejects_unknown_mode() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["audit", "--mode", "bogus"])


def test_audit_writes_output_file_when_requested(tmp_path: Path) -> None:
    """Sabotage anchor: remove the args.output branch in cmd_audit — no file."""
    parser = build_parser()
    args = parser.parse_args(["audit", "--mode", "all", "--format", "json", "--output", str(tmp_path / "audit.json")])
    deps = EntityAuditDeps(neo4j_client=_ScriptedNeo4j(), now_fn=lambda: "T")

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cmd_audit(args, deps=deps)
    assert rc == 0

    audit_file = tmp_path / "audit.json"
    assert audit_file.exists()
    payload = json.loads(audit_file.read_text(encoding="utf-8"))
    assert payload["mode"] == "all"
    assert payload["rows"] == []


def test_audit_text_format_to_stdout() -> None:
    parser = build_parser()
    args = parser.parse_args(["audit", "--mode", "all"])
    deps = EntityAuditDeps(neo4j_client=_ScriptedNeo4j(), now_fn=lambda: "T")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_audit(args, deps=deps)
    assert rc == 0
    assert "No audit rows found" in out.getvalue()


def test_audit_output_file_write_failure_returns_1(tmp_path: Path) -> None:
    parser = build_parser()
    bad_path = tmp_path / "no-such-dir" / "audit.json"
    args = parser.parse_args(["audit", "--format", "json", "--output", str(bad_path)])
    deps = EntityAuditDeps(neo4j_client=_ScriptedNeo4j(), now_fn=lambda: "T")
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        rc = cmd_audit(args, deps=deps)
    assert rc == 1
    assert "ERROR" in err.getvalue()


def test_purge_requires_dry_run_or_execute() -> None:
    """Sabotage anchor: drop required=True on the mutex group — argparse stops rejecting."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["purge", "--audit-report", "/tmp/x.json"])


def test_purge_rejects_both_dry_run_and_execute() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["purge", "--audit-report", "/tmp/x.json", "--dry-run", "--execute"])


def test_purge_accepts_dry_run_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(["purge", "--audit-report", "/tmp/x.json", "--dry-run"])
    assert args.dry_run is True
    assert args.execute is False
    assert args.audit_report == "/tmp/x.json"


def test_purge_accepts_execute_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(["purge", "--audit-report", "/tmp/x.json", "--execute"])
    assert args.execute is True
    assert args.dry_run is False


def test_purge_dry_run_does_not_invoke_neo4j(tmp_path: Path) -> None:
    """End-to-end through cmd_purge: dry-run reads the report, runs no Cypher."""
    report = tmp_path / "audit.json"
    payload = {
        "mode": "all",
        "generated_at": "T",
        "total": 1,
        "rows": [{"id": "a", "name": "A", "type": "Person", "mode": "junk", "reason": "r"}],
    }
    report.write_text(json.dumps(payload), encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(["purge", "--audit-report", str(report), "--dry-run"])
    neo4j = _ScriptedNeo4j()
    deps = EntityPurgeDeps(neo4j_client=neo4j)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_purge(args, deps=deps)
    assert rc == 0
    assert neo4j.calls == []
    assert "DRY-RUN" in out.getvalue()
    assert "a" in out.getvalue()


def test_purge_missing_report_returns_1() -> None:
    parser = build_parser()
    args = parser.parse_args(["purge", "--audit-report", "/tmp/definitely-not-here.json", "--dry-run"])
    deps = EntityPurgeDeps(neo4j_client=_ScriptedNeo4j())
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_purge(args, deps=deps)
    assert rc == 1
    assert "FileNotFoundError" in out.getvalue()


def test_purge_json_format(tmp_path: Path) -> None:
    report = tmp_path / "audit.json"
    report.write_text(json.dumps({"mode": "all", "rows": []}), encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(["purge", "--audit-report", str(report), "--dry-run", "--format", "json"])
    deps = EntityPurgeDeps(neo4j_client=_ScriptedNeo4j())
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_purge(args, deps=deps)
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["dry_run"] is True
    assert payload["candidate_count"] == 0


def test_main_dispatches_audit() -> None:
    """main() routes ``audit`` to cmd_audit and returns its exit code."""
    out = io.StringIO()
    with redirect_stdout(out):
        # No neo4j_client is wired; cmd_audit falls back to the resolver
        # which returns a real client when the lib is importable. Inject
        # a stub via the neo4j_client kwarg so we don't hit production.
        rc = main(["audit", "--mode", "all"], neo4j_client=_ScriptedNeo4j())
    assert rc == 0
    assert "No audit rows found" in out.getvalue()


def test_main_dispatches_purge(tmp_path: Path) -> None:
    report = tmp_path / "audit.json"
    report.write_text(json.dumps({"mode": "all", "rows": []}), encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(
            ["purge", "--audit-report", str(report), "--dry-run"],
            neo4j_client=_ScriptedNeo4j(),
        )
    assert rc == 0
    assert "DRY-RUN" in out.getvalue()
