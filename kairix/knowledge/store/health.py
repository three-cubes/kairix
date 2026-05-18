"""
kairix.knowledge.store.health — Document store and entity graph health check (Neo4j-primary).

Queries Neo4j for entity completeness, relationship density, and synthesis
coverage. Falls back to a minimal report when Neo4j is unavailable.

Never raises — returns a StoreHealthReport on any failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Node property completeness thresholds
_MIN_SUMMARY_LENGTH = 50  # chars — nodes shorter than this count as missing
_STALE_DAYS = 90  # days without updated_at change = stale


@dataclass
class StoreHealthReport:
    """
    Result of a document store + graph health check.

    Covers Neo4j node counts, property completeness, and relationship density.
    `ok` is True only when Neo4j is available and all checks pass.
    """

    generated_at: str
    neo4j_available: bool = False

    # Node counts
    organisation_count: int = 0
    person_count: int = 0
    outcome_count: int = 0

    # Completeness
    orgs_missing_vault_path: int = 0
    persons_missing_vault_path: int = 0
    orgs_missing_summary: int = 0
    persons_missing_summary: int = 0

    # Relationship density
    works_at_edge_count: int = 0
    knows_edge_count: int = 0
    mentions_edge_count: int = 0

    # Issues
    issues: list[str] = field(default_factory=list)

    @property
    def total_entities(self) -> int:
        return self.organisation_count + self.person_count + self.outcome_count

    @property
    def ok(self) -> bool:
        return (
            self.neo4j_available
            and self.total_entities > 0
            and self.orgs_missing_vault_path == 0
            and self.persons_missing_vault_path == 0
            and len(self.issues) == 0
        )


def _populate_node_counts(neo4j_client: Any, report: StoreHealthReport) -> None:
    """Fill organisation/person/outcome counts from a single COUNT-by-label query."""
    try:
        rows = neo4j_client.cypher(
            "MATCH (n) WHERE labels(n)[0] IN ['Organisation','Person','Outcome'] "
            "RETURN labels(n)[0] AS label, COUNT(*) AS cnt"
        )
        for row in rows:
            label = row.get("label", "")
            cnt = int(row.get("cnt", 0))
            if label == "Organisation":
                report.organisation_count = cnt
            elif label == "Person":
                report.person_count = cnt
            elif label == "Outcome":
                report.outcome_count = cnt
    except Exception as exc:
        logger.warning("store health: node count query failed — %s", exc)
        report.issues.append(f"Node count query failed: {exc}")


def _run_count_query(neo4j_client: Any, query: str, params: dict[str, Any] | None, label: str) -> int | None:
    """Run a COUNT(n) Cypher query and return the first row's cnt as int, or None on failure."""
    try:
        rows = neo4j_client.cypher(query, params) if params else neo4j_client.cypher(query)
    except Exception as exc:
        logger.warning("store health: %s check failed — %s", label, exc)
        return None
    if not rows:
        return None
    return int(rows[0].get("cnt", 0))


def _populate_property_gaps(neo4j_client: Any, report: StoreHealthReport) -> None:
    """Fill orgs/persons missing-vault_path and missing-summary counts."""
    checks: list[tuple[str, str, dict[str, Any] | None, str]] = [
        (
            "orgs_missing_vault_path",
            "MATCH (n:Organisation) WHERE n.vault_path IS NULL OR n.vault_path = '' RETURN COUNT(n) AS cnt",
            None,
            "org vault_path",
        ),
        (
            "persons_missing_vault_path",
            "MATCH (n:Person) WHERE n.vault_path IS NULL OR n.vault_path = '' RETURN COUNT(n) AS cnt",
            None,
            "person vault_path",
        ),
        (
            "orgs_missing_summary",
            "MATCH (n:Organisation) WHERE n.summary IS NULL OR size(n.summary) < $min_len RETURN COUNT(n) AS cnt",
            {"min_len": _MIN_SUMMARY_LENGTH},
            "org summary",
        ),
        (
            "persons_missing_summary",
            "MATCH (n:Person) WHERE n.summary IS NULL OR size(n.summary) < $min_len RETURN COUNT(n) AS cnt",
            {"min_len": _MIN_SUMMARY_LENGTH},
            "person summary",
        ),
    ]
    for attr, query, params, label in checks:
        value = _run_count_query(neo4j_client, query, params, label)
        if value is not None:
            setattr(report, attr, value)


