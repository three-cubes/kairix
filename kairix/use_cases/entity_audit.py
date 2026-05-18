"""Entity audit use case — one-shot junk/path/enrichment scan (#260).

Replaces the six-command stitched workflow in the entity-audit runbook
with a single read-only pure function. The use case wires three audit
modes — ``junk``, ``paths``, ``enrichment`` — plus a ``all`` mode that
returns the deduplicated union.

The return shape (``AuditReport``/``EntityAuditRow``) is the contract that
``entity_purge`` (#261) consumes: ``run_entity_purge`` reads the JSON
emission and acts on the ``id`` of each row.

Pure: no graph writes, no filesystem writes. Filesystem reads are scoped
to ``vault_path`` existence checks via an injected ``FsProbe``.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Entity labels considered first-class by the audit. Matches the set used
# by the curator-health surface so the two views agree on what an entity is.
_ENTITY_LABELS: tuple[str, ...] = (
    "Organisation",
    "Person",
    "Outcome",
    "Concept",
    "Project",
    "Framework",
    "Technology",
    "Publication",
)

# Shared Cypher fragments. Extracted because the three audit-mode queries
# all open with the same node-label predicate and reuse the same
# ``id/name/label`` return shape (F17: ≥3-time duplication forbids inline
# literals).
_MATCH_LABELS_WHERE_PREFIX = "MATCH (n) WHERE ("
_RETURN_ID_NAME_LABEL = "RETURN n.id AS id, n.name AS name, labels(n)[0] AS label "


class EntityAuditMode(str, enum.Enum):
    """Which audit lens to apply. ``all`` is the deduplicated union."""

    JUNK = "junk"
    PATHS = "paths"
    ENRICHMENT = "enrichment"
    ALL = "all"


@dataclass(frozen=True)
class EntityAuditRow:
    """A single auditable entity. The shape downstream ``purge`` consumes.

    Attributes:
        id: Stable entity id from Neo4j (the ``n.id`` property, not Neo4j's
            internal node id). Used by ``entity_purge`` to issue the
            ``DETACH DELETE``.
        name: Display name of the entity.
        type: Neo4j label (``Organisation``, ``Person``, ...).
        mode: Which audit lens flagged this row (``junk``/``paths``/
            ``enrichment``).
        reason: Human-readable explanation (``"no vault_path and no summary"``,
            ``"vault file missing: <path>"``, ``"missing wikidata_qid"``).
    """

    id: str
    name: str
    type: str
    mode: str
    reason: str


@dataclass(frozen=True)
class AuditReport:
    """The full audit result. Includes a timestamp + mode + row list.

    The JSON shape this projects to is the on-disk format documented in
    the entity-audit runbook and consumed by ``entity_purge``.
    """

    mode: str
    generated_at: str
    rows: list[EntityAuditRow] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)


def _default_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_path_exists(path: str) -> bool:
    """Production filesystem probe — ``Path.exists()`` with no side effects."""
    if not path:
        return False
    try:
        return Path(path).exists()
    except OSError as exc:
        logger.warning("entity_audit path check failed for %s: %s", path, exc)
        return False


@dataclass(frozen=True)
class EntityAuditDeps:
    """Injectable dependencies for ``run_entity_audit``.

    Mirrors ``EntityGetDeps``: every dependency has a non-Optional
    ``default_factory`` so tests pass concrete fakes and production
    callers leave ``deps=None`` (F6: no test-only kwargs in production).

    Attributes:
        neo4j_client: Anything that exposes ``cypher(query, params) ->
            list[dict]`` and the ``available`` property. Production injects
            the real ``Neo4jClient``; tests use ``FakeNeo4jClient`` from
            ``tests/fixtures/neo4j_mock.py``.
        path_exists: Callable mapping a ``vault_path`` (str) to a boolean.
            Default is ``Path.exists`` on the literal path; tests pass a
            dict-backed fake to drive the paths audit deterministically.
        document_root: Optional vault root prepended to relative
            ``vault_path`` values before the existence check. When empty,
            ``vault_path`` is treated as already absolute or repository-
            relative.
        now_fn: Returns the ISO timestamp written into the report's
            ``generated_at``. Tests inject a deterministic value.
    """

    neo4j_client: Any = None
    path_exists: Callable[[str], bool] = field(default_factory=lambda: _default_path_exists)
    document_root: str = ""
    now_fn: Callable[[], str] = field(default_factory=lambda: _default_now)


def _label_where_clause() -> str:
    return " OR ".join(f"n:{lbl}" for lbl in _ENTITY_LABELS)


def _row_to_audit_row(row: dict[str, Any], *, mode: str, reason: str) -> EntityAuditRow | None:
    """Project a Cypher row to ``EntityAuditRow`` — skip rows missing id/name."""
    eid = str(row.get("id") or "")
    name = str(row.get("name") or "")
    if not eid and not name:
        return None
    return EntityAuditRow(
        id=eid,
        name=name,
        type=str(row.get("label") or "unknown"),
        mode=mode,
        reason=reason,
    )


def _query_rows(neo4j_client: Any, query: str) -> list[dict[str, Any]]:
    """Run a Cypher query and return rows, swallowing failures into ``[]``."""
    if neo4j_client is None or not getattr(neo4j_client, "available", False):
        return []
    try:
        return list(neo4j_client.cypher(query))
    except Exception as exc:
        logger.warning("entity_audit Cypher query failed: %s", exc, exc_info=True)
        return []


def _audit_junk(deps: EntityAuditDeps) -> list[EntityAuditRow]:
    """Find entities with neither ``vault_path`` nor ``summary``."""
    label_where = _label_where_clause()
    rows = _query_rows(
        deps.neo4j_client,
        f"{_MATCH_LABELS_WHERE_PREFIX}{label_where}) "
        "AND (n.vault_path IS NULL OR trim(toString(n.vault_path)) = '') "
        "AND (n.summary IS NULL OR trim(toString(n.summary)) = '') "
        f"{_RETURN_ID_NAME_LABEL}"
        "ORDER BY labels(n)[0], n.name",
    )
    out: list[EntityAuditRow] = []
    for r in rows:
        row = _row_to_audit_row(r, mode=EntityAuditMode.JUNK.value, reason="no vault_path and no summary")
        if row is not None:
            out.append(row)
    return out


def _audit_paths(deps: EntityAuditDeps) -> list[EntityAuditRow]:
    """Find entities whose ``vault_path`` no longer exists on disk."""
    label_where = _label_where_clause()
    rows = _query_rows(
        deps.neo4j_client,
        f"{_MATCH_LABELS_WHERE_PREFIX}{label_where}) "
        "AND n.vault_path IS NOT NULL AND trim(toString(n.vault_path)) <> '' "
        f"{_RETURN_ID_NAME_LABEL}, n.vault_path AS vault_path "
        "ORDER BY labels(n)[0], n.name",
    )
    out: list[EntityAuditRow] = []
    for r in rows:
        vault_path = str(r.get("vault_path") or "")
        if not vault_path:
            continue
        full_path = _resolve_path(vault_path, deps.document_root)
        if deps.path_exists(full_path):
            continue
        row = _row_to_audit_row(
            r,
            mode=EntityAuditMode.PATHS.value,
            reason=f"vault file missing: {vault_path}",
        )
        if row is not None:
            out.append(row)
    return out


def _resolve_path(vault_path: str, document_root: str) -> str:
    """Join the vault path to the document root when one is configured."""
    if not document_root:
        return vault_path
    return str(Path(document_root) / vault_path)


def _audit_enrichment(deps: EntityAuditDeps) -> list[EntityAuditRow]:
    """Find entities missing ``summary``, ``wikidata_qid``, or label."""
    label_where = _label_where_clause()
    rows = _query_rows(
        deps.neo4j_client,
        f"{_MATCH_LABELS_WHERE_PREFIX}{label_where}) "
        "AND ("
        "(n.summary IS NULL OR trim(toString(n.summary)) = '') "
        "OR (n.wikidata_qid IS NULL OR trim(toString(n.wikidata_qid)) = '') "
        "OR labels(n)[0] IS NULL"
        ") "
        f"{_RETURN_ID_NAME_LABEL}, "
        "n.summary AS summary, n.wikidata_qid AS wikidata_qid "
        "ORDER BY labels(n)[0], n.name",
    )
    out: list[EntityAuditRow] = []
    for r in rows:
        reason = _enrichment_reason(r)
        if not reason:
            continue
        row = _row_to_audit_row(r, mode=EntityAuditMode.ENRICHMENT.value, reason=reason)
        if row is not None:
            out.append(row)
    return out


def _enrichment_reason(row: dict[str, Any]) -> str:
    """Build the missing-fields reason for the enrichment audit."""
    missing: list[str] = []
    if not str(row.get("summary") or "").strip():
        missing.append("summary")
    if not str(row.get("wikidata_qid") or "").strip():
        missing.append("wikidata_qid")
    if not str(row.get("label") or "").strip():
        missing.append("label")
    if not missing:
        return ""
    return f"missing {', '.join(missing)}"


_MODE_DISPATCH: dict[EntityAuditMode, Callable[[EntityAuditDeps], list[EntityAuditRow]]] = {
    EntityAuditMode.JUNK: _audit_junk,
    EntityAuditMode.PATHS: _audit_paths,
    EntityAuditMode.ENRICHMENT: _audit_enrichment,
}


def _audit_all(deps: EntityAuditDeps) -> list[EntityAuditRow]:
    """Union of junk + paths + enrichment, deduplicated by ``id`` then ``name``."""
    seen: set[str] = set()
    out: list[EntityAuditRow] = []
    for mode in (EntityAuditMode.JUNK, EntityAuditMode.PATHS, EntityAuditMode.ENRICHMENT):
        for row in _MODE_DISPATCH[mode](deps):
            key = row.id or f"name:{row.name}"
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
    return out


def run_entity_audit(
    mode: EntityAuditMode = EntityAuditMode.ALL,
    *,
    deps: EntityAuditDeps | None = None,
) -> AuditReport:
    """Run the entity audit in the requested mode and return a structured report.

    Never raises — query failures are logged and reported as empty row lists.

    Args:
        mode: Which audit lens to apply.
        deps: Injectable dependencies; production callers leave ``None``.
    """
    d = deps or EntityAuditDeps()
    generated_at = d.now_fn()
    if mode == EntityAuditMode.ALL:
        rows = _audit_all(d)
    else:
        rows = _MODE_DISPATCH[mode](d)
    return AuditReport(mode=mode.value, generated_at=generated_at, rows=rows)


def format_report_json(report: AuditReport) -> str:
    """Project an ``AuditReport`` to the on-disk JSON shape.

    Shape: ``{"mode": str, "generated_at": str, "total": int,
    "rows": [{"id", "name", "type", "mode", "reason"}, ...]}``.
    """
    payload: dict[str, Any] = {
        "mode": report.mode,
        "generated_at": report.generated_at,
        "total": report.total,
        "rows": [dataclasses.asdict(r) for r in report.rows],
    }
    return json.dumps(payload, indent=2)


def format_report_text(report: AuditReport) -> str:
    """Project an ``AuditReport`` to an operator-facing table."""
    if not report.rows:
        return f"No audit rows found (mode={report.mode}, generated_at={report.generated_at}).\n"

    lines: list[str] = [
        f"Entity audit — mode={report.mode}, generated_at={report.generated_at}, total={report.total}",
        "",
    ]
    col_id = max((len(r.id) for r in report.rows), default=2)
    col_name = max((len(r.name) for r in report.rows), default=4)
    col_type = max((len(r.type) for r in report.rows), default=4)
    col_mode = max((len(r.mode) for r in report.rows), default=4)
    col_id = max(col_id, 2)
    col_name = max(col_name, 4)
    col_type = max(col_type, 4)
    col_mode = max(col_mode, 4)
    header = f"{'ID':<{col_id}}  {'NAME':<{col_name}}  {'TYPE':<{col_type}}  {'MODE':<{col_mode}}  REASON"
    lines.append(header)
    lines.append("-" * len(header))
    for r in report.rows:
        lines.append(f"{r.id:<{col_id}}  {r.name:<{col_name}}  {r.type:<{col_type}}  {r.mode:<{col_mode}}  {r.reason}")
    return "\n".join(lines) + "\n"
