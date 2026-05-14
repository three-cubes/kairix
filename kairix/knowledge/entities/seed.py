"""Entity seeding — discover entities from indexed documents.

Scans document titles and content for potential entities (organisations,
people, frameworks, technologies). Uses regex patterns as the default
discovery method; optionally uses spaCy NER when available.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from kairix.utils import display_name, slugify

logger = logging.getLogger(__name__)

# Patterns that suggest entity types from document paths/titles
_ORG_PATTERNS = re.compile(
    r"(?:^|/)(?:clients?|organisations?|companies|vendors?|partners?)/([^/]+?)(?:\.md)?$",
    re.IGNORECASE,
)
_PERSON_PATTERNS = re.compile(
    r"(?:^|/)(?:people|persons?|contacts?|team|stakeholders?)/([^/]+?)(?:\.md)?$",
    re.IGNORECASE,
)
_FRAMEWORK_PATTERNS = re.compile(
    r"(?:^|/)(?:frameworks?|methods?|methodologies|patterns?)/([^/]+?)(?:\.md)?$",
    re.IGNORECASE,
)


@dataclass
class EntityCandidate:
    """A potential entity discovered from document analysis."""

    name: str
    entity_type: str  # Organisation, Person, Framework, Technology, Concept
    confidence: float  # 0.0 to 1.0
    source_docs: list[str] = field(default_factory=list)
    suggested_id: str = ""

    def __post_init__(self) -> None:
        if not self.suggested_id:
            self.suggested_id = slugify(self.name)


def scan_for_entities(db: sqlite3.Connection, limit: int = 500) -> list[EntityCandidate]:
    """Discover entity candidates from indexed documents.

    Uses document paths and titles to infer entity types via regex.
    Deduplicates by normalised name.
    """
    candidates: dict[str, EntityCandidate] = {}

    rows = db.execute(
        "SELECT path, title FROM documents WHERE active = 1 ORDER BY path LIMIT ?",
        (limit * 5,),  # over-fetch since many won't match
    ).fetchall()

    for path, _title in rows:
        _check_path_patterns(path, candidates)

    # Deduplicate and sort by confidence
    result = sorted(candidates.values(), key=lambda c: (-c.confidence, c.name))
    return result[:limit]


def _check_path_patterns(path: str, candidates: dict[str, EntityCandidate]) -> None:
    """Check if a document path matches entity folder patterns."""
    for pattern, entity_type, confidence in [
        (_ORG_PATTERNS, "Organisation", 0.85),
        (_PERSON_PATTERNS, "Person", 0.85),
        (_FRAMEWORK_PATTERNS, "Framework", 0.75),
    ]:
        m = pattern.search(path)
        if m:
            name = _title_case(m.group(1))
            key = name.lower()
            if key in candidates:
                candidates[key].source_docs.append(path)
            else:
                candidates[key] = EntityCandidate(
                    name=name,
                    entity_type=entity_type,
                    confidence=confidence,
                    source_docs=[path],
                )


def _title_case(s: str) -> str:
    """Title-case a name, preserving acronyms (all-caps words stay caps).

    Uses ``display_name`` as the base transformation and then restores any
    ALL-CAPS words from the original input.
    """
    base = display_name(s)
    # Restore acronyms: if the original word was ALL-CAPS (len>1), keep it
    original_words = s.replace("-", " ").replace("_", " ").split()
    base_words = base.split()
    result: list[str] = []
    for orig, titled in zip(original_words, base_words, strict=False):
        if orig.isupper() and len(orig) > 1:
            result.append(orig)
        else:
            result.append(titled)
    # If base has more/fewer words (shouldn't happen), append remainder
    result.extend(base_words[len(original_words) :])
    return " ".join(result)


def seed_graph(client: Any, candidates: list[EntityCandidate]) -> int:
    """Upsert confirmed entity candidates into Neo4j.

    Returns the number of successfully upserted entities.
    """
    if not getattr(client, "available", False):
        logger.warning("seed_graph: Neo4j not available — skipping")
        return 0

    count = 0
    for c in candidates:
        props: dict[str, Any] = {"name": c.name}
        if c.source_docs:
            props["source_docs"] = c.source_docs[:5]  # cap for Neo4j property size

        ok = client.upsert_node(c.entity_type, c.suggested_id, props)
        if ok:
            count += 1
        else:
            logger.warning("seed_graph: failed to upsert %s:%s", c.entity_type, c.suggested_id)

    logger.info("seed_graph: upserted %d/%d entities", count, len(candidates))
    return count
