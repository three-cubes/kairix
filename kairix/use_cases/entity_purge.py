"""Entity purge use case — DETACH DELETE the rows an audit report flags (#261).

Reads a JSON audit report (the shape ``entity_audit.format_report_json``
emits) and either previews or executes the deletes. The dry-run path
runs no Cypher; the execute path issues one ``DETACH DELETE`` per row.

Pure: dry-run touches no graph state; execute is the only side-effect
path and emits a per-row audit log via ``PurgeResult.audit_log``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kairix.use_cases.entity_audit import EntityAuditRow

logger = logging.getLogger(__name__)


_DELETE_CYPHER = "MATCH (n {id: $id}) DETACH DELETE n"


@dataclass(frozen=True)
class PurgeAuditEntry:
    """One audit-log entry written for every attempted delete.

    ``status`` is ``"deleted"`` on success, ``"skipped"`` when the row
    had no usable id, and ``"error: <Class>: <msg>"`` when the Cypher
    raised.
    """

    id: str
    name: str
    type: str
    mode: str
    reason: str
    status: str


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of a single ``run_entity_purge`` invocation.

    Attributes:
        dry_run: ``True`` when no graph writes were attempted.
        cypher: The Cypher statement (template) the executor would run
            for every row. Surfaced so operators can inspect or re-run
            manually.
        candidate_count: Number of rows read from the audit report.
        deleted_count: Number of successful ``DETACH DELETE`` calls.
            ``0`` when ``dry_run`` is ``True``.
        rows: The audit rows that were considered.
        audit_log: Per-row outcome list (empty for dry-run).
        error: Empty on success; ``"<Class>: <msg>"`` on top-level
            failure (audit-report file missing, malformed JSON, ...).
    """

    dry_run: bool
    cypher: str = _DELETE_CYPHER
    candidate_count: int = 0
    deleted_count: int = 0
    rows: list[EntityAuditRow] = field(default_factory=list)
    audit_log: list[PurgeAuditEntry] = field(default_factory=list)
    error: str = ""


def _default_audit_loader(report_path: str) -> list[EntityAuditRow]:
    """Production audit-report loader. Reads the JSON shape ``entity_audit`` emits.

    Raises ``FileNotFoundError`` if the report is missing, ``ValueError``
    for malformed JSON, ``KeyError`` for a missing ``rows`` field.
    """
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    rows_raw = data.get("rows")
    if rows_raw is None:
        raise KeyError("audit report missing 'rows' field")
    return [
        EntityAuditRow(
            id=str(r.get("id") or ""),
            name=str(r.get("name") or ""),
            type=str(r.get("type") or ""),
            mode=str(r.get("mode") or ""),
            reason=str(r.get("reason") or ""),
        )
        for r in rows_raw
    ]


@dataclass(frozen=True)
class EntityPurgeDeps:
    """Injectable dependencies for ``run_entity_purge``.

    Mirrors ``EntityAuditDeps`` — every dependency has a non-Optional
    ``default_factory`` so production callers leave ``deps=None``.

    Attributes:
        neo4j_client: Anything that exposes ``cypher(query, params) ->
            list[dict]`` and ``available``. Production injects the real
            ``Neo4jClient``.
        audit_loader: Callable mapping a report file path to a list of
            ``EntityAuditRow``. Default reads the JSON the audit use case
            emits; tests pass a list-returning lambda.
    """

    neo4j_client: Any = None
    audit_loader: Callable[[str], list[EntityAuditRow]] = field(default_factory=lambda: _default_audit_loader)


def _load_rows(deps: EntityPurgeDeps, audit_report_path: str) -> tuple[list[EntityAuditRow], str]:
    """Load rows from the audit report, returning (rows, error_string)."""
    try:
        rows = deps.audit_loader(audit_report_path)
    except (FileNotFoundError, ValueError, KeyError, OSError) as exc:
        logger.warning("entity_purge could not load audit report %s: %s", audit_report_path, exc)
        return [], f"{type(exc).__name__}: {exc}"
    return rows, ""


