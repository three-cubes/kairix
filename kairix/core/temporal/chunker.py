"""
kairix.core.temporal.chunker — Pre-processor for date-indexed document chunks.

Transforms two document types into TemporalChunk objects:

1. Kanban board files (e.g. <boards-dir>/Sprint.md)
   - Parses kanban columns (## Done, ## In Progress, ## Ready, ## Backlog)
   - Extracts [completed::YYYY-MM-DD], [started::YYYY-MM-DD], [created::YYYY-MM-DD]
   - Each card → one TemporalChunk with date, status, source_path, card_id

2. Daily memory logs (e.g. memory/2026-03-22.md)
   - Parses date from filename
   - Splits on ## section headers
   - Each section → one TemporalChunk with date from filename, section_heading

Never raises — returns [] on any parse failure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from kairix.text import strip_frontmatter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class TemporalChunk:
    """A date-indexed chunk of content from a board card or memory section."""

    text: str
    date: date | None  # None if undated
    source_path: str  # Original file path
    chunk_type: str  # "board_card" | "memory_section"
    metadata: dict = field(default_factory=dict)  # status, section_heading, card_id, etc.


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Kanban column headings — normalised to status strings
_COLUMN_STATUS: dict[str, str] = {
    "done": "done",
    "completed": "done",
    "finished": "done",
    "in progress": "in_progress",
    "in-progress": "in_progress",
    "wip": "in_progress",
    "ready": "ready",
    "todo": "ready",
    "to do": "ready",
    "backlog": "backlog",
    "icebox": "backlog",
    "blocked": "blocked",
}

# Date extraction patterns in card text — in priority order
_DATE_FIELD_RE = re.compile(
    r"\[(?P<field>completed|started|created)::(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\]",
    re.IGNORECASE,
)

# Memory log filename pattern: YYYY-MM-DD.md
_MEMORY_LOG_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")

# Card line: starts with "- [ ]" or "- [x]" (checklist item)
_CARD_LINE_RE = re.compile(
    r"^[-*]\s+\[[ xX]\]\s+", re.MULTILINE
)  # NOSONAR — re.MULTILINE-anchored; no nested quantifiers; backtracking is linear.

# Section heading for boards (## Heading at start of line)
_SECTION_H2_RE = re.compile(
    r"^##\s+(.+)$", re.MULTILINE
)  # NOSONAR — single-line via re.MULTILINE; bounded by line length.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_column(heading: str) -> str:
    """Map a column heading to a canonical status string."""
    key = heading.strip().lower()
    return _COLUMN_STATUS.get(key, key.replace(" ", "_"))


def _extract_date_from_card(text: str) -> tuple[date | None, str | None]:
    """
    Extract the best date from card text.

    Priority: completed > started > created
    Returns (date_obj, field_name) or (None, None) if no date found.
    """
    priority = ["completed", "started", "created"]
    found: dict[str, date] = {}

    for m in _DATE_FIELD_RE.finditer(text):
        field_name = m.group("field").lower()
        try:
            d = date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
            found[field_name] = d
        except ValueError:
            pass

    for f in priority:
        if f in found:
            return found[f], f
    return None, None


def _make_card_id(source_path: str, column: str, index: int) -> str:
    """Generate a deterministic card ID from path + column + index."""
    stem = Path(source_path).stem
    col_slug = column.lower().replace(" ", "-")
    return f"{stem}:{col_slug}:{index}"


# ---------------------------------------------------------------------------
# Board chunker
# ---------------------------------------------------------------------------


def chunk_board(path: str) -> list[TemporalChunk]:
    """
    Parse a Kanban board file into per-card TemporalChunk objects.

    Scans for ## section headings to detect columns, then parses each
    checklist item (- [ ] or - [x]) as a card.

    Args:
        path: Absolute or relative path to the board Markdown file.

    Returns:
        List of TemporalChunk objects, one per card.
        Returns [] on any parse failure.
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("chunk_board: cannot read %r — %s", path, e)
        return []

    # Strip kanban plugin frontmatter if present
    content = strip_frontmatter(content)

    chunks: list[TemporalChunk] = []

    # Split the document into column sections by ## headings
    # We keep the heading text along with the section body
    lines = content.splitlines()

    current_column: str = "backlog"
    current_status: str = "backlog"
    card_buffer: list[str] = []
    card_index: int = 0

    def _flush_card(col: str, status: str, buf: list[str], idx: int) -> None:
        """Convert buffered card lines into a TemporalChunk and append to chunks."""
        if not buf:
            return
        card_text = "\n".join(buf).strip()
        card_date, date_field = _extract_date_from_card(card_text)
        card_id = _make_card_id(path, col, idx)

        meta: dict = {
            "status": status,
            "column": col,
            "card_id": card_id,
        }
        if date_field:
            meta["date_field"] = date_field

        chunks.append(
            TemporalChunk(
                text=card_text,
                date=card_date,
                source_path=path,
                chunk_type="board_card",
                metadata=meta,
            )
        )

    for line in lines:
        h2_match = _SECTION_H2_RE.match(line)
        if h2_match:
            _flush_card(current_column, current_status, card_buffer, card_index)
            card_buffer = []
            card_index += 1
            heading_text = h2_match.group(1).strip()
            current_column = heading_text
            current_status = _normalise_column(heading_text)
            continue

        if _CARD_LINE_RE.match(line):
            _flush_card(current_column, current_status, card_buffer, card_index)
            card_buffer = [line]
            card_index += 1
            continue

        if not card_buffer:
            continue

        # Continuation: indented / blank lines stay with the card; anything
        # else terminates the card buffer.
        if line.strip() == "" or line.startswith(("  ", "\t")):
            card_buffer.append(line)
        else:
            _flush_card(current_column, current_status, card_buffer, card_index)
            card_buffer = []

    _flush_card(current_column, current_status, card_buffer, card_index)

    logger.debug("chunk_board: %r → %d chunks", path, len(chunks))
    return chunks


