"""SheetRowChunker — one row = one chunk (ADR-028 §"XLSX — `SheetRowChunker`").

For tabular sheets above the small-sheet threshold (default: 50 data
rows), every row becomes one :class:`~kairix.core.protocols.Chunk` with
the header row prepended into the chunk text. For small reference
sheets (<= threshold), the whole sheet collapses to one chunk so the
reference context stays intact.

**Used by:** SharePoint + Google Drive ``.xlsx`` / ``.xls`` / ``.xlsm``.

**Size / overlap:** one row + header (tabular) or whole sheet (small
reference) / none — row / sheet boundary is the natural unit.

**Failure mode of prose-style chunking that this fixes:** row
``[42, "Acme", "2025-03", 17000]`` embedded without column headers
matches nothing useful; chunks spanning rows merge unrelated records;
sheet-level context ("FY26 Forecast" vs "FY25 Actuals") lost.

**Protocol contract:** :class:`~kairix.core.protocols.Chunker` —
``chunk(text=, section_kind=, source_uri=) -> tuple[Chunk, ...]``.
Each emitted Chunk carries:
  * ``text`` — header row + the row's pipe-syntax markdown (tabular),
    or the whole sheet markdown (small reference).
  * ``source_uri`` — propagated from input.
  * ``chunker_version=self.version`` — F55.
  * ``metadata["sheet_name"]`` — sheet title from the ``## Sheet:`` header.
  * ``metadata["row_index"]`` — 1-indexed row ordinal (header = 0;
    the small-sheet branch emits ``"0"``).
  * ``metadata["cell_ref"]`` — A1-style ref of the first cell of the
    row (e.g. ``A2``); small-sheet branch emits ``A1``.

Input contract: ``text`` is the sheet markdown coming out of
:class:`~kairix.extractors.xlsx.XlsxExtractor` — ``## Sheet: <title>``
header followed by a GitHub-Flavored Markdown pipe-syntax table.

See ``tests/bdd/features/chunker_sheet_row.feature`` for the spec.
"""

from __future__ import annotations

import hashlib
import re

from kairix.core.protocols import Chunk

#: F55-mandated module-level version. Bump when per-row rendering rules
#: change in a way that affects downstream embeddings.
version: str = "0.1.0"

#: Canonical plugin name surfaced to the chunker registry.
PLUGIN_NAME = "sheet_row"

#: Regex matching the ``## Sheet: <title>`` header XlsxExtractor emits
#: per sheet. Capturing group: the sheet title.
#: Only same-line spaces are allowed around the marker, so an unnamed sheet
#: (``## Sheet:``) never borrows its table header as the name; the name group is
#: then absent. Linear-time: no two adjacent quantifiers share a class.
_SHEET_HEADER_RE = re.compile(r"^##[^\S\n]+Sheet:[^\S\n]*(\S.*)?$", re.MULTILINE)

#: Pipe-syntax table row prefix and suffix.
_ROW_PIPE = "|"

#: Default small-sheet threshold (data rows, excluding the header).
#: Sheets at or below this size collapse to one chunk; sheets above
#: it emit one chunk per row with the header row prepended.
_DEFAULT_SMALL_SHEET_THRESHOLD = 50


class SheetRowChunker:
    """Row-per-chunk (or whole-sheet) chunker for XLSX-family sheets.

    Declares ``version: str = "0.1.0"`` (F55). Every emitted
    :class:`Chunk` carries ``chunker_version=self.version`` (also F55).

    The small-sheet threshold is configurable so operators with very
    short reference sheets (e.g. a 20-row enum table) can tune the
    "whole sheet as one chunk" boundary without code change.
    """

    name: str = PLUGIN_NAME
    version: str

    def __init__(self, small_sheet_threshold: int = _DEFAULT_SMALL_SHEET_THRESHOLD) -> None:
        """Construct the chunker; ``small_sheet_threshold`` is the data-row count
        at or below which the whole sheet becomes one chunk.
        """
        self.version = version
        self.small_sheet_threshold = small_sheet_threshold

    def chunk(self, *, text: str, section_kind: str, source_uri: str) -> tuple[Chunk, ...]:
        """Split ``text`` into one Chunk per data row, or one Chunk for the
        whole sheet (small-reference branch).

        ``section_kind`` is read once for Protocol compliance / F19; the
        sheet-row strategy is uniform across section kinds.
        """
        if not section_kind:
            section_kind = "tabular"  # defensive default for F19
        del section_kind
        stripped = text.strip()
        if not stripped:
            return ()
        sheet_name, table_lines = _parse_sheet(stripped)
        if not table_lines:
            return ()
        header_line, separator_line, data_rows = _split_table(table_lines)
        if header_line is None or not data_rows:
            return ()
        if len(data_rows) <= self.small_sheet_threshold:
            return (
                _build_whole_sheet_chunk(
                    sheet_text=stripped,
                    sheet_name=sheet_name,
                    source_uri=source_uri,
                    chunker_version=self.version,
                ),
            )
        return tuple(
            _build_row_chunk(
                header_line=header_line,
                separator_line=separator_line,
                row_line=row_line,
                row_ordinal=row_ordinal,
                sheet_name=sheet_name,
                source_uri=source_uri,
                chunker_version=self.version,
            )
            for row_ordinal, row_line in enumerate(data_rows, start=1)
        )


