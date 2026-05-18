"""
kairix.agents.curator.health — Entity graph health check (CA-1).

Neo4j is the canonical entity store. All health checks query the graph
directly via Cypher. No SQLite dependency.

Checks:
  - Entity count by type
  - Synthesis failures (entities with no summary property)
  - Missing vault_path (entities not linked to canonical vault note)
  - Staleness (entities with last_seen before threshold, if tracked)

Never raises — returns a HealthReport reflecting available data.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

STALENESS_THRESHOLD_DAYS = 90

_NO_ISSUES_LINE = "✅ None."

_ENTITY_LABELS = (
    "Organisation",
    "Person",
    "Outcome",
    "Concept",
    "Project",
    "Framework",
    "Technology",
    "Publication",
)


@dataclass
class HealthIssue:
    """A single entity-level health issue."""

    entity_id: str
    name: str
    entity_type: str
    detail: str


@dataclass
class HealthReport:
    """
    Result of a single Curator health check run.

    All list fields are empty when no issues are found.
    Use report.ok to test overall health at a glance.
    """

    generated_at: str  # ISO UTC timestamp
    total_entities: int
    entities_by_type: dict[str, int]
    synthesis_failures: list[HealthIssue] = field(default_factory=list)
    stale_entities: list[HealthIssue] = field(default_factory=list)
    missing_vault_path: list[HealthIssue] = field(default_factory=list)
    neo4j_available: bool = False
    neo4j_node_counts: dict[str, int] = field(default_factory=dict)
    staleness_threshold_days: int = STALENESS_THRESHOLD_DAYS

    @property
    def issue_count(self) -> int:
        """Total number of entity-level issues found."""
        return len(self.synthesis_failures) + len(self.stale_entities) + len(self.missing_vault_path)

    @property
    def ok(self) -> bool:
        """True when no issues were found."""
        return self.issue_count == 0


def _row_to_health_issue(row: dict[str, Any], default_detail: str) -> HealthIssue | None:
    """Convert one Cypher row to a HealthIssue, or None when neither id nor name is set.

    When the row carries ``last_seen`` (stale-entity queries), the detail
    field is overridden with a human-readable last-seen marker.
    """
    eid = str(row.get("id") or "")
    name = str(row.get("name") or "")
    if not (eid or name):
        return None
    label = str(row.get("label") or "unknown")
    detail = f"last seen: {row.get('last_seen') or 'never'}" if "last_seen" in row else default_detail
    return HealthIssue(entity_id=eid, name=name, entity_type=label.lower(), detail=detail)


def _query_entity_issues(
    neo4j_client: Any,
    cypher: str,
    detail: str,
    log_label: str,
    params: dict[str, Any] | None = None,
    log_level: str = "warning",
) -> list[HealthIssue]:
    """Run a Cypher query and convert rows to HealthIssue objects.

    Each row must return id, name, label columns. Optionally returns
    last_seen for stale-entity queries (used to build detail string).

    Returns [] on query failure (logged at log_level).
    """
    try:
        rows = neo4j_client.cypher(cypher, params) if params else neo4j_client.cypher(cypher)
    except Exception as exc:
        getattr(logger, log_level)("health: %s query failed — %s", log_label, exc)
        return []
    issues: list[HealthIssue] = []
    for row in rows:
        issue = _row_to_health_issue(row, detail)
        if issue is not None:
            issues.append(issue)
    return issues


def run_health_check(
    neo4j_client: Any,
    staleness_days: int = STALENESS_THRESHOLD_DAYS,
) -> HealthReport:
    """
    Run a full entity graph health check against Neo4j.

    Args:
        neo4j_client: Neo4jClient instance (from kairix.knowledge.graph.client.get_client()).
            When unavailable, returns an empty report with neo4j_available=False.
        staleness_days: Entities with no activity for this many days are flagged
            as stale. Defaults to STALENESS_THRESHOLD_DAYS (90).

    Returns:
        HealthReport describing the current state of the entity graph.
    """
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if neo4j_client is None or not getattr(neo4j_client, "available", False):
        return HealthReport(
            generated_at=generated_at,
            total_entities=0,
            entities_by_type={},
            neo4j_available=False,
            staleness_threshold_days=staleness_days,
        )

    label_filter = "['" + "','".join(_ENTITY_LABELS) + "']"
    label_where = " OR ".join(f"n:{lbl}" for lbl in _ENTITY_LABELS)

    # Entity counts
    entities_by_type: dict[str, int] = {}
    total_entities = 0
    try:
        rows = neo4j_client.cypher(
            f"MATCH (n) WHERE labels(n)[0] IN {label_filter} RETURN labels(n)[0] AS label, COUNT(*) AS cnt"
        )
        for r in rows:
            label = r.get("label")
            cnt = r.get("cnt")
            if label is not None and cnt is not None:
                entities_by_type[str(label).lower()] = int(cnt)
                total_entities += int(cnt)
    except Exception as exc:
        logger.warning("health: entity count query failed — %s", exc)

    # Individual check sections
    synthesis_failures = _query_entity_issues(
        neo4j_client,
        f"MATCH (n) WHERE ({label_where}) "
        "AND (n.summary IS NULL OR trim(toString(n.summary)) = '') "
        "RETURN n.id AS id, n.name AS name, labels(n)[0] AS label "
        "ORDER BY labels(n)[0], n.name",
        detail="no summary",
        log_label="synthesis failure",
    )

    missing_vault_path = _query_entity_issues(
        neo4j_client,
        f"MATCH (n) WHERE ({label_where}) "
        "AND (n.vault_path IS NULL OR trim(toString(n.vault_path)) = '') "
        "RETURN n.id AS id, n.name AS name, labels(n)[0] AS label "
        "ORDER BY labels(n)[0], n.name",
        detail="vault_path not set",
        log_label="missing vault_path",
    )

    threshold_str = (datetime.now(timezone.utc) - timedelta(days=staleness_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_entities = _query_entity_issues(
        neo4j_client,
        f"MATCH (n) WHERE ({label_where}) "
        "AND n.last_seen IS NOT NULL AND toString(n.last_seen) < $threshold "
        "RETURN n.id AS id, n.name AS name, labels(n)[0] AS label, "
        "n.last_seen AS last_seen "
        "ORDER BY labels(n)[0], n.name",
        detail="",  # overridden by last_seen in row
        log_label="stale entity",
        params={"threshold": threshold_str},
        log_level="debug",
    )

    neo4j_node_counts = {k.capitalize(): v for k, v in entities_by_type.items()}

    return HealthReport(
        generated_at=generated_at,
        total_entities=total_entities,
        entities_by_type=entities_by_type,
        synthesis_failures=synthesis_failures,
        stale_entities=stale_entities,
        missing_vault_path=missing_vault_path,
        neo4j_available=True,
        neo4j_node_counts=neo4j_node_counts,
        staleness_threshold_days=staleness_days,
    )


def _format_issue_section(heading: str, issues: list[HealthIssue]) -> list[str]:
    """Render a single issue section as Markdown lines."""
    lines = ["", heading, ""]
    if issues:
        for issue in issues:
            lines.append(f"- ⚠ `{issue.entity_id}` ({issue.entity_type}) — {issue.detail}")
    else:
        lines.append(_NO_ISSUES_LINE)
    return lines


def _format_entity_counts(report: HealthReport) -> list[str]:
    """Render the entity-count table (or empty-state line)."""
    if not report.entities_by_type:
        return ["_No entities found._"]
    lines = ["| Type | Count |", "|------|-------|"]
    for etype, cnt in sorted(report.entities_by_type.items()):
        lines.append(f"| {etype} | {cnt} |")
    return lines


def _format_neo4j_section(report: HealthReport) -> list[str]:
    """Render the Neo4j availability + node-count line."""
    if not report.neo4j_available:
        return ["⚠ Unavailable."]
    if report.neo4j_node_counts:
        node_summary = ", ".join(f"{cnt} {label}" for label, cnt in sorted(report.neo4j_node_counts.items()))
        return [f"✅ Connected — {node_summary}"]
    return ["✅ Connected — no nodes found"]


def _format_status_footer(report: HealthReport) -> list[str]:
    """Render the trailing status line; lists the failure dimensions when not OK."""
    if report.ok:
        return ["**Status: ✅ HEALTHY** — no issues found"]
    parts: list[str] = []
    if report.synthesis_failures:
        parts.append(f"{len(report.synthesis_failures)} synthesis failure(s)")
    if report.stale_entities:
        parts.append(f"{len(report.stale_entities)} stale")
    if report.missing_vault_path:
        parts.append(f"{len(report.missing_vault_path)} missing vault path")
    return [f"**Status: ⚠ ISSUES FOUND** — {', '.join(parts)}"]


def format_report_text(report: HealthReport) -> str:
    """Format a HealthReport as vault-ready Markdown."""
    lines: list[str] = [
        "# Kairix — Entity Health Report",
        f"_Generated: {report.generated_at}_",
        "",
        f"## Entity Counts (total: {report.total_entities})",
        "",
    ]
    lines += _format_entity_counts(report)
    lines += _format_issue_section(
        f"## Synthesis Failures ({len(report.synthesis_failures)})",
        report.synthesis_failures,
    )
    lines += _format_issue_section(
        f"## Stale Entities ({len(report.stale_entities)}, threshold: {report.staleness_threshold_days} days)",
        report.stale_entities,
    )
    lines += _format_issue_section(
        f"## Missing Vault Path ({len(report.missing_vault_path)})",
        report.missing_vault_path,
    )
    lines += ["", "## Graph (Neo4j)", ""]
    lines += _format_neo4j_section(report)
    lines += ["", "---"]
    lines += _format_status_footer(report)
    return "\n".join(lines) + "\n"


def format_report_json(report: HealthReport) -> str:
    """Format a HealthReport as indented JSON.

    ``dataclasses.asdict`` strips ``@property`` descriptors, so the
    operator-visible booleans (``ok``, ``issue_count``) are added back
    in explicitly — operators rely on them for green/red gating.
    """
    payload = dataclasses.asdict(report)
    payload["ok"] = report.ok
    payload["issue_count"] = report.issue_count
    return json.dumps(payload, indent=2)