# ---------------------------------------------------------------------------
# Memory log chunker
# ---------------------------------------------------------------------------


def _extract_log_date(name: str) -> date | None:
    """Pull the ``YYYY-MM-DD`` from a memory-log filename, or ``None`` if absent/invalid."""
    m = _MEMORY_LOG_RE.match(name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def chunk_memory_log(path: str) -> list[TemporalChunk]:
    """Parse a daily memory log into per-section TemporalChunk objects.

    The date is extracted from the filename (YYYY-MM-DD.md).
    Content is split on ## headings; each section becomes a chunk.

    Args:
        path: Absolute or relative path to the memory log file.

    Returns:
        List of TemporalChunk objects, one per section.
        Returns a single undated chunk if no ## headings are found.
        Returns [] on any parse failure.
    """
    p = Path(path)
    log_date = _extract_log_date(p.name)

    try:
        content = p.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("chunk_memory_log: cannot read %r — %s", path, e)
        return []

    # Strip frontmatter
    content = strip_frontmatter(content)

    chunks: list[TemporalChunk] = []
    lines = content.splitlines()

    current_heading: str | None = None
    section_lines: list[str] = []

    def _flush_section(heading: str | None, buf: list[str]) -> None:
        """Flush buffered section lines into a TemporalChunk."""
        text = "\n".join(buf).strip()
        if not text:
            return
        # Prepend heading to text for context
        full_text = f"## {heading}\n{text}" if heading else text
        chunks.append(
            TemporalChunk(
                text=full_text,
                date=log_date,
                source_path=path,
                chunk_type="memory_section",
                metadata={"section_heading": heading},
            )
        )

    for line in lines:
        h2_match = _SECTION_H2_RE.match(line)
        if h2_match:
            _flush_section(current_heading, section_lines)
            current_heading = h2_match.group(1).strip()
            section_lines = []
        else:
            section_lines.append(line)

    # Flush final section
    _flush_section(current_heading, section_lines)

    # If nothing was produced (no ## headings, just raw text), emit one chunk
    if not chunks:
        text = content.strip()
        if text:
            chunks.append(
                TemporalChunk(
                    text=text,
                    date=log_date,
                    source_path=path,
                    chunk_type="memory_section",
                    metadata={"section_heading": None},
                )
            )

    logger.debug("chunk_memory_log: %r → %d chunks", path, len(chunks))
    return chunks


# ---------------------------------------------------------------------------
# Auto-dispatch
# ---------------------------------------------------------------------------

# Patterns used to identify board files
_BOARD_DIR_RE = re.compile(r"[/\\]Boards?[/\\]", re.IGNORECASE)
_BOARD_SUFFIX_RE = re.compile(r"(?:Board|Kanban)", re.IGNORECASE)


def _is_board_file(path: str) -> bool:
    """Heuristic: return True if path looks like a Kanban board file."""
    p = Path(path)
    # Check filename
    if _BOARD_SUFFIX_RE.search(p.stem):
        return True
    # Check parent directory
    if _BOARD_DIR_RE.search(str(p.parent)):
        return True
    # Peek at first 500 chars for kanban-plugin marker
    try:
        content = p.read_text(encoding="utf-8", errors="ignore")[:500]
        if "kanban-plugin" in content or "## Done" in content or "## Backlog" in content:
            return True
    except OSError:
        pass
    return False


def _is_memory_log(path: str) -> bool:
    """Return True if filename matches YYYY-MM-DD.md pattern."""
    return bool(_MEMORY_LOG_RE.match(Path(path).name))


def chunk_file(path: str) -> list[TemporalChunk]:
    """
    Auto-detect file type and dispatch to the appropriate chunker.

    Detection order:
      1. Filename matches YYYY-MM-DD.md → memory log
      2. Path contains /Boards/ or filename contains Board/Kanban → board
      3. File content contains kanban-plugin marker → board
      4. Default: memory log (treat as generic dated document)

    Returns [] on any parse failure.
    """
    if _is_memory_log(path):
        return chunk_memory_log(path)
    if _is_board_file(path):
        return chunk_board(path)
    # Default: try memory log (will produce undated chunks if no ## headings)
    logger.debug("chunk_file: %r — type unknown, defaulting to memory_log chunker", path)
    return chunk_memory_log(path)
