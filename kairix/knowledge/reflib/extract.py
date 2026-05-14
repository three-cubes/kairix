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
# Domain mapping — collection name to human-readable domain
# ---------------------------------------------------------------------------

_COLLECTION_DOMAIN: dict[str, str] = {
    "agentic-ai": "artificial-intelligence",
    "data-and-analysis": "data-science",
    "engineering": "software-engineering",
    "security": "cybersecurity",
    "operating-models": "operating-models",
    "product-and-design": "product-management",
    "leadership-and-culture": "leadership",
    "economics-and-strategy": "strategy",
    "personal-effectiveness": "personal-development",
    "health-and-fitness": "health",
    "philosophy": "philosophy",
    "family-and-education": "education",
    "industry-standards": "industry-standards",
    "foundations": "foundations",
}

# ---------------------------------------------------------------------------
# Known seed entities (high-value, unambiguous)
# ---------------------------------------------------------------------------

_SEED_PEOPLE: dict[str, dict[str, Any]] = {
    "Marcus Aurelius": {
        "domain": "philosophy",
        "aliases": ["Marcus Aurelius Antoninus"],
    },
    "Epictetus": {"domain": "philosophy"},
    "Seneca": {"domain": "philosophy", "aliases": ["Lucius Annaeus Seneca"]},
    "Sun Tzu": {"domain": "philosophy", "aliases": ["Sunzi"]},
    "Lao-Tse": {"domain": "philosophy", "aliases": ["Laozi", "Lao Tzu", "Lao-Tzu"]},
    "Patanjali": {"domain": "philosophy"},
    "Confucius": {"domain": "philosophy", "aliases": ["Kong Qiu", "Kongzi"]},
    "Aristotle": {"domain": "philosophy"},
    "Plato": {"domain": "philosophy"},
    "Miyamoto Musashi": {"domain": "philosophy", "aliases": ["Musashi"]},
    "John Dewey": {"domain": "education"},
    "Maria Montessori": {"domain": "education"},
}

_SEED_ORGANISATIONS: dict[str, dict[str, Any]] = {
    "OWASP": {
        "domain": "cybersecurity",
        "aliases": ["Open Web Application Security Project"],
    },
    "CNCF": {
        "domain": "software-engineering",
        "aliases": ["Cloud Native Computing Foundation"],
    },
    "Google": {"domain": "technology"},
    "Microsoft": {"domain": "technology"},
    "Mozilla": {"domain": "technology", "aliases": ["Mozilla Foundation"]},
    "dbt Labs": {"domain": "data-science", "aliases": ["dbt"]},
    "PostHog": {"domain": "data-science"},
    "OpenAI": {"domain": "artificial-intelligence"},
    "EleutherAI": {"domain": "artificial-intelligence"},
    "Stanford": {
        "domain": "artificial-intelligence",
        "aliases": ["Stanford University"],
    },
    "18F": {"domain": "software-engineering"},
    "USDS": {
        "domain": "product-management",
        "aliases": ["United States Digital Service"],
    },
    "GDS": {
        "domain": "software-engineering",
        "aliases": ["Government Digital Service"],
    },
    "Meta": {"domain": "technology", "aliases": ["Facebook"]},
    "BIAN": {
        "domain": "industry-standards",
        "aliases": ["Banking Industry Architecture Network"],
    },
    "MOSIP": {"domain": "industry-standards"},
    "Dropbox": {"domain": "technology"},
    "DAIR.AI": {"domain": "artificial-intelligence"},
    "Panaversity": {"domain": "artificial-intelligence"},
    "SuttaCentral": {"domain": "philosophy"},
    "Neuromatch": {"domain": "foundations", "aliases": ["Neuromatch Academy"]},
    "GrowthBook": {"domain": "data-science"},
    "PyMC Labs": {"domain": "strategy", "aliases": ["PyMC"]},
    "Gong": {"domain": "product-management"},
}

_SEED_FRAMEWORKS: dict[str, dict[str, Any]] = {
    "Twelve-Factor App": {
        "domain": "software-engineering",
        "aliases": ["12-Factor", "12 Factor App"],
    },
    "SLSA": {
        "domain": "cybersecurity",
        "aliases": ["Supply-chain Levels for Software Artifacts"],
    },
    "CycloneDX": {"domain": "cybersecurity"},
    "arc42": {"domain": "software-engineering"},
    "HELM": {
        "domain": "artificial-intelligence",
        "aliases": ["Holistic Evaluation of Language Models"],
    },
    "FSRS": {
        "domain": "personal-development",
        "aliases": ["Free Spaced Repetition Scheduler"],
    },
    "OKR": {
        "domain": "personal-development",
        "aliases": ["Objectives and Key Results"],
    },
    "Business Model Canvas": {"domain": "strategy"},
    "OpenTelemetry": {"domain": "software-engineering", "aliases": ["OTel"]},
    "ADR": {
        "domain": "software-engineering",
        "aliases": ["Architecture Decision Record", "Architecture Decision Records"],
    },
    "MADR": {
        "domain": "software-engineering",
        "aliases": ["Markdown ADR", "Markdown Architecture Decision Record"],
    },
}

_SEED_TECHNOLOGIES: dict[str, dict[str, Any]] = {
    "AutoGen": {"domain": "artificial-intelligence"},
    "Robyn": {"domain": "strategy", "aliases": ["Meta Robyn"]},
    "Meridian": {"domain": "strategy", "aliases": ["Google Meridian"]},
    "PyMC-Marketing": {"domain": "strategy"},
    "Neo4j": {"domain": "technology"},
}

_SEED_PUBLICATIONS: dict[str, dict[str, Any]] = {
    "Tao Te Ching": {
        "domain": "philosophy",
        "aliases": ["Tao Teh King", "Dao De Jing"],
    },
    "Art of War": {"domain": "philosophy", "aliases": ["The Art of War"]},
    "Bhagavad Gita": {"domain": "philosophy"},
    "Yoga Sutras": {"domain": "philosophy", "aliases": ["Yoga Sutras of Patanjali"]},
    "Bushido": {"domain": "philosophy", "aliases": ["Bushido: The Soul of Japan"]},
    "Chuang Tzu": {"domain": "philosophy", "aliases": ["Zhuangzi"]},
    "Meditations": {"domain": "philosophy"},
    "Discourses": {"domain": "philosophy", "aliases": ["Discourses of Epictetus"]},
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

# Heading extraction
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)

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
                entity_type="Organisation",
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
                    to_type="Organisation",
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
    _ALL_SEEDS[_n] = ("Organisation", "Organisation", _i)
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
    relationships: list[RawRelationship],
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
