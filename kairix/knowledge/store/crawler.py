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

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairix.knowledge.entities.filters import KnownEntityAllowlist, OverrideMatchCounter
from kairix.knowledge.entities.overrides import EntityOverrides
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
class OverrideCoverage:
    """Per-crawl override-coverage summary.

    Closes #263. Records which entries in the
    ``_entity-overrides.md`` allowlist were matched (at least once)
    against the crawled text and which never fired. The crawl orchestrator
    serialises this to ``${KAIRIX_DATA_DIR}/entity-override-coverage.json``
    so curators can spot dead allowlist entries without an O(N) shell loop
    over ``kairix entity get``.
    """

    crawl_started_at: str
    total_overrides: int = 0
    matched: int = 0
    never_matched: list[str] = field(default_factory=list)
    match_counts: dict[str, int] = field(default_factory=dict)


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
    # #262 — reset path summary; None when --reset was not requested.
    reset_nodes_deleted: int | None = None
    reset_relationships_deleted: int | None = None
    # #263 — override coverage; None when no overrides file was supplied.
    override_coverage: OverrideCoverage | None = None
    override_coverage_path: str | None = None

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


# ---------------------------------------------------------------------------
# Domain handlers
# ---------------------------------------------------------------------------


def _canonical_org_note(org_dir: Path) -> Path | None:
    """Pick the canonical .md file for an org directory.

    Prefers the index file ``{OrgDir}/{OrgDir}.md``; falls back to the
    first ``*.md`` in the directory; returns None when none exist.
    """
    index_md = org_dir / f"{org_dir.name}.md"
    if index_md.exists():
        return index_md
    mds = list(org_dir.glob("*.md"))
    return mds[0] if mds else None


def _build_organisation_node(root: Path, org_dir: Path) -> OrganisationNode:
    """Read frontmatter from the canonical note and construct an OrganisationNode."""
    from kairix.knowledge.graph.models import OrganisationNode

    canonical = _canonical_org_note(org_dir)
    fm: dict[str, Any] = parse_frontmatter(canonical) if canonical else {}
    vault_path = str(canonical.relative_to(root)) if canonical else str(org_dir.relative_to(root))
    return OrganisationNode(
        id=slugify(org_dir.name),
        name=fm.get("name") or display_name(org_dir.name),
        tier=str(fm.get("tier", "client")),
        engagement_status=str(fm.get("engagement_status", "active")),
        vault_path=vault_path,
        industry=as_list(fm.get("industry")),
        geography=as_list(fm.get("geography")),
        stakeholder_personas=as_list(fm.get("stakeholder_personas")),
        aliases=as_list(fm.get("aliases")),
    )


def crawl_organisations(
    root: Path, report: CrawlReport, neo4j_client: Any, dry_run: bool
) -> dict[str, OrganisationNode]:
    """Discover org dirs under 02-Areas/00-Clients, parse frontmatter, build nodes, upsert."""
    orgs: dict[str, OrganisationNode] = {}
    clients_dir = root / "02-Areas" / "00-Clients"
    if not clients_dir.exists():
        return orgs

    for org_dir in sorted(clients_dir.iterdir()):
        if not org_dir.is_dir():
            continue
        node = _build_organisation_node(root, org_dir)
        orgs[org_dir.name.lower()] = node
        orgs[node.id] = node
        report.organisations_found += 1
        logger.debug("org: %s (%s)", node.name, node.vault_path)
        if not dry_run:
            if neo4j_client.upsert_organisation(node):
                report.organisations_upserted += 1
            else:
                report.errors.append(f"Failed to upsert org: {node.id}")

    return orgs


