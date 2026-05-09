"""
kairix.knowledge.store.crawler — Document-store-to-Neo4j entity crawler (ADR-014).

Derives entity nodes and relationship edges from the natural Obsidian document
store structure, then upserts them into Neo4j Community Edition via Neo4jClient.

Document store structure expected:
  {document_root}/02-Areas/00-Clients/{Org}/          → OrganisationNode per directory
  {document_root}/**/Network/People-Notes/             → PersonNode per .md file
  {document_root}/05-Knowledge/01-Domain-Outcomes/     → OutcomeNode per .md file (optional)
  Wikilinks ([[Name]]) across all .md files         → GraphEdge (MENTIONS)
  Frontmatter: org, role, interests, tier, etc.     → node properties

Designed to run idempotently. Safe to call on every document store sync — Neo4j
MERGE prevents duplicates. Any rename in Obsidian propagates on the next crawl.

Never raises — logs failures and continues. Returns a CrawlReport on completion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairix.knowledge.wikilinks import WIKILINK_RE
from kairix.utils import display_name, slugify

if TYPE_CHECKING:
    from kairix.knowledge.graph.models import OrganisationNode, PersonNode

logger = logging.getLogger(__name__)

# Regex: extract all [[wikilinks]] from text (ignores [[Link|Alias]] alias part)
_WIKILINK_PATTERN = WIKILINK_RE

# Directory names under 02-Areas to search for People-Notes
_PEOPLE_DIRS = {"People-Notes", "people-notes"}


@dataclass
class CrawlReport:
    """Summary of a document store crawl run."""

    document_root: str
    dry_run: bool
    organisations_found: int = 0
    persons_found: int = 0
    outcomes_found: int = 0
    edges_found: int = 0
    organisations_upserted: int = 0
    persons_upserted: int = 0
    outcomes_upserted: int = 0
    edges_upserted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


# ---------------------------------------------------------------------------
# Domain handlers
# ---------------------------------------------------------------------------


def crawl_organisations(
    root: Path, report: CrawlReport, neo4j_client: Any, dry_run: bool
) -> dict[str, OrganisationNode]:
    """Discover org dirs under 02-Areas/00-Clients, parse frontmatter, build nodes, upsert."""
    from kairix.knowledge.graph.models import OrganisationNode

    orgs: dict[str, OrganisationNode] = {}
    clients_dir = root / "02-Areas" / "00-Clients"
    if not clients_dir.exists():
        return orgs

    for org_dir in sorted(clients_dir.iterdir()):
        if not org_dir.is_dir():
            continue
        org_id = _to_slug(org_dir.name)
        # Canonical note: {OrgDir}/{OrgDir}.md (index file)
        index_md = org_dir / f"{org_dir.name}.md"
        canonical: Path | None
        if index_md.exists():
            canonical = index_md
        else:
            # Fall back to any .md file in the directory
            mds = list(org_dir.glob("*.md"))
            canonical = mds[0] if mds else None

        fm: dict[str, Any] = {}
        if canonical:
            fm = _parse_frontmatter(canonical)

        vault_path = str(canonical.relative_to(root)) if canonical else str(org_dir.relative_to(root))
        node = OrganisationNode(
            id=org_id,
            name=fm.get("name") or _to_display_name(org_dir.name),
            tier=str(fm.get("tier", "client")),
            engagement_status=str(fm.get("engagement_status", "active")),
            vault_path=vault_path,
            industry=_as_list(fm.get("industry")),
            geography=_as_list(fm.get("geography")),
            stakeholder_personas=_as_list(fm.get("stakeholder_personas")),
            aliases=_as_list(fm.get("aliases")),
        )
        orgs[org_dir.name.lower()] = node
        orgs[org_id] = node
        report.organisations_found += 1
        logger.debug("org: %s (%s)", node.name, vault_path)

        if not dry_run:
            if neo4j_client.upsert_organisation(node):
                report.organisations_upserted += 1
            else:
                report.errors.append(f"Failed to upsert org: {org_id}")

    return orgs


def crawl_persons(
    root: Path,
    orgs: dict[str, OrganisationNode],
    report: CrawlReport,
    neo4j_client: Any,
    dry_run: bool,
) -> dict[str, PersonNode]:
    """Discover person files, resolve orgs, build nodes, create WORKS_AT edges."""
    from kairix.knowledge.graph.models import EdgeKind, GraphEdge, PersonNode

    persons: dict[str, PersonNode] = {}
    for people_dir in _find_people_dirs(root):
        for md_file in sorted(people_dir.glob("*.md")):
            person_id = _to_slug(md_file.stem)
            fm = _parse_frontmatter(md_file)
            vault_path = str(md_file.relative_to(root))

            # Resolve org by name lookup in discovered orgs
            org_raw = str(fm.get("org") or fm.get("organisation") or "")
            org_id = _resolve_org_id(org_raw, orgs) if org_raw else ""

            person_node = PersonNode(
                id=person_id,
                name=fm.get("name") or _to_display_name(md_file.stem),
                org=org_id,
                role=str(fm.get("role") or ""),
                relationship_type=str(fm.get("relationship_type") or "network"),
                last_interaction=str(fm.get("last_interaction") or ""),
                vault_path=vault_path,
                interests=_as_list(fm.get("interests")),
                aliases=_as_list(fm.get("aliases")),
            )
            persons[person_id] = person_node
            report.persons_found += 1
            logger.debug("person: %s (%s)", person_node.name, vault_path)

            if not dry_run:
                if neo4j_client.upsert_person(person_node):
                    report.persons_upserted += 1
                else:
                    report.errors.append(f"Failed to upsert person: {person_id}")

            # WORKS_AT edge when org is known
            if org_id:
                edge = GraphEdge(
                    from_id=person_id,
                    from_label="Person",
                    to_id=org_id,
                    to_label="Organisation",
                    kind=EdgeKind.WORKS_AT,
                )
                report.edges_found += 1
                if not dry_run:
                    if neo4j_client.upsert_edge(edge):
                        report.edges_upserted += 1
                    else:
                        report.errors.append(f"Failed to upsert WORKS_AT edge: {person_id}→{org_id}")

    return persons


def crawl_outcomes(root: Path, report: CrawlReport, neo4j_client: Any, dry_run: bool) -> None:
    """Discover outcome files under 05-Knowledge/01-Domain-Outcomes, build nodes, upsert."""
    from kairix.knowledge.graph.models import OutcomeNode

    outcomes_dir = root / "05-Knowledge" / "01-Domain-Outcomes"
    if not outcomes_dir.exists():
        return

    for md_file in sorted(outcomes_dir.rglob("*.md")):
        outcome_id = _to_slug(md_file.stem)
        fm = _parse_frontmatter(md_file)
        vault_path = str(md_file.relative_to(root))

        outcome_node = OutcomeNode(
            id=outcome_id,
            name=fm.get("name") or _to_display_name(md_file.stem),
            domain=str(fm.get("domain") or ""),
            vault_path=vault_path,
        )
        report.outcomes_found += 1
        logger.debug("outcome: %s (%s)", outcome_node.name, vault_path)

        if not dry_run:
            if neo4j_client.upsert_outcome(outcome_node):
                report.outcomes_upserted += 1
            else:
                report.errors.append(f"Failed to upsert outcome: {outcome_id}")


def crawl_wikilink_edges(
    root: Path,
    orgs: dict[str, OrganisationNode],
    persons: dict[str, PersonNode],
    report: CrawlReport,
    neo4j_client: Any,
    dry_run: bool,
) -> None:
    """Extract wikilinks from all .md files and create MENTIONS edges."""
    from kairix.knowledge.graph.models import EdgeKind, GraphEdge

    all_known = set(orgs.keys()) | set(persons.keys())
    for md_file in root.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        source_path = str(md_file.relative_to(root))
        for link_target in _WIKILINK_PATTERN.findall(text):
            target_slug = _to_slug(link_target.split("/")[-1])
            if target_slug not in all_known:
                continue
            # Determine label of target
            if target_slug in orgs:
                to_label, to_id = "Organisation", orgs[target_slug].id
            else:
                to_label, to_id = "Person", persons[target_slug].id

            edge = GraphEdge(
                from_id=_to_slug(md_file.stem),
                from_label="Document",
                to_id=to_id,
                to_label=to_label,
                kind=EdgeKind.MENTIONS,
                props={"source_path": source_path},
            )
            report.edges_found += 1
            if not dry_run:
                if neo4j_client.upsert_edge(edge):
                    report.edges_upserted += 1


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def crawl(
    document_root: str | Path,
    neo4j_client: Any,
    dry_run: bool = False,
) -> CrawlReport:
    """
    Crawl the document store and upsert entity nodes + edges into Neo4j.

    Args:
        document_root: Absolute path to the Obsidian document store root.
        neo4j_client: An open Neo4jClient instance. Pass a mock for testing.
        dry_run: When True, discover and log entities without writing to Neo4j.

    Returns:
        CrawlReport describing nodes found and upserted.
    """
    root = Path(document_root)
    report = CrawlReport(document_root=str(root), dry_run=dry_run)

    if not root.exists():
        report.errors.append(f"document_root does not exist: {root}")
        return report

    orgs = crawl_organisations(root, report, neo4j_client, dry_run)
    persons = crawl_persons(root, orgs, report, neo4j_client, dry_run)
    crawl_outcomes(root, report, neo4j_client, dry_run)
    crawl_wikilink_edges(root, orgs, persons, report, neo4j_client, dry_run)

    return report


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_frontmatter(path: Path) -> dict[str, Any]:
    """Parse YAML frontmatter from a markdown file. Returns {} on any failure.

    Delegates text extraction to ``extract_existing_frontmatter``, then
    re-parses with ``yaml.safe_load`` for full YAML support (lists, nested values)
    that the crawler's entity model requires.
    """
    from kairix.knowledge.reflib.frontmatter import extract_existing_frontmatter

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}

    simple_parsed, _ = extract_existing_frontmatter(text)
    if simple_parsed is None:
        # No frontmatter found at all — also try lenient match (no trailing newline)
        import re

        # NOSONAR: non-greedy `.*?` bounded by literal `\n---`
        # terminator; input is markdown frontmatter (file-bounded).
        lenient = re.match(r"\A---\s*\n(.*?)\n---", text, re.DOTALL)
        if not lenient:
            return {}
        block = lenient.group(1)
    else:
        # Re-extract the raw YAML block for full yaml.safe_load parsing
        import re

        # NOSONAR: same bounded-input rationale as above.
        match = re.match(r"\A---\s*\n(.*?)\n---", text, re.DOTALL)
        if not match:
            return dict(simple_parsed)  # fallback to simple parsing
        block = match.group(1)

    try:
        import yaml

        result = yaml.safe_load(block)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _find_people_dirs(document_root: Path) -> list[Path]:
    """Find all People-Notes directories under the vault root."""
    found: list[Path] = []
    for candidate in document_root.rglob("*"):
        if candidate.is_dir() and candidate.name in _PEOPLE_DIRS:
            found.append(candidate)
    return found


def _to_slug(name: str) -> str:
    """Convert a vault directory/file name to a lowercase hyphenated slug.

    Delegates to ``kairix.utils.slugify`` — kept as a local alias for
    backwards compatibility (tests import this private name).
    """
    return slugify(name)


def _to_display_name(name: str) -> str:
    """Convert slug/filename to a display name (title case, hyphens → spaces)."""
    return display_name(name)


def _as_list(value: Any) -> list[str]:
    """Normalise a scalar, list, or None frontmatter field to list[str]."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _resolve_org_id(org_raw: str, orgs: dict[str, OrganisationNode]) -> str:
    """Find an org id by name or partial match in the discovered orgs dict."""
    slug = _to_slug(org_raw)
    if slug in orgs:
        return str(orgs[slug].id)
    # Partial match: org_raw is a substring of a known org name
    org_raw_lower = org_raw.lower()
    for key, node in orgs.items():
        if org_raw_lower in node.name.lower() or org_raw_lower in key:
            return str(node.id)
    return ""
