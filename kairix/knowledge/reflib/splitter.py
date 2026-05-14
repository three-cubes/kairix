"""File splitting and merging for reference library normalisation.

Splits files over MAX_FILE_SIZE at heading boundaries.
Discards or merges files under MIN_FILE_SIZE.
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_FILE_SIZE: int = 50_000  # 50KB
MIN_FILE_SIZE: int = 500  # 500 bytes

# Match h1 and h2 headings
_HEADING_RE = re.compile(
    r"^(#{1,2})\s+(.+)$", re.MULTILINE
)  # NOSONAR — bounded `{1,2}` repetition; line-anchored via re.MULTILINE; backtracking is linear in line length.


def needs_split(text: str, max_size: int = MAX_FILE_SIZE) -> bool:
    """Check if a file exceeds the size threshold."""
    return len(text.encode("utf-8")) > max_size


def is_too_small(text: str, min_size: int = MIN_FILE_SIZE) -> bool:
    """Check if a file is below the minimum size threshold."""
    stripped = text.strip()
    if not stripped:
        return True
    return len(stripped.encode("utf-8")) < min_size


def _collect_sections(text: str, headings: list[re.Match[str]]) -> list[tuple[str, str]]:
    """Extract (slug, content) sections from heading match positions."""
    sections: list[tuple[str, str]] = []
    for i, match in enumerate(headings):
        start = match.start()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        section_text = text[start:end].strip()

        if section_text:
            heading_text = match.group(2).strip()
            section_slug = _heading_slug(heading_text)
            sections.append((section_slug, section_text))

    # Include any preamble before the first heading
    if headings[0].start() > 0:
        preamble = text[: headings[0].start()].strip()
        if preamble and not is_too_small(preamble):
            sections.insert(0, ("preamble", preamble))

    return sections


def split_at_headings(
    text: str,
    stem: str,
    max_size: int = MAX_FILE_SIZE,
) -> list[tuple[str, str]]:
    """Split text at heading boundaries into smaller chunks.

    Args:
        text: The full document text (after frontmatter extraction).
        stem: The filename stem for generating part names.
        max_size: Maximum size per chunk in bytes.

    Returns:
        List of (filename_stem, content) tuples.
        If no split is needed, returns [(stem, text)].
    """
    if not needs_split(text, max_size):
        return [(stem, text)]

    # Find all h1/h2 heading positions
    headings = list(_HEADING_RE.finditer(text))

    if not headings:
        # No headings to split on — return as-is
        return [(stem, text)]

    sections = _collect_sections(text, headings)

    if not sections:
        return [(stem, text)]

    # If splitting produced only one section, return it with original stem
    if len(sections) == 1:
        return [(stem, sections[0][1])]

    # Number the parts
    result: list[tuple[str, str]] = []
    for i, (section_slug, content) in enumerate(sections, 1):
        part_stem = f"{stem}-part-{i:02d}-{section_slug}"
        # Truncate long stems
        if len(part_stem) > 100:
            part_stem = part_stem[:100]
        result.append((part_stem, content))

    return result


def _heading_slug(text: str) -> str:
    """Convert a markdown heading to a URL-safe slug (max 60 chars).

    This differs from ``kairix.utils.slugify`` in two ways:
    1. Caps output at 60 characters (headings can be very long).
    2. Uses a simpler regex that only keeps ``[a-z0-9]``, spaces, and
       hyphens — ``slugify`` also strips Unicode and applies different
       whitespace rules suited to document *titles* rather than section
       headings.

    Kept separate because section-heading slugs feed into chunk stem
    names (e.g. ``doc-part-03-design-decisions``) where a shorter,
    ASCII-only slug avoids filesystem issues.
    """
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")[:60]


def to_kebab_case(name: str) -> str:
    """Convert a filename to kebab-case.

    Lowercases, replaces spaces/underscores with hyphens,
    removes special characters, collapses multiple hyphens.
    """
    stem = Path(name).stem
    suffix = Path(name).suffix or ".md"

    # Insert hyphens at camelCase boundaries
    result = re.sub(r"([a-z])([A-Z])", r"\1-\2", stem)
    result = result.lower()
    # Replace spaces, underscores, dots with hyphens
    result = re.sub(r"[\s_\.]+", "-", result)
    # Remove special characters except hyphens
    result = re.sub(r"[^a-z0-9-]", "", result)
    # Collapse multiple hyphens
    result = re.sub(r"-{2,}", "-", result)
    result = result.strip("-")

    return f"{result}{suffix}" if result else f"document{suffix}"