def _build_person_node(root: Path, md_file: Path, orgs: dict[str, OrganisationNode]) -> PersonNode:
    """Parse a person .md, resolve its org, and construct a PersonNode."""
    from kairix.knowledge.graph.models import PersonNode

    fm = parse_frontmatter(md_file)
    org_raw = str(fm.get("org") or fm.get("organisation") or "")
    org_id = _resolve_org_id(org_raw, orgs) if org_raw else ""
    return PersonNode(
        id=slugify(md_file.stem),
        name=fm.get("name") or display_name(md_file.stem),
        org=org_id,
        role=str(fm.get("role") or ""),
        relationship_type=str(fm.get("relationship_type") or "network"),
        last_interaction=str(fm.get("last_interaction") or ""),
        vault_path=str(md_file.relative_to(root)),
        interests=as_list(fm.get("interests")),
        aliases=as_list(fm.get("aliases")),
    )


def _upsert_works_at_edge(
    person_id: str,
    org_id: str,
    report: CrawlReport,
    neo4j_client: Any,
    dry_run: bool,
) -> None:
    """Create the Person->Organisation WORKS_AT edge when both ends are known."""
    if not org_id:
        return
    from kairix.knowledge.graph.models import EdgeKind, GraphEdge

    edge = GraphEdge(
        from_id=person_id,
        from_label="Person",
        to_id=org_id,
        to_label="Organisation",
        kind=EdgeKind.WORKS_AT,
    )
    report.edges_found += 1
    if dry_run:
        return
    if neo4j_client.upsert_edge(edge):
        report.edges_upserted += 1
    else:
        report.errors.append(f"Failed to upsert WORKS_AT edge: {person_id}→{org_id}")


def crawl_persons(
    root: Path,
    orgs: dict[str, OrganisationNode],
    report: CrawlReport,
    neo4j_client: Any,
    dry_run: bool,
) -> dict[str, PersonNode]:
    """Discover person files, resolve orgs, build nodes, create WORKS_AT edges."""
    persons: dict[str, PersonNode] = {}
    for people_dir in _find_people_dirs(root):
        for md_file in sorted(people_dir.glob("*.md")):
            person_node = _build_person_node(root, md_file, orgs)
            persons[person_node.id] = person_node
            report.persons_found += 1
            logger.debug("person: %s (%s)", person_node.name, person_node.vault_path)
            if not dry_run:
                if neo4j_client.upsert_person(person_node):
                    report.persons_upserted += 1
                else:
                    report.errors.append(f"Failed to upsert person: {person_node.id}")
            _upsert_works_at_edge(person_node.id, person_node.org, report, neo4j_client, dry_run)
    return persons


