"""Heading-aware markdown chunker — splits on H1/H2/H3, recursive on oversize.

The algorithm in plain English:

  1. Walk the markdown line-by-line, tracking the current heading
     stack (H1 > H2 > H3). A new heading at depth N pops every entry
     at depth >= N and pushes the new title.
  2. Each leaf section (lines between two headings or between a
     heading and end-of-document) is one candidate chunk. The section
     starts with its breadcrumb header path so the embedding sees the
     context.
  3. If a section is under the character budget, emit one chunk.
  4. If a section exceeds the budget, recursively split on paragraph
     boundaries (``\\n\\n``) into windows that fit. Apply a sliding
     overlap **within the section only** — never across heading
     boundaries — so each window keeps the trailing ``OVERLAP_CHARS``
     of the previous one as context.
  5. If a single paragraph still exceeds the budget after step 4,
     hard-split on sentence boundaries; if that still overflows
     (rare), hard-cut on character count.

No upstream dependency — keeps the wheel light and the behaviour
fully covered by tests in this repo. The implementation matches the
recipes cited in ADR-028 §"Markdown" (LangChain's
``MarkdownHeaderTextSplitter`` + ``RecursiveCharacterTextSplitter``
pattern) without dragging the LangChain dependency tree along.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from kairix.core.protocols import Chunk

#: Canonical plugin name surfaced by the entry-point registry.
PLUGIN_NAME = "markdown_structural"

#: Target chunk size in characters. 512 tokens * 4 chars/token = 2048.
#: ADR-028 §"Markdown" sets 512 tokens as the target; the char budget
#: is the proxy used here (no tokeniser dependency).
_TARGET_CHARS = 2048

#: Overlap window applied **inside** an oversize section's recursive
#: split. 10-15 % of 2048 = ~256. Never crosses a heading boundary
#: (the boundary is the topic; bleed defeats the point).
_OVERLAP_CHARS = 256

#: Maximum heading depth honoured. ADR-028 §"Markdown" pins H1/H2/H3
#: as the splitter's structural surface; H4+ stay inside the section.
_MAX_HEADING_DEPTH = 3

#: ATX heading regex (``#``-prefixed); we ignore setext-style
#: underline headings (``===`` / ``---``) — they're rare in modern
#: markdown and add edge-case complexity without measurable lift.
#: Group 2 is the raw heading text from its first non-space character;
#: the optional closing ``#`` sequence is stripped in code by
#: :func:`_strip_closing_hashes` so the pattern stays linear-time.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*)$")

#: Sentence-boundary regex for the final fallback. Conservative —
#: requires a sentence-final punctuation mark followed by whitespace.
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")

#: Metadata key carrying the heading hierarchy path (joined with " > ").
#: Read by downstream retrieval to surface "Section X > Subsection Y"
#: provenance alongside the chunk text.
_METADATA_HEADING_PATH = "heading_path"


@dataclass(frozen=True)
class _Section:
    """One contiguous markdown section keyed by heading path."""

    heading_path: tuple[str, ...]
    body: str


class MarkdownStructuralChunker:
    """Heading-aware :class:`Chunker` for markdown documents.

    Construct with ``version=`` from the package-level
    :data:`kairix.chunkers.markdown_structural.version` constant so
    the F55 declaration site stays canonical (no per-class duplicate
    string literal).
    """

    def __init__(self, *, version: str) -> None:
        """Bind the F55 version string and the plugin name.

        ``version`` is passed through to every emitted Chunk's
        ``chunker_version`` field — that's how the maintenance-tick
        re-chunk sweep (ADR-028 §"Mechanics") finds documents on
        stale chunker output.
        """
        self.name: str = PLUGIN_NAME
        self.version: str = version

    def chunk(self, *, text: str, section_kind: str, source_uri: str) -> tuple[Chunk, ...]:
        """Split ``text`` on heading boundaries and emit Chunk items.

        ``section_kind`` is part of the :class:`Chunker` Protocol
        shape; the markdown chunker is heading-driven irrespective of
        the section discriminator, so the value is consumed once for
        F19 and then unused. ``source_uri`` is propagated to every
        emitted Chunk per F39.
        """
        if section_kind:
            # Held live for F19; markdown chunking is heading-driven,
            # not section-kind-driven.
            del section_kind
        sections = _split_into_sections(text)
        chunks: list[Chunk] = []
        for section in sections:
            chunks.extend(_emit_section_chunks(section, source_uri, self.version))
        return tuple(chunks)


def _split_into_sections(text: str) -> tuple[_Section, ...]:
    """Walk ``text`` line-by-line and group lines under their heading path.

    The first section (before any heading) carries an empty
    ``heading_path``; downstream emit logic treats it the same as any
    other section.
    """
    if not text.strip():
        return ()
    heading_stack: list[str] = []
    current_lines: list[str] = []
    sections: list[_Section] = []

    def flush_current(path: tuple[str, ...]) -> None:
        body = "\n".join(current_lines).strip()
        if body:
            sections.append(_Section(heading_path=path, body=body))
        current_lines.clear()

    last_path: tuple[str, ...] = ()
    for line in text.splitlines():
        depth, title = _parse_heading(line)
        if depth is not None and title is not None:
            flush_current(last_path)
            heading_stack = heading_stack[: depth - 1]
            heading_stack.append(title)
            last_path = tuple(heading_stack[:_MAX_HEADING_DEPTH])
            continue
        current_lines.append(line)
    flush_current(last_path)
    return tuple(sections)


def _parse_heading(line: str) -> tuple[int | None, str | None]:
    """Return ``(depth, title)`` for ATX heading lines, else ``(None, None)``.

    Depth is the count of leading ``#``; title is the heading text
    with leading / trailing whitespace + trailing ``#`` markers
    stripped.
    """
    match = _HEADING_RE.match(line.rstrip())
    if match is None:
        return None, None
    depth = len(match.group(1))
    title = _strip_closing_hashes(match.group(2)).strip()
    if not title:
        return None, None
    return depth, title


def _strip_closing_hashes(text: str) -> str:
    """Drop an ATX closing sequence — a trailing ``#`` run preceded by a space.

    Per CommonMark the run only closes the heading when whitespace precedes it
    (``C#`` keeps its ``#``), and an all-``#`` title is an empty heading.
    """
    opened = text.rstrip("#")
    if not opened:
        return ""
    return opened.rstrip() if opened[-1].isspace() else text


def _emit_section_chunks(section: _Section, source_uri: str, chunker_version: str) -> tuple[Chunk, ...]:
    """Emit one or more Chunks for ``section`` honouring the size budget."""
    prefix = _format_heading_prefix(section.heading_path)
    body_with_prefix = f"{prefix}{section.body}" if prefix else section.body
    if len(body_with_prefix) <= _TARGET_CHARS:
        return (_build_chunk(body_with_prefix, source_uri, chunker_version, section.heading_path),)
    # Oversize section — split recursively on paragraph / sentence
    # boundaries, applying overlap **within the section**.
    windows = _split_oversize_section(body_with_prefix)
    return tuple(_build_chunk(w, source_uri, chunker_version, section.heading_path) for w in windows)


def _format_heading_prefix(path: tuple[str, ...]) -> str:
    """Format the heading breadcrumb as a one-line prefix.

    Empty path returns an empty string. Non-empty paths render as
    ``"# Section > Sub\\n\\n"`` so the embedding sees a single human-
    readable context line, not the original ``#`` / ``##`` markup
    (which an embedding model treats as noise).
    """
    if not path:
        return ""
    return f"# {' > '.join(path)}\n\n"


def _split_oversize_section(text: str) -> tuple[str, ...]:
    """Window an oversize section into ``_TARGET_CHARS``-sized chunks.

    Strategy:
      1. Split on paragraph boundaries (``\\n\\n``).
      2. Greedily pack paragraphs into a window until adding the next
         paragraph would exceed the budget.
      3. When a single paragraph exceeds the budget, sentence-split
         it first; if a single sentence still exceeds the budget,
         hard-cut on character count.
      4. Apply overlap by carrying the trailing ``_OVERLAP_CHARS`` of
         each emitted window into the start of the next.
    """
    pieces = _split_into_size_safe_pieces(text)
    if not pieces:
        return ()
    windows: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
            continue
        if len(current) + len(piece) + 2 <= _TARGET_CHARS:
            current = current + "\n\n" + piece
        else:
            windows.append(current)
            overlap = current[-_OVERLAP_CHARS:] if len(current) > _OVERLAP_CHARS else current
            current = overlap + "\n\n" + piece
    if current:
        windows.append(current)
    return tuple(windows)


def _split_into_size_safe_pieces(text: str) -> tuple[str, ...]:
    """Split ``text`` into pieces no larger than the target budget.

    Paragraph → sentence → hard char cut, in that order.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for para in paragraphs:
        if len(para) <= _TARGET_CHARS:
            pieces.append(para)
            continue
        sentences = _SENTENCE_BOUNDARY_RE.split(para)
        for sent in sentences:
            if len(sent) <= _TARGET_CHARS:
                pieces.append(sent)
                continue
            # Hard cut — sentence longer than budget (extremely long
            # URL, base64 blob, …). Walk in budget-sized strides.
            pieces.extend(_hard_cut(sent))
    return tuple(pieces)


def _hard_cut(text: str) -> tuple[str, ...]:
    """Final fallback — chop ``text`` into ``_TARGET_CHARS``-sized slices."""
    return tuple(text[i : i + _TARGET_CHARS] for i in range(0, len(text), _TARGET_CHARS))


def _build_chunk(
    text: str,
    source_uri: str,
    chunker_version: str,
    heading_path: tuple[str, ...],
) -> Chunk:
    """Construct one :class:`Chunk` from a rendered window.

    The :class:`Chunker` Protocol only carries ``text``, ``section_kind``
    and ``source_uri`` — ``source_name``, ``source_modified_at`` and
    ``sensitivity`` are wrapped on by Silver at the composition site
    (same shape the fallback uses). Heading path is surfaced via
    ``metadata`` so downstream retrieval can render the breadcrumb.
    """
    metadata: dict[str, str] = {}
    if heading_path:
        metadata[_METADATA_HEADING_PATH] = " > ".join(heading_path)
    return Chunk(
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        source_name="",
        source_uri=source_uri,
        source_modified_at="",
        source_page=None,
        sensitivity="internal",
        chunker_version=chunker_version,
        metadata=metadata,
    )
