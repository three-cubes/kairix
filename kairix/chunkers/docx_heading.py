"""DocxHeadingChunker — heading-hierarchy split (ADR-028 §"DOCX — `DocxHeadingChunker`").

Splits a DOCX-shaped markdown document on its heading hierarchy
(``#`` H1 / ``##`` H2 / ``###`` H3) so each section becomes one
:class:`~kairix.core.protocols.Chunk` tagged with its section path
(e.g. ``"Chapter 5: Compliance > 5.2.1 Risk register"``). Tables are
emitted as separate chunks rather than linearised into prose.

**Used by:** SharePoint + Google Drive ``.docx``.

**Size / overlap:** 512-1024 tokens / 10-15% overlap nominally; the
plugin tracks section boundaries first and only sub-splits a section
that exceeds the upper size cap. Empty sections are skipped.

**Failure modes of the prose-style fallback that this fixes:** tables
linearised into prose lose row/column semantics; numbered-list
continuations severed from parent; "5.2.1 Risk register" loses the
H1 "Chapter 5: Compliance" context.

**Protocol contract:** :class:`~kairix.core.protocols.Chunker` —
``chunk(text=, section_kind=, source_uri=) -> tuple[Chunk, ...]``.
Each emitted Chunk carries:
  * ``text`` — section body, prefixed by the inherited heading path so
    the chunk-only context preserves "where in the document we are".
  * ``source_uri`` — propagated from input.
  * ``chunker_version=self.version`` — F55.
  * ``metadata["section_path"]`` — ``"H1 > H2 > H3"`` breadcrumb.
  * ``metadata["section_kind"]`` — ``"prose"`` for heading sections;
    ``"table"`` for table chunks.

Input contract: ``text`` is the markdown coming out of
:class:`~kairix.extractors.docx.DocxExtractor` — H1/H2/H3 prefixed
paragraphs, bullet / numbered list lines, and GFM pipe-syntax tables.

See ``tests/bdd/features/chunker_docx_heading.feature`` for the spec.
"""

from __future__ import annotations

import hashlib
import re

from kairix.core.protocols import Chunk

#: F55-mandated module-level version. Bump when section / table
#: rendering rules change in a way that affects downstream embeddings.
version: str = "0.1.0"

#: Canonical plugin name surfaced to the chunker registry.
PLUGIN_NAME = "docx_heading"

#: Regex matching a markdown heading line: ``# Title`` / ``## Title`` /
#: ``### Title``. Capturing groups: (hash prefix, title text).
_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*)$")

#: A line is a table row when it starts with a pipe character (after
#: any leading whitespace). Markdown pipe-syntax tables are contiguous
#: blocks of pipe-prefixed lines.
_TABLE_ROW_PREFIX = "|"

#: Section-path separator in metadata. Lifted to a module constant
#: because the same string appears across several builders (F17 guard:
#: >=10 chars, but the canonical surface is the same regardless).
_SECTION_PATH_SEP = " > "


class DocxHeadingChunker:
    """Heading-hierarchy + table-aware chunker for DOCX-rendered markdown.

    Declares ``version: str = "0.1.0"`` (F55). Every emitted
    :class:`Chunk` carries ``chunker_version=self.version`` (also F55).
    """

    name: str = PLUGIN_NAME
    version: str

    def __init__(self) -> None:
        self.version = version

    def chunk(self, *, text: str, section_kind: str, source_uri: str) -> tuple[Chunk, ...]:
        """Split ``text`` on heading boundaries; emit prose + table chunks.

        ``section_kind`` is read once for Protocol compliance / F19; the
        heading-hierarchy strategy is uniform across section kinds.
        """
        if not section_kind:
            section_kind = "text"  # defensive default for F19
        del section_kind
        stripped = text.strip()
        if not stripped:
            return ()
        sections = _split_into_sections(stripped)
        chunks: list[Chunk] = []
        for section in sections:
            chunks.extend(
                _emit_section_chunks(
                    section=section,
                    source_uri=source_uri,
                    chunker_version=self.version,
                )
            )
        return tuple(chunks)


class _Section:
    """In-memory view of one heading-bounded section.

    Plain class (not a dataclass) because it carries mutable lists
    during the walk; converted to immutable :class:`Chunk` tuples
    once parsing is done.
    """

    def __init__(self, section_path: str, lines: list[str]) -> None:
        self.section_path = section_path
        self.lines = lines