def crawl_outcomes(root: Path, report: CrawlReport, neo4j_client: Any, dry_run: bool) -> None:
    """Discover outcome files under 05-Knowledge/01-Domain-Outcomes, build nodes, upsert."""
    from kairix.knowledge.graph.models import OutcomeNode

    outcomes_dir = root / "05-Knowledge" / "01-Domain-Outcomes"
    if not outcomes_dir.exists():
        return

    for md_file in sorted(outcomes_dir.rglob("*.md")):
        outcome_id = slugify(md_file.stem)
        fm = parse_frontmatter(md_file)
        vault_path = str(md_file.relative_to(root))

        outcome_node = OutcomeNode(
            id=outcome_id,
            name=fm.get("name") or display_name(md_file.stem),
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


def _resolve_link_target(
    target_slug: str,
    orgs: dict[str, OrganisationNode],
    persons: dict[str, PersonNode],
) -> tuple[str, str] | None:
    """Map a wikilink target slug onto (to_label, to_id) for orgs/persons, or None."""
    if target_slug in orgs:
        return "Organisation", orgs[target_slug].id
    if target_slug in persons:
        return "Person", persons[target_slug].id
    return None


def _emit_mentions_edges(
    md_file: Path,
    text: str,
    source_path: str,
    orgs: dict[str, OrganisationNode],
    persons: dict[str, PersonNode],
    report: CrawlReport,
    neo4j_client: Any,
    dry_run: bool,
) -> None:
    """Scan one file's text for wikilinks and emit MENTIONS edges."""
    from kairix.knowledge.graph.models import EdgeKind, GraphEdge

    for link_target in _WIKILINK_PATTERN.findall(text):
        target_slug = slugify(link_target.split("/")[-1])
        resolved = _resolve_link_target(target_slug, orgs, persons)
        if resolved is None:
            continue
        to_label, to_id = resolved
        edge = GraphEdge(
            from_id=slugify(md_file.stem),
            from_label="Document",
            to_id=to_id,
            to_label=to_label,
            kind=EdgeKind.MENTIONS,
            props={"source_path": source_path},
        )
        report.edges_found += 1
        if not dry_run and neo4j_client.upsert_edge(edge):
            report.edges_upserted += 1


def crawl_wikilink_edges(
    root: Path,
    orgs: dict[str, OrganisationNode],
    persons: dict[str, PersonNode],
    report: CrawlReport,
    neo4j_client: Any,
    dry_run: bool,
    *,
    allowlist_filter: KnownEntityAllowlist | None = None,
) -> None:
    """Extract wikilinks from all .md files and create MENTIONS edges.

    When ``allowlist_filter`` is supplied, each file's text is also fed to
    the filter so its shared :class:`OverrideMatchCounter` records which
    allowlist entries fired during the crawl (#263). The filter return
    value is discarded — we only need the side-effect on the counter.
    """
    for md_file in root.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        source_path = str(md_file.relative_to(root))
        _emit_mentions_edges(md_file, text, source_path, orgs, persons, report, neo4j_client, dry_run)
        if allowlist_filter is not None:
            # ``apply`` is the public surface; we pass an empty suggestion
            # list because the crawler does not run NER — the goal here is
            # purely to record per-override match counts.
            allowlist_filter.apply([], context=text)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def crawl(
    document_root: str | Path,
    neo4j_client: Any,
    dry_run: bool = False,
    *,
    reset: bool = False,
    overrides: EntityOverrides | None = None,
    coverage_path: Path | None = None,
) -> CrawlReport:
    """
    Crawl the document store and upsert entity nodes + edges into Neo4j.

    Args:
        document_root: Absolute path to the Obsidian document store root.
        neo4j_client: An open Neo4jClient instance. Pass a mock for testing.
        dry_run: When True, discover and log entities without writing to Neo4j.
        reset: When True, DETACH DELETE everything in the graph before walking
            the document root. Closes #262. ``dry_run=True`` short-circuits
            the destructive call — the report still records the operator's
            intent but the graph is untouched.
        overrides: When supplied, the loader-resolved ``EntityOverrides``
            are scanned against every .md file's text during the wikilink
            pass. Match counts populate ``report.override_coverage`` and are
            written to ``coverage_path`` (sidecar JSON) so curators can spot
            dead allowlist entries (#263).
        coverage_path: Sidecar JSON destination for the coverage report.
            Defaults to ``${KAIRIX_DATA_DIR}/entity-override-coverage.json``
            when ``overrides`` is supplied but the path is omitted.

    Returns:
        CrawlReport describing nodes found, upserted, optionally reset, and
        override coverage when an overrides file was provided.
    """
    root = Path(document_root)
    report = CrawlReport(document_root=str(root), dry_run=dry_run)

    if reset:
        _apply_reset(neo4j_client, report, dry_run=dry_run)

    if not root.exists():
        report.errors.append(f"document_root does not exist: {root}")
        return report

    counter, allowlist_filter = _build_coverage_tracker(overrides)
    started_at = datetime.now(timezone.utc).isoformat()

    orgs = crawl_organisations(root, report, neo4j_client, dry_run)
    persons = crawl_persons(root, orgs, report, neo4j_client, dry_run)
    crawl_outcomes(root, report, neo4j_client, dry_run)
    crawl_wikilink_edges(
        root,
        orgs,
        persons,
        report,
        neo4j_client,
        dry_run,
        allowlist_filter=allowlist_filter,
    )

    if overrides is not None and counter is not None:
        _finalise_override_coverage(
            overrides=overrides,
            counter=counter,
            started_at=started_at,
            coverage_path=coverage_path,
            report=report,
        )

    return report


def _apply_reset(neo4j_client: Any, report: CrawlReport, *, dry_run: bool) -> None:
    """Run ``DETACH DELETE`` against the live graph and record counts on ``report``.

    Dry-run mode records zero counts without invoking the destructive
    Cypher path — the CLI surfaces this to the operator so the intent is
    visible even when nothing is written.
    """
    if dry_run:
        report.reset_nodes_deleted = 0
        report.reset_relationships_deleted = 0
        return
    nodes, rels = neo4j_client.reset_graph()
    report.reset_nodes_deleted = int(nodes)
    report.reset_relationships_deleted = int(rels)


def _build_coverage_tracker(
    overrides: EntityOverrides | None,
) -> tuple[OverrideMatchCounter | None, KnownEntityAllowlist | None]:
    """Wire a shared ``OverrideMatchCounter`` into a filter, or return ``(None, None)``.

    Returning a tuple keeps the crawl call-site explicit: when no overrides
    are supplied we don't pay the per-file regex cost in the wikilink pass.
    """
    if overrides is None or not overrides.allowlist:
        return None, None
    counter = OverrideMatchCounter()
    allowlist_filter = KnownEntityAllowlist(overrides.allowlist, match_counter=counter)
    return counter, allowlist_filter


def _finalise_override_coverage(
    *,
    overrides: EntityOverrides,
    counter: OverrideMatchCounter,
    started_at: str,
    coverage_path: Path | None,
    report: CrawlReport,
) -> None:
    """Build the coverage summary, attach it to the report, and write the sidecar JSON."""
    override_texts = sorted({str(entry.get("text", "")) for entry in overrides.allowlist if entry.get("text")})
    match_counts = {text: counter.get(text) for text in override_texts if counter.get(text) > 0}
    never_matched = sorted(text for text in override_texts if counter.get(text) == 0)
    coverage = OverrideCoverage(
        crawl_started_at=started_at,
        total_overrides=len(override_texts),
        matched=len(match_counts),
        never_matched=never_matched,
        match_counts=match_counts,
    )
    report.override_coverage = coverage

    destination = coverage_path
    if destination is None:
        from kairix.paths import data_dir

        destination = data_dir() / "entity-override-coverage.json"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(
                {
                    "crawl_started_at": coverage.crawl_started_at,
                    "total_overrides": coverage.total_overrides,
                    "matched": coverage.matched,
                    "never_matched": coverage.never_matched,
                    "match_counts": coverage.match_counts,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        report.override_coverage_path = str(destination)
    except OSError as exc:
        logger.warning("override-coverage: cannot write %s — %s", destination, exc)
        report.override_coverage_path = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def parse_frontmatter(path: Path) -> dict[str, Any]:
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

        lenient = re.match(
            r"\A---\s*\n(.*?)\n---", text, re.DOTALL
        )  # NOSONAR — non-greedy `.*?` bounded by `\n---`; file-bounded frontmatter input.
        if not lenient:
            return {}
        block = lenient.group(1)
    else:
        # Re-extract the raw YAML block for full yaml.safe_load parsing
        import re

        match = re.match(
            r"\A---\s*\n(.*?)\n---", text, re.DOTALL
        )  # NOSONAR — same bounded-input rationale as the lenient match above.
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


def as_list(value: Any) -> list[str]:
    """Normalise a scalar, list, or None frontmatter field to list[str]."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _resolve_org_id(org_raw: str, orgs: dict[str, OrganisationNode]) -> str:
    """Find an org id by name or partial match in the discovered orgs dict."""
    slug = slugify(org_raw)
    if slug in orgs:
        return str(orgs[slug].id)
    # Partial match: org_raw is a substring of a known org name
    org_raw_lower = org_raw.lower()
    for key, node in orgs.items():
        if org_raw_lower in node.name.lower() or org_raw_lower in key:
            return str(node.id)
    return ""