def _delete_one(neo4j_client: Any, row: EntityAuditRow) -> PurgeAuditEntry:
    """Issue one ``DETACH DELETE`` and return the audit entry."""
    if not row.id:
        return PurgeAuditEntry(
            id="",
            name=row.name,
            type=row.type,
            mode=row.mode,
            reason=row.reason,
            status="skipped: no id",
        )
    try:
        neo4j_client.cypher(_DELETE_CYPHER, {"id": row.id})
    except Exception as exc:
        return PurgeAuditEntry(
            id=row.id,
            name=row.name,
            type=row.type,
            mode=row.mode,
            reason=row.reason,
            status=f"error: {type(exc).__name__}: {exc}",
        )
    return PurgeAuditEntry(
        id=row.id,
        name=row.name,
        type=row.type,
        mode=row.mode,
        reason=row.reason,
        status="deleted",
    )


def run_entity_purge(
    audit_report_path: str,
    *,
    dry_run: bool,
    deps: EntityPurgeDeps | None = None,
) -> PurgeResult:
    """Purge entities listed in an audit report.

    Args:
        audit_report_path: Path to the JSON audit report that
            ``run_entity_audit`` produced.
        dry_run: When ``True`` (the safety default), no Cypher runs and
            ``deleted_count`` is ``0`` — the result still carries the
            row list and the Cypher template so operators can review.
        deps: Injectable dependencies; production callers leave ``None``.
    """
    d = deps or EntityPurgeDeps()
    rows, err = _load_rows(d, audit_report_path)
    if err:
        return PurgeResult(dry_run=dry_run, error=err)

    if dry_run:
        return PurgeResult(
            dry_run=True,
            candidate_count=len(rows),
            rows=list(rows),
        )

    audit_log: list[PurgeAuditEntry] = []
    deleted = 0
    if d.neo4j_client is None or not getattr(d.neo4j_client, "available", False):
        return PurgeResult(
            dry_run=False,
            candidate_count=len(rows),
            rows=list(rows),
            error="Neo4jUnavailable: graph client not available",
        )
    for row in rows:
        entry = _delete_one(d.neo4j_client, row)
        audit_log.append(entry)
        if entry.status == "deleted":
            deleted += 1
    return PurgeResult(
        dry_run=False,
        candidate_count=len(rows),
        deleted_count=deleted,
        rows=list(rows),
        audit_log=audit_log,
    )


def format_purge_text(result: PurgeResult) -> str:
    """Render a ``PurgeResult`` for the operator."""
    if result.error:
        return f"error: {result.error}\n"
    lines: list[str] = []
    mode_label = "DRY-RUN" if result.dry_run else "EXECUTE"
    lines.append(f"Entity purge — mode={mode_label}, candidates={result.candidate_count}")
    if result.dry_run:
        lines.append(f"Cypher (would run for each row): {result.cypher}")
    else:
        lines.append(f"Cypher: {result.cypher}")
        lines.append(f"Deleted: {result.deleted_count} / {result.candidate_count}")
    if not result.rows:
        lines.append("(no candidate rows in audit report)")
        return "\n".join(lines) + "\n"
    lines.append("")
    col_id = max((len(r.id) for r in result.rows), default=2)
    col_name = max((len(r.name) for r in result.rows), default=4)
    col_id = max(col_id, 2)
    col_name = max(col_name, 4)
    if result.dry_run:
        lines.append(f"{'ID':<{col_id}}  {'NAME':<{col_name}}  MODE/REASON")
        for r in result.rows:
            lines.append(f"{r.id:<{col_id}}  {r.name:<{col_name}}  {r.mode}: {r.reason}")
    else:
        lines.append(f"{'ID':<{col_id}}  {'NAME':<{col_name}}  STATUS")
        for entry in result.audit_log:
            lines.append(f"{entry.id:<{col_id}}  {entry.name:<{col_name}}  {entry.status}")
    return "\n".join(lines) + "\n"


def format_purge_json(result: PurgeResult) -> str:
    """Project a ``PurgeResult`` to JSON for downstream tooling."""
    payload: dict[str, Any] = {
        "dry_run": result.dry_run,
        "cypher": result.cypher,
        "candidate_count": result.candidate_count,
        "deleted_count": result.deleted_count,
        "rows": [dataclasses.asdict(r) for r in result.rows],
        "audit_log": [dataclasses.asdict(e) for e in result.audit_log],
        "error": result.error,
    }
    return json.dumps(payload, indent=2)