def _populate_relationship_counts(neo4j_client: Any, report: StoreHealthReport) -> None:
    """Fill the three relationship edge-count fields."""
    edges = (
        ("WORKS_AT", "works_at_edge_count"),
        ("KNOWS", "knows_edge_count"),
        ("MENTIONS", "mentions_edge_count"),
    )
    for rel_type, attr in edges:
        value = _run_count_query(
            neo4j_client,
            f"MATCH ()-[r:{rel_type}]->() RETURN COUNT(r) AS cnt",
            None,
            f"{rel_type} edge count",
        )
        if value is not None:
            setattr(report, attr, value)


def _surface_issues(report: StoreHealthReport) -> None:
    """Append operator-facing diagnostic strings to ``report.issues``."""
    if report.total_entities == 0:
        report.issues.append("No entity nodes found in Neo4j — run `kairix store crawl` first")
    if report.orgs_missing_vault_path > 0:
        report.issues.append(
            f"{report.orgs_missing_vault_path} organisation(s) missing vault_path — re-run store crawl"
        )
    if report.persons_missing_vault_path > 0:
        report.issues.append(f"{report.persons_missing_vault_path} person(s) missing vault_path — re-run store crawl")


def run_store_health(
    neo4j_client: Any,
    document_root: str | None = None,
) -> StoreHealthReport:
    """Run document store + entity graph health check.

    Args:
        neo4j_client: Neo4jClient instance. When unavailable, returns minimal report.
        document_root: Optional document root for file-system checks (not yet used in v0).

    Returns:
        StoreHealthReport describing entity graph state.
    """
    _ = document_root  # reserved for v1 file-system checks
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = StoreHealthReport(generated_at=generated_at)

    if not neo4j_client.available:
        report.issues.append("Neo4j unavailable — graph health check skipped")
        return report

    report.neo4j_available = True
    _populate_node_counts(neo4j_client, report)
    _populate_property_gaps(neo4j_client, report)
    _populate_relationship_counts(neo4j_client, report)
    _surface_issues(report)
    return report


def format_health_text(report: StoreHealthReport) -> str:
    """Format a StoreHealthReport as human-readable text."""
    lines = [
        "# Kairix — Document Store Health Report",
        f"_Generated: {report.generated_at}_",
        "",
    ]

    if not report.neo4j_available:
        lines += [
            "⚠ Neo4j unavailable — run `kairix store crawl` to populate the graph",
            "",
        ]
        for issue in report.issues:
            lines.append(f"  - {issue}")
        return "\n".join(lines)

    lines += [
        f"## Entity Nodes (total: {report.total_entities})",
        f"  Organisations: {report.organisation_count}",
        f"  Persons:       {report.person_count}",
        f"  Outcomes:      {report.outcome_count}",
        "",
        "## Property Completeness",
        f"  Orgs missing vault_path:     {report.orgs_missing_vault_path}",
        f"  Persons missing vault_path:  {report.persons_missing_vault_path}",
        f"  Orgs missing summary:        {report.orgs_missing_summary}",
        f"  Persons missing summary:     {report.persons_missing_summary}",
        "",
        "## Relationship Density",
        f"  WORKS_AT edges:  {report.works_at_edge_count}",
        f"  KNOWS edges:     {report.knows_edge_count}",
        f"  MENTIONS edges:  {report.mentions_edge_count}",
        "",
    ]

    if report.issues:
        lines.append("## Issues")
        for issue in report.issues:
            lines.append(f"  ⚠ {issue}")
        lines.append("")

    status = "✅ HEALTHY" if report.ok else "⚠ ISSUES FOUND"
    lines.append(f"**Status: {status}**")
    return "\n".join(lines) + "\n"


# Backwards-compat aliases
VaultHealthReport = StoreHealthReport
run_vault_health = run_store_health
