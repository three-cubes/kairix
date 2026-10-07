"""Rule-based entity extraction from the reference library.

Scans normalised markdown files and extracts entities (people,
organisations, concepts, frameworks, technologies, publications) and
relationships using high-precision regex/pattern matching.  No NLP
libraries are used — LLM extraction is a separate future phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kairix.knowledge.reflib.frontmatter import extract_existing_frontmatter

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RawEntity:
    """An entity extracted before dedup/resolution."""

    name: str
    entity_type: str  # Organisation, Person, Concept, Framework, Technology, Publication, Document
    description: str = ""
    source_docs: list[str] = field(default_factory=list)
    domain: str = ""
    domains: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass
class RawRelationship:
    """A directed relationship extracted from a document."""

    from_name: str
    from_type: str
    to_name: str
    to_type: str
    kind: str  # EdgeKind value as string
    source_doc: str = ""
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Domain values — referenced by collection map AND every seed entity below.
# F17 — extracted so a domain-vocabulary rename hits a single edit site.
# ---------------------------------------------------------------------------

_DOMAIN_PHILOSOPHY = "philosophy"
_DOMAIN_SOFTWARE_ENGINEERING = "software-engineering"
_DOMAIN_ARTIFICIAL_INTELLIGENCE = "artificial-intelligence"
_DOMAIN_TECHNOLOGY = "technology"
_DOMAIN_INDUSTRY_STANDARDS = "industry-standards"
_DOMAIN_DATA_SCIENCE = "data-science"
_DOMAIN_CYBERSECURITY = "cybersecurity"
_DOMAIN_PRODUCT_MANAGEMENT = "product-management"
_DOMAIN_PERSONAL_DEVELOPMENT = "personal-development"
_DOMAIN_FOUNDATIONS = "foundations"

# Entity-type label repeated across seed dispatch + relationship builders.
_TYPE_ORGANISATION = "Organisation"

# ---------------------------------------------------------------------------
# Domain mapping — collection name to human-readable domain
# ---------------------------------------------------------------------------

_COLLECTION_DOMAIN: dict[str, str] = {
    "agentic-ai": _DOMAIN_ARTIFICIAL_INTELLIGENCE,
    "data-and-analysis": _DOMAIN_DATA_SCIENCE,
    "engineering": _DOMAIN_SOFTWARE_ENGINEERING,
    "security": _DOMAIN_CYBERSECURITY,
    "operating-models": "operating-models",
    "product-and-design": _DOMAIN_PRODUCT_MANAGEMENT,
    "leadership-and-culture": "leadership",
    "economics-and-strategy": "strategy",
    "personal-effectiveness": _DOMAIN_PERSONAL_DEVELOPMENT,
    "health-and-fitness": "health",
    _DOMAIN_PHILOSOPHY: _DOMAIN_PHILOSOPHY,
    "family-and-education": "education",
    _DOMAIN_INDUSTRY_STANDARDS: _DOMAIN_INDUSTRY_STANDARDS,
    _DOMAIN_FOUNDATIONS: _DOMAIN_FOUNDATIONS,
}

# ---------------------------------------------------------------------------
# Known seed entities (high-value, unambiguous)
# ---------------------------------------------------------------------------

_SEED_PEOPLE: dict[str, dict[str, Any]] = {
    "Marcus Aurelius": {
        "domain": _DOMAIN_PHILOSOPHY,
        "aliases": ["Marcus Aurelius Antoninus"],
    },
    "Epictetus": {"domain": _DOMAIN_PHILOSOPHY},
    "Seneca": {"domain": _DOMAIN_PHILOSOPHY, "aliases": ["Lucius Annaeus Seneca"]},
    "Sun Tzu": {"domain": _DOMAIN_PHILOSOPHY, "aliases": ["Sunzi"]},
    "Lao-Tse": {"domain": _DOMAIN_PHILOSOPHY, "aliases": ["Laozi", "Lao Tzu", "Lao-Tzu"]},
    "Patanjali": {"domain": _DOMAIN_PHILOSOPHY},
    "Confucius": {"domain": _DOMAIN_PHILOSOPHY, "aliases": ["Kong Qiu", "Kongzi"]},
    "Aristotle": {"domain": _DOMAIN_PHILOSOPHY},
    "Plato": {"domain": _DOMAIN_PHILOSOPHY},
    "Miyamoto Musashi": {"domain": _DOMAIN_PHILOSOPHY, "aliases": ["Musashi"]},
    "John Dewey": {"domain": "education"},
    "Maria Montessori": {"domain": "education"},
}

_SEED_ORGANISATIONS: dict[str, dict[str, Any]] = {
    "OWASP": {
        "domain": _DOMAIN_CYBERSECURITY,
        "aliases": ["Open Web Application Security Project"],
    },
    "CNCF": {
        "domain": _DOMAIN_SOFTWARE_ENGINEERING,
        "aliases": ["Cloud Native Computing Foundation"],
    },
    "Google": {"domain": _DOMAIN_TECHNOLOGY},
    "Microsoft": {"domain": _DOMAIN_TECHNOLOGY},
    "Mozilla": {"domain": _DOMAIN_TECHNOLOGY, "aliases": ["Mozilla Foundation"]},
    "dbt Labs": {"domain": _DOMAIN_DATA_SCIENCE, "aliases": ["dbt"]},
    "PostHog": {"domain": _DOMAIN_DATA_SCIENCE},
    "OpenAI": {"domain": _DOMAIN_ARTIFICIAL_INTELLIGENCE},
    "EleutherAI": {"domain": _DOMAIN_ARTIFICIAL_INTELLIGENCE},
    "Stanford": {
        "domain": _DOMAIN_ARTIFICIAL_INTELLIGENCE,
        "aliases": ["Stanford University"],
    },
    "18F": {"domain": _DOMAIN_SOFTWARE_ENGINEERING},
    "USDS": {
        "domain": _DOMAIN_PRODUCT_MANAGEMENT,
        "aliases": ["United States Digital Service"],
    },
    "GDS": {
        "domain": _DOMAIN_SOFTWARE_ENGINEERING,
        "aliases": ["Government Digital Service"],
    },
    "Meta": {"domain": _DOMAIN_TECHNOLOGY, "aliases": ["Facebook"]},
    "BIAN": {
        "domain": _DOMAIN_INDUSTRY_STANDARDS,
        "aliases": ["Banking Industry Architecture Network"],
    },
    "MOSIP": {"domain": _DOMAIN_INDUSTRY_STANDARDS},
    "Dropbox": {"domain": _DOMAIN_TECHNOLOGY},
    "DAIR.AI": {"domain": _DOMAIN_ARTIFICIAL_INTELLIGENCE},
    "Panaversity": {"domain": _DOMAIN_ARTIFICIAL_INTELLIGENCE},
    "SuttaCentral": {"domain": _DOMAIN_PHILOSOPHY},
    "Neuromatch": {"domain": _DOMAIN_FOUNDATIONS, "aliases": ["Neuromatch Academy"]},
    "GrowthBook": {"domain": _DOMAIN_DATA_SCIENCE},
    "PyMC Labs": {"domain": "strategy", "aliases": ["PyMC"]},
    "Gong": {"domain": _DOMAIN_PRODUCT_MANAGEMENT},
}

_SEED_FRAMEWORKS: dict[str, dict[str, Any]] = {
    "Twelve-Factor App": {
        "domain": _DOMAIN_SOFTWARE_ENGINEERING,
        "aliases": ["12-Factor", "12 Factor App"],
    },
    "SLSA": {
        "domain": _DOMAIN_CYBERSECURITY,
        "aliases": ["Supply-chain Levels for Software Artifacts"],
    },
    "CycloneDX": {"domain": _DOMAIN_CYBERSECURITY},
    "arc42": {"domain": _DOMAIN_SOFTWARE_ENGINEERING},
    "HELM": {
        "domain": _DOMAIN_ARTIFICIAL_INTELLIGENCE,
        "aliases": ["Holistic Evaluation of Language Models"],
    },
    "FSRS": {
        "domain": _DOMAIN_PERSONAL_DEVELOPMENT,
        "aliases": ["Free Spaced Repetition Scheduler"],
    },
    "OKR": {
        "domain": _DOMAIN_PERSONAL_DEVELOPMENT,
        "aliases": ["Objectives and Key Results"],
    },
    "Business Model Canvas": {"domain": "strategy"},
    "OpenTelemetry": {"domain": _DOMAIN_SOFTWARE_ENGINEERING, "aliases": ["OTel"]},
    "ADR": {
        "domain": _DOMAIN_SOFTWARE_ENGINEERING,
        "aliases": ["Architecture Decision Record", "Architecture Decision Records"],
    },
    "MADR": {
        "domain": _DOMAIN_SOFTWARE_ENGINEERING,
        "aliases": ["Markdown ADR", "Markdown Architecture Decision Record"],
    },
}

_SEED_TECHNOLOGIES: dict[str, dict[str, Any]] = {
    "AutoGen": {"domain": _DOMAIN_ARTIFICIAL_INTELLIGENCE},
    "Robyn": {"domain": "strategy", "aliases": ["Meta Robyn"]},
    "Meridian": {"domain": "strategy", "aliases": ["Google Meridian"]},
    "PyMC-Marketing": {"domain": "strategy"},
    "Neo4j": {"domain": _DOMAIN_TECHNOLOGY},
}

_SEED_PUBLICATIONS: dict[str, dict[str, Any]] = {
    "Tao Te Ching": {
        "domain": _DOMAIN_PHILOSOPHY,
        "aliases": ["Tao Teh King", "Dao De Jing"],
    },
    "Art of War": {"domain": _DOMAIN_PHILOSOPHY, "aliases": ["The Art of War"]},
    "Bhagavad Gita": {"domain": _DOMAIN_PHILOSOPHY},
    "Yoga Sutras": {"domain": _DOMAIN_PHILOSOPHY, "aliases": ["Yoga Sutras of Patanjali"]},
    "Bushido": {"domain": _DOMAIN_PHILOSOPHY, "aliases": ["Bushido: The Soul of Japan"]},
    "Chuang Tzu": {"domain": _DOMAIN_PHILOSOPHY, "aliases": ["Zhuangzi"]},
    "Meditations": {"domain": _DOMAIN_PHILOSOPHY},
    "Discourses": {"domain": _DOMAIN_PHILOSOPHY, "aliases": ["Discourses of Epictetus"]},
}

# ---------------------------------------------------------------------------
# Patterns for rule-based extraction from headings
# ---------------------------------------------------------------------------

# Matches "X Framework", "X Method", "X Model", "X Pattern", "X Methodology"
_FRAMEWORK_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)*)"
    r"\s+(Framework|Method|Model|Pattern|Methodology|Approach|Principle|Architecture)\b"
)

# Matches title-case proper nouns (2-5 words starting with capitals)
_PROPER_NOUN_PATTERN = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4})\b"
)  # NOSONAR — bounded `{1,4}` repetition with word-boundary anchors; backtracking linear.

# Heading extraction. Only same-line spaces may follow the ``#`` run (an empty
# heading must not borrow the next line as its title), and the title starts at
# its first non-space character — linear-time, no overlapping quantifiers.
_HEADING_RE = re.compile(r"^(#{1,3})[^\S\n]+(\S.*)$", re.MULTILINE)

# Common words that are NOT entities when title-cased in headings
_STOP_TITLE_WORDS = frozenset(
    {
        "The",
        "And",
        "For",
        "With",
        "From",
        "Into",
        "About",
        "This",
        "That",
        "These",
        "Those",
        "What",
        "When",
        "Where",
        "How",
        "Why",
        "Getting Started",
        "Quick Start",
        "Table Of Contents",
        "Next Steps",
        "See Also",
        "Further Reading",
        "More Information",
        "Best Practices",
        "Key Takeaways",
        "Key Points",
        "Common Mistakes",
        "Common Patterns",
        "Related Topics",
        "Related Resources",
        "In This",
        "In The",
        "Overview",
        "Introduction",
        "Summary",
        "Conclusion",
        "References",
        "Appendix",
        "Prerequisites",
        "Requirements",
        "Installation",
        "Configuration",
        "Usage",
        "Examples",
        "Example",
        "Setup",
        "Final Thoughts",
        "Part One",
        "Part Two",
        "Part Three",
    }
)


def is_stop_heading(text: str) -> bool:
    """Return True if the heading text is generic/non-entity."""
    stripped = text.strip().rstrip(".")
    if stripped in _STOP_TITLE_WORDS:
        return True
    # Too short or too long
    if len(stripped) < 3 or len(stripped) > 80:
        return True
    # All lowercase (not a proper noun)
    if stripped[0].islower():
        return True
    return False


# ---------------------------------------------------------------------------
# Core extraction logic
# ---------------------------------------------------------------------------


def domain_from_path(rel_path: str) -> str:
    """Infer domain from the collection (first path component)."""
    parts = rel_path.split("/")
    if parts:
        return _COLLECTION_DOMAIN.get(parts[0], parts[0])
    return "unknown"


def extract_from_frontmatter(
    fm: dict[str, str],
    rel_path: str,
    domain: str,
    entities: list[RawEntity],
    relationships: list[RawRelationship],
) -> None:
    """Extract entities and relationships from parsed frontmatter."""
    title = fm.get("title", "")
    source = fm.get("source", "")

    # The document itself is a Document entity
    if title:
        entities.append(
            RawEntity(
                name=title,
                entity_type="Document",
                description=f"Reference document: {title}",
                source_docs=[rel_path],
                domain=domain,
                domains=[domain],
                confidence=1.0,
            )
        )

    # Source name is an Organisation entity
    if source:
        entities.append(
            RawEntity(
                name=source,
                entity_type=_TYPE_ORGANISATION,
                description=f"Source organisation: {source}",
                source_docs=[rel_path],
                domain=domain,
                domains=[domain],
                confidence=0.9,
            )
        )
        # AUTHORED_BY relationship
        if title:
            relationships.append(
                RawRelationship(
                    from_name=title,
                    from_type="Document",
                    to_name=source,
                    to_type=_TYPE_ORGANISATION,
                    kind="AUTHORED_BY",
                    source_doc=rel_path,
                    confidence=0.9,
                )
            )

    # DESCRIBED_IN — the document describes content in its domain
    if title:
        relationships.append(
            RawRelationship(
                from_name=title,
                from_type="Document",
                to_name=domain,
                to_type="Concept",
                kind="DESCRIBED_IN",
                source_doc=rel_path,
                confidence=0.7,
            )
        )

    # Detect framework-like titles
    if title:
        for suffix in (
            "Framework",
            "Method",
            "Model",
            "Pattern",
            "Methodology",
            "Architecture",
            "Playbook",
            "Guide",
            "Specification",
        ):
            if suffix in title:
                entities.append(
                    RawEntity(
                        name=title,
                        entity_type="Framework",
                        description=f"{suffix}: {title}",
                        source_docs=[rel_path],
                        domain=domain,
                        domains=[domain],
                        confidence=0.85,
                    )
                )
                break


def extract_from_headings(
    body: str,
    rel_path: str,
    domain: str,
    parent_title: str,
    entities: list[RawEntity],
    relationships: list[RawRelationship],
) -> None:
    """Extract entities and relationships from markdown headings."""
    heading_stack: list[tuple[int, str]] = []  # (level, text)

    for match in _HEADING_RE.finditer(body):
        level = len(match.group(1))
        text = match.group(2).strip()
        # Strip markdown links
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[`*_]", "", text).strip()

        if is_stop_heading(text):
            continue

        # Update heading stack for hierarchy
        heading_stack = [(lvl, t) for lvl, t in heading_stack if lvl < level]
        heading_stack.append((level, text))

        # Check for framework patterns in headings
        fm_match = _FRAMEWORK_PATTERN.search(text)
        if fm_match:
            fw_name = fm_match.group(0)
            entities.append(
                RawEntity(
                    name=fw_name,
                    entity_type="Framework",
                    description="Framework/method mentioned in heading",
                    source_docs=[rel_path],
                    domain=domain,
                    domains=[domain],
                    confidence=0.7,
                )
            )

        # TEACHES relationship: only from h2 headings (reduces noise)
        if level <= 2 and parent_title:
            relationships.append(
                RawRelationship(
                    from_name=parent_title,
                    from_type="Document",
                    to_name=text,
                    to_type="Concept",
                    kind="TEACHES",
                    source_doc=rel_path,
                    confidence=0.6,
                )
            )

        # PART_OF from sub-headings (h2 under h1, h3 under h2)
        if len(heading_stack) >= 2:
            parent_heading = heading_stack[-2][1]
            relationships.append(
                RawRelationship(
                    from_name=text,
                    from_type="Concept",
                    to_name=parent_heading,
                    to_type="Concept",
                    kind="PART_OF",
                    source_doc=rel_path,
                    confidence=0.5,
                )
            )


# Pre-build a combined lookup: name -> (entity_type, description_prefix, info_dict)
_ALL_SEEDS: dict[str, tuple[str, str, dict[str, Any]]] = {}
for _n, _i in _SEED_PEOPLE.items():
    _ALL_SEEDS[_n] = ("Person", "Historical/notable person", _i)
for _n, _i in _SEED_ORGANISATIONS.items():
    _ALL_SEEDS[_n] = (_TYPE_ORGANISATION, _TYPE_ORGANISATION, _i)
for _n, _i in _SEED_FRAMEWORKS.items():
    _ALL_SEEDS[_n] = ("Framework", "Framework/standard", _i)
for _n, _i in _SEED_TECHNOLOGIES.items():
    _ALL_SEEDS[_n] = ("Technology", "Technology/tool", _i)
for _n, _i in _SEED_PUBLICATIONS.items():
    _ALL_SEEDS[_n] = ("Publication", "Publication/text", _i)

# Build a single regex that matches any seed name (longest first to avoid partial)
_SEED_NAMES_SORTED = sorted(_ALL_SEEDS.keys(), key=len, reverse=True)
_SEED_RE = re.compile("|".join(re.escape(n) for n in _SEED_NAMES_SORTED))


def extract_seed_entities(
    text: str,
    rel_path: str,
    domain: str,
    entities: list[RawEntity],
    _relationships: list[RawRelationship],
) -> None:
    """Check for seed entities in document text using a single compiled regex."""
    found: set[str] = set()
    for match in _SEED_RE.finditer(text):
        found.add(match.group(0))

    for name in found:
        etype, desc, info = _ALL_SEEDS[name]
        entities.append(
            RawEntity(
                name=name,
                entity_type=etype,
                description=desc,
                source_docs=[rel_path],
                domain=info.get("domain", domain),
                domains=[info.get("domain", domain), domain],
                aliases=list(info.get("aliases", [])),
                confidence=0.95,
            )
        )


def _process_file(
    file_path: Path,
    reflib_root: Path,
    entities: list[RawEntity],
    relationships: list[RawRelationship],
) -> None:
    """Process a single markdown file for entity extraction."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    rel_path = str(file_path.relative_to(reflib_root))
    domain = domain_from_path(rel_path)

    fm, body = extract_existing_frontmatter(text)

    if fm:
        extract_from_frontmatter(fm, rel_path, domain, entities, relationships)

    parent_title = (fm or {}).get("title", "")
    extract_from_headings(body, rel_path, domain, parent_title, entities, relationships)
    extract_seed_entities(text, rel_path, domain, entities, relationships)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_reference_library(
    reflib_root: Path,
) -> tuple[list[RawEntity], list[RawRelationship]]:
    """Scan all markdown files in the reference library and extract entities.

    Args:
        reflib_root: Root directory of the normalised reference library.

    Returns:
        Tuple of (entities, relationships) extracted from the library.
    """
    entities: list[RawEntity] = []
    relationships: list[RawRelationship] = []

    md_files = sorted(reflib_root.rglob("*.md"))

    for file_path in md_files:
        # Skip catalogue/licence files at root
        if file_path.parent == reflib_root:
            continue
        _process_file(file_path, reflib_root, entities, relationships)

    return entities, relationships
