"""
Canonical text utilities for kairix.

All token-counting code should use these functions rather than
rolling local estimators. The word-count heuristic matches the
OpenAI tokeniser within 10 % for English prose.

Also contains pure-text helpers (frontmatter stripping, title extraction)
that are used across both knowledge/ and core/ layers.
"""

from __future__ import annotations

import re
from pathlib import Path

# Approximate characters per token (for byte-budget calculations)
APPROX_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count. Uses word count * 1.3 (matches OpenAI tokeniser within 10%)."""
    words = len(text.split())
    if words == 0:
        return 0
    return max(1, int(words * 1.3))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to approximately max_tokens."""
    words = text.split()
    target_words = int(max_tokens / 1.3)
    if len(words) <= target_words:
        return text
    return " ".join(words[:target_words]) + " ... [truncated]"


# ---------------------------------------------------------------------------
# Frontmatter helpers (pure text — no file I/O)
# ---------------------------------------------------------------------------

# YAML frontmatter block — \A anchor ensures match only at string start
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL
)  # NOSONAR — non-greedy `.*?` bounded by `\n---\s*\n`; file-bounded frontmatter input.

# Same pattern without capture group, for strip_frontmatter
_FRONTMATTER_STRIP_RE = re.compile(
    r"\A---\s*\n.*?\n---\s*\n", re.DOTALL
)  # NOSONAR — same rationale as _FRONTMATTER_RE: bounded input.

# First markdown heading — `(.+)` is anchored to a single line via re.MULTILINE
# so backtracking is bounded by line length.
_FIRST_HEADING_RE = re.compile(
    r"^#{1,3}\s+(.+)$", re.MULTILINE
)  # NOSONAR — single-line input via re.MULTILINE; no polynomial blowup.


def strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter block from the start of text.

    Uses \\A anchor to match only at string start (not mid-string with DOTALL).
    """
    return _FRONTMATTER_STRIP_RE.sub("", text, count=1)


def _title_from_frontmatter(fm_block: str) -> str | None:
    """Extract the ``title:`` value from a YAML frontmatter block, or None."""
    for line in fm_block.split("\n"):
        line = line.strip()
        if line.lower().startswith("title:") and ":" in line:
            _, _, value = line.partition(":")
            value = value.strip().strip("'\"")
            if value:
                return value
    return None


def _title_from_first_heading(body: str) -> str | None:
    """Find the first markdown ``#`` heading in body; strip markdown links."""
    heading_match = _FIRST_HEADING_RE.search(body)
    if not heading_match:
        return None
    title = heading_match.group(1).strip()
    title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title)
    return title if title and len(title) < 200 else None


def extract_title(text: str, path: Path) -> str:
    """Extract a document title using priority: frontmatter > heading > filename.

    Args:
        text: Full document text (may include frontmatter).
        path: File path for filename-based fallback.
    """
    from kairix.utils import display_name

    # Priority 1: existing frontmatter title
    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        fm_title = _title_from_frontmatter(fm_match.group(1))
        if fm_title:
            return fm_title
        body = text[fm_match.end() :]
    else:
        body = text

    # Priority 2: first heading in body
    heading_title = _title_from_first_heading(body)
    if heading_title:
        return heading_title

    # Priority 3: filename stem -> title case
    return display_name(path.stem) or "Untitled"
