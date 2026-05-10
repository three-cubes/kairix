"""YAML frontmatter generation and injection for reference library normalisation.

Generates standard frontmatter compatible with kairix/db/scanner.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from kairix.knowledge.reflib.sources import SourceDef
from kairix.text import extract_title, strip_frontmatter

# YAML frontmatter block — \A anchor ensures match only at string start
# NOSONAR: non-greedy `.*?` bounded by literal `\n---\s*\n`
# terminator; input is markdown frontmatter (file-bounded, not user request).
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Re-export for backwards compatibility
__all__ = [
    "Frontmatter",
    "build_frontmatter",
    "extract_existing_frontmatter",
    "extract_title",
    "inject_frontmatter",
    "render_frontmatter",
    "strip_frontmatter",
]


@dataclass
class Frontmatter:
    """Standard frontmatter fields for reference library documents."""

    title: str
    source: str
    source_url: str
    licence: str
    domain: str
    subdomain: str
    date_added: str


def extract_existing_frontmatter(text: str) -> tuple[dict[str, str] | None, str]:
    """Extract existing YAML frontmatter from text.

    Returns (parsed_dict, body_without_frontmatter).
    If no frontmatter found, returns (None, original_text).
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text

    fm_block = match.group(1)
    body = text[match.end() :]

    # Simple YAML key: value parsing (no nested structures)
    parsed: dict[str, str] = {}
    for line in fm_block.split("\n"):
        line = line.strip()
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and value:
                parsed[key] = value

    return parsed, body


def build_frontmatter(path: Path, source: SourceDef, text: str) -> Frontmatter:
    """Build frontmatter for a document.

    Args:
        path: Document file path.
        source: Source definition from registry.
        text: Document text content.
    """
    title = extract_title(text, path)

    return Frontmatter(
        title=title,
        source=source.name,
        source_url=source.source_url,
        licence=source.licence,
        domain=source.collection,
        subdomain=source.dir_name,
        date_added=date.today().isoformat(),
    )


def render_frontmatter(fm: Frontmatter) -> str:
    """Render frontmatter as a YAML block string."""
    # Escape title for YAML safety
    title = fm.title.replace('"', '\\"')

    lines = [
        "---",
        f'title: "{title}"',
        f"source: {fm.source}",
        f"source_url: {fm.source_url}",
        f"licence: {fm.licence}",
        f"domain: {fm.domain}",
        f"subdomain: {fm.subdomain}",
        f"date_added: {fm.date_added}",
        "---",
    ]
    return "\n".join(lines)


def inject_frontmatter(text: str, fm: Frontmatter) -> str:
    """Inject or replace frontmatter in document text.

    If the document already has frontmatter, it is replaced.
    If not, frontmatter is prepended.
    """
    _, body = extract_existing_frontmatter(text)
    header = render_frontmatter(fm)
    return f"{header}\n\n{body.lstrip()}"
