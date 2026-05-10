"""Markdown cleanup for reference library normalisation.

Handles HTML stripping, badge removal, line ending normalisation,
and Project Gutenberg boilerplate removal.
"""

from __future__ import annotations

import re

# Badge images: [![alt](img_url)](link_url) or ![alt](img_url)
_BADGE_RE = re.compile(
    r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)"  # [![badge](img)](link)
    r"|!\[(?:build|ci|test|coverage|license|licence|npm|pypi|version|downloads|stars|badge|status)[^\]]*\]\([^)]*\)",
    re.IGNORECASE,
)

# HTML block tags to strip (but preserve content between them)
# NOSONAR: bounded by `>` terminator and a fixed alternation
# of well-known tag names; input is reflib markdown (file-bounded).
_HTML_BLOCK_STRIP_RE = re.compile(
    r"</?(?:div|span|center|font|b|i|u|em|strong|br|hr)\s*[^>]*?>",
    re.IGNORECASE,
)

# HTML img tags — strip entirely (content is in the tag, not between)
# NOSONAR: bounded by `>` terminator; reflib input.
_HTML_IMG_RE = re.compile(r"<img\s[^>]*?>", re.IGNORECASE)

# HTML anchor tags — convert to markdown links
# NOSONAR: all `?` quantifiers are non-greedy and bounded by
# literal terminators (`["\']`, `>`, `</a>`); reflib input.
_HTML_ANCHOR_RE = re.compile(
    r'<a\s+(?:[^>]*?\s+)?href=["\']([^"\']*)["\'][^>]*?>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# HTML comment blocks
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Project Gutenberg header — everything before the real content
_GUTENBERG_START_RE = re.compile(
    r"\*\*\*\s*START OF (?:THE |THIS )?PROJECT GUTENBERG.*?\*\*\*",
    re.IGNORECASE,
)

# Project Gutenberg footer — everything after this marker
_GUTENBERG_END_RE = re.compile(
    r"\*\*\*\s*END OF (?:THE |THIS )?PROJECT GUTENBERG.*?\*\*\*",
    re.IGNORECASE,
)

# Multiple blank lines
_MULTI_BLANK_RE = re.compile(r"\n{4,}")

# Multiple spaces (not in code blocks)
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def strip_badges(text: str) -> str:
    """Remove CI/CD badge images that add noise to search and embedding."""
    return _BADGE_RE.sub("", text)


def strip_html_tags(text: str) -> str:
    """Remove inline HTML tags while preserving content.

    Keeps <details>/<summary> blocks (common in docs).
    Converts <a href> to markdown links.
    Strips <div>, <span>, <img>, comments.
    """
    # Convert anchors to markdown links first
    text = _HTML_ANCHOR_RE.sub(r"[\2](\1)", text)
    # Strip comments
    text = _HTML_COMMENT_RE.sub("", text)
    # Strip img tags
    text = _HTML_IMG_RE.sub("", text)
    # Strip block/inline tags (keep content)
    text = _HTML_BLOCK_STRIP_RE.sub("", text)
    return text


def strip_gutenberg_boilerplate(text: str) -> str:
    """Remove Project Gutenberg header and footer boilerplate.

    Strips everything before '*** START OF THE PROJECT GUTENBERG ...'
    and everything after '*** END OF THE PROJECT GUTENBERG ...'.
    """
    # Remove header (everything up to and including the START marker)
    start_match = _GUTENBERG_START_RE.search(text)
    if start_match:
        text = text[start_match.end() :]

    # Remove footer (everything from the END marker onwards)
    end_match = _GUTENBERG_END_RE.search(text)
    if end_match:
        text = text[: end_match.start()]

    return text.strip()


def normalise_line_endings(text: str) -> str:
    """Convert CRLF to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def collapse_blank_lines(text: str) -> str:
    """Collapse 4+ consecutive blank lines to 2."""
    return _MULTI_BLANK_RE.sub("\n\n\n", text)


def clean_markdown(text: str, is_gutenberg: bool = False) -> str:
    """Apply all markdown cleanup steps.

    Args:
        text: Raw markdown content.
        is_gutenberg: If True, strip Project Gutenberg boilerplate.
    """
    text = normalise_line_endings(text)
    if is_gutenberg:
        text = strip_gutenberg_boilerplate(text)
    text = strip_badges(text)
    text = strip_html_tags(text)
    text = collapse_blank_lines(text)
    return text.strip() + "\n"