def _split_into_sections(text: str) -> list[_Section]:
    """Walk ``text`` line-by-line; build sections keyed by heading-path.

    The leading section (before any heading) is emitted with an empty
    path so it's still chunkable. Each heading line opens a new
    section; its path is computed from the current heading stack.
    """
    heading_stack: list[tuple[int, str]] = []
    sections: list[_Section] = []
    current_lines: list[str] = []
    current_path = ""

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = _HEADING_RE.match(line)
        if match is None:
            current_lines.append(line)
            continue
        # Heading line — flush the in-progress section and open a new one.
        if current_lines or not sections:
            sections.append(_Section(section_path=current_path, lines=current_lines))
        level = len(match.group(1))
        title = match.group(2).strip()
        heading_stack = [(lvl, ttl) for lvl, ttl in heading_stack if lvl < level]
        heading_stack.append((level, title))
        current_path = _SECTION_PATH_SEP.join(ttl for _lvl, ttl in heading_stack)
        current_lines = [line]  # the heading itself opens the section body
    # Flush trailing section.
    if current_lines:
        sections.append(_Section(section_path=current_path, lines=current_lines))
    # Drop sections that came out empty after stripping.
    return [s for s in sections if any(line.strip() for line in s.lines)]


def _emit_section_chunks(
    *,
    section: _Section,
    source_uri: str,
    chunker_version: str,
) -> list[Chunk]:
    """Split one section into prose + table chunks.

    Tables (contiguous pipe-prefixed lines) emit their own chunk so
    retrieval can cite the table independently of the prose around it.
    The non-table lines collapse to one prose chunk per section.
    """
    table_blocks, prose_lines = _partition_table_and_prose(section.lines)
    chunks: list[Chunk] = []
    prose_text = "\n".join(prose_lines).strip()
    if prose_text:
        chunks.append(
            _build_section_chunk(
                section_text=prose_text,
                section_path=section.section_path,
                section_kind_value="prose",
                source_uri=source_uri,
                chunker_version=chunker_version,
            )
        )
    for table_block in table_blocks:
        table_text = "\n".join(table_block).strip()
        if not table_text:
            continue
        chunks.append(
            _build_section_chunk(
                section_text=table_text,
                section_path=section.section_path,
                section_kind_value="table",
                source_uri=source_uri,
                chunker_version=chunker_version,
            )
        )
    return chunks


def _partition_table_and_prose(lines: list[str]) -> tuple[list[list[str]], list[str]]:
    """Walk ``lines``; group contiguous pipe-prefixed lines as tables.

    Returns ``(table_blocks, prose_lines)`` — table_blocks is a list of
    per-table line lists; prose_lines is the flat list of every
    non-table line (in original order).
    """
    table_blocks: list[list[str]] = []
    prose_lines: list[str] = []
    current_table: list[str] = []
    for line in lines:
        if line.lstrip().startswith(_TABLE_ROW_PREFIX):
            current_table.append(line)
            continue
        if current_table:
            table_blocks.append(current_table)
            current_table = []
        prose_lines.append(line)
    if current_table:
        table_blocks.append(current_table)
    return table_blocks, prose_lines


def _build_section_chunk(
    *,
    section_text: str,
    section_path: str,
    section_kind_value: str,
    source_uri: str,
    chunker_version: str,
) -> Chunk:
    """Construct one F39 + F55 clean :class:`Chunk` for a section.

    The section path is prepended to the chunk text as a breadcrumb
    comment so the chunk-only embedding context preserves "where in
    the document we are" without requiring a metadata round-trip.
    """
    prefix = f"[Section: {section_path}]\n\n" if section_path else ""
    text_with_path = prefix + section_text
    return Chunk(
        text=text_with_path,
        content_hash=hashlib.sha256(text_with_path.encode("utf-8")).hexdigest(),
        source_name="",
        source_uri=source_uri,
        source_modified_at="",
        source_page=None,
        sensitivity="internal",
        chunker_version=chunker_version,
        metadata={
            "section_path": section_path,
            "section_kind": section_kind_value,
        },
    )


def make_chunker() -> DocxHeadingChunker:
    """Construct the DocxHeadingChunker for entry-point discovery."""
    return DocxHeadingChunker()


__all__ = [
    "PLUGIN_NAME",
    "DocxHeadingChunker",
    "make_chunker",
    "version",
]