def _parse_sheet(text: str) -> tuple[str, list[str]]:
    """Return ``(sheet_name, table_lines)`` — sheet header and table body.

    The ``## Sheet:`` header is stripped from the returned lines.
    Blank lines between the header and the table are skipped. If no
    header is present, ``sheet_name`` is empty and every non-blank line
    is treated as table content.
    """
    match = _SHEET_HEADER_RE.search(text)
    if match is None:
        return "", [line for line in text.splitlines() if line.strip()]
    sheet_name = (match.group(1) or "").strip()
    body = text[match.end() :]
    table_lines = [line for line in body.splitlines() if line.strip()]
    return sheet_name, table_lines


def _split_table(table_lines: list[str]) -> tuple[str | None, str, list[str]]:
    """Return ``(header_line, separator_line, data_rows)`` from a pipe table.

    A well-formed GitHub-Flavored Markdown table has:
      * row 0 — header (pipe-delimited)
      * row 1 — separator (``| --- | --- |``)
      * row 2+ — data rows

    Returns ``(None, "", [])`` when the first line isn't pipe-delimited
    (defensive — chunker degrades gracefully on malformed input).
    """
    if not table_lines or not table_lines[0].lstrip().startswith(_ROW_PIPE):
        return None, "", []
    header_line = table_lines[0]
    separator_line = table_lines[1] if len(table_lines) > 1 else ""
    data_rows = [line for line in table_lines[2:] if line.lstrip().startswith(_ROW_PIPE)]
    return header_line, separator_line, data_rows


def _build_row_chunk(
    *,
    header_line: str,
    separator_line: str,
    row_line: str,
    row_ordinal: int,
    sheet_name: str,
    source_uri: str,
    chunker_version: str,
) -> Chunk:
    """Construct one F39 + F55 clean :class:`Chunk` for one data row.

    The chunk text is ``<header>\\n<separator>\\n<row>`` so a retrieval
    citation can render the row with column headers intact (the
    ADR-028 §"XLSX" failure mode the chunker exists to fix).
    """
    # Data rows are 1-indexed within the data block, but row 1 in the
    # spreadsheet is the header — so the A1-style ref is row + 1.
    cell_ref = f"A{row_ordinal + 1}"
    chunk_text = "\n".join([header_line, separator_line, row_line])
    return Chunk(
        text=chunk_text,
        content_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
        source_name="",
        source_uri=source_uri,
        source_modified_at="",
        source_page=None,
        sensitivity="internal",
        chunker_version=chunker_version,
        metadata={
            "sheet_name": sheet_name,
            "row_index": str(row_ordinal),
            "cell_ref": cell_ref,
        },
    )


def _build_whole_sheet_chunk(
    *,
    sheet_text: str,
    sheet_name: str,
    source_uri: str,
    chunker_version: str,
) -> Chunk:
    """Construct one F39 + F55 clean :class:`Chunk` carrying the entire sheet.

    Used for small reference sheets (rows <= small_sheet_threshold) so
    the inter-row context is preserved (the ADR-028 small-sheet branch).
    """
    return Chunk(
        text=sheet_text,
        content_hash=hashlib.sha256(sheet_text.encode("utf-8")).hexdigest(),
        source_name="",
        source_uri=source_uri,
        source_modified_at="",
        source_page=None,
        sensitivity="internal",
        chunker_version=chunker_version,
        metadata={
            "sheet_name": sheet_name,
            "row_index": "0",
            "cell_ref": "A1",
        },
    )


def make_chunker() -> SheetRowChunker:
    """Construct the SheetRowChunker for entry-point discovery."""
    return SheetRowChunker()


__all__ = [
    "PLUGIN_NAME",
    "SheetRowChunker",
    "make_chunker",
    "version",
]
