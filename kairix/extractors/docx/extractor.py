"""python-docx-backed extractor for Word ``.docx`` (OF-2, Wave 4).

Heading-hierarchy-aware extraction: ``Heading 1`` / ``Heading 2`` /
``Heading 3`` paragraphs render as markdown ``#`` / ``##`` / ``###``
respectively. Bullet and numbered lists render as
``- {text}`` / ``1. {text}``. Tables render as GitHub-Flavored
Markdown pipe-syntax tables. Track-changes are handled per the
"accepted version" rule — ``<w:ins>`` content stays, ``<w:del>``
content is skipped, and the extractor exposes a
``last_extract_had_tracked_changes`` boolean attribute that the
caller inspects when the metadata flag matters.

Spec ref: ``docs/architecture/connector-ingestion-architecture.md``
§2 ("extractors tree"), §3 ("Extractor Protocol"), §10 (Wave 4 OF-2);
KFEAT-012 Phase 2 §Word.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from kairix.core.protocols import SourceMetadata
from kairix.extractors import (
    DocMetadata,
    ExtractedDocument,
    MimeType,
)

#: Canonical plugin name surfaced by the extractor registry.
PLUGIN_NAME = "docx"

#: Minimum decoded-markdown length the plugin treats as "quality ok"
#: per spec §10. A non-trivial Word document recovers well over 100
#: characters; below this floor the document is treated as a parse
#: failure and the orchestrator escalates.
_QUALITY_MIN_CHARS = 100

#: IANA mime type for ``.docx`` (Office Open XML wordprocessing).
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: ZIP-archive header — every ``.docx`` file is a ZIP wrapping the
#: Office Open XML payload. Magic-byte sniff catches a docx served
#: with a generic Content-Type when paired with a mime hint that
#: ends with "document".
_MAGIC_ZIP = b"PK\x03\x04"

#: Heading-style-name → markdown heading-prefix mapping. Levels 1-3
#: are surfaced; deeper levels collapse to ``###`` (no markdown
#: equivalent that downstream chunking distinguishes).
_HEADING_PREFIX: dict[str, str] = {
    "Heading 1": "# ",
    "Heading 2": "## ",
    "Heading 3": "### ",
}

#: Style names that mark a paragraph as a bullet list item. python-docx
#: surfaces several variants of the same Word built-in style.
_BULLET_STYLES = frozenset({"List Bullet", "List Paragraph"})

#: Style names that mark a paragraph as a numbered list item.
_NUMBER_STYLES = frozenset({"List Number"})


class _DocxDocument(Protocol):
    """Wire-shape Protocol for the upstream :class:`docx.document.Document`.

    Declared locally so a unit test can pass an in-memory fake without
    monkeypatching the upstream module (F1-clean). The real
    ``docx.Document`` carries the same surface plus a long tail of
    layout / styling helpers we don't consult here.
    """

    @property
    def paragraphs(self) -> list[Any]:
        """All top-level paragraphs in the document body."""

    @property
    def tables(self) -> list[Any]:
        """All top-level tables in the document body."""

    @property
    def element(self) -> Any:
        """Underlying lxml element for the document — body XML access."""

    @property
    def core_properties(self) -> Any:
        """Title / author / created metadata."""


#: Type of the factory callable that opens a docx file on disk.
DocxOpener = Callable[[str], _DocxDocument]


def _default_document_opener() -> DocxOpener:
    """Lazy-import the upstream :func:`docx.Document` callable.

    python-docx is declared as an *optional* dependency in
    ``pyproject.toml`` (extra ``docx``) — operators ingesting only
    markdown / plain-text content skip the install. Resolving the
    import inside the factory means
    ``import kairix.extractors.docx`` succeeds in environments
    without the upstream library; the ``RuntimeError`` only fires
    when ``extract()`` is actually called.
    """
    try:
        import docx as _docx
    except ImportError as exc:
        raise RuntimeError(
            "docx: the upstream 'python-docx' package is not installed. "
            "fix: pip install 'Kairix-agentic-knowledge-mgt[docx]' "
            "to opt into the heading-aware Word extractor (MIT licence). "
            "next: re-run the connector sync; docx will then resolve."
        ) from exc

    def _open_path(path: str) -> _DocxDocument:
        # python-docx returns its concrete ``Document`` type which
        # satisfies the local :class:`_DocxDocument` Protocol structurally.
        return cast("_DocxDocument", _docx.Document(path))

    return _open_path


def _paragraph_to_markdown(style_name: str, text: str) -> str:
    """Render one paragraph as one markdown line per style.

    Headings render as ``#``/``##``/``###`` prefixes. List items
    render as ``- ...`` / ``1. ...`` depending on style. Anything
    else renders verbatim. Empty paragraphs (no text after strip)
    collapse to the empty string so a caller can filter them out.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    prefix = _HEADING_PREFIX.get(style_name)
    if prefix is not None:
        return prefix + stripped
    if style_name in _BULLET_STYLES:
        return "- " + stripped
    if style_name in _NUMBER_STYLES:
        return "1. " + stripped
    return stripped


def _table_row_to_markdown(cells: list[str]) -> str:
    """Cells are stripped + newline-collapsed so each rendered row is one
    physical line; empty cells stay empty so column alignment is preserved.
    """
    rendered = [cell.strip().replace("\n", " ") for cell in cells]
    return "| " + " | ".join(rendered) + " |"


def _table_to_markdown(rows: list[list[str]]) -> str:
    """First row is the header; a ``| --- |`` separator row is injected
    so the result is valid GitHub-Flavored Markdown.
    """
    if not rows:
        return ""
    header = _table_row_to_markdown(rows[0])
    separator = "| " + " | ".join(["---"] * len(rows[0])) + " |"
    body = [_table_row_to_markdown(row) for row in rows[1:]]
    return "\n".join([header, separator, *body])


def _has_tracked_changes(body_xml: str) -> bool:
    """``True`` iff the document body XML carries any ``<w:ins>`` /
    ``<w:del>`` element. Used to populate the side-channel flag on
    the extractor instance per OF-2 §Word.
    """
    return ("<w:ins" in body_xml) or ("<w:del" in body_xml)


def _paragraph_accepted_text(paragraph: Any) -> str:
    """Return the paragraph's text with track-changes accepted.

    Walks the underlying lxml element, collecting ``<w:t>`` text but
    skipping any ``<w:t>`` whose ancestor chain contains a ``<w:del>``
    element. ``<w:ins>`` content stays in the output (the insertion
    is accepted). Falls back to ``paragraph.text`` when the element
    is not lxml-shaped (e.g. test fakes).

    The walk uses :meth:`lxml.etree._Element.iter` to visit every
    descendant; for each ``<w:t>`` node we walk up the ancestor chain
    looking for a ``<w:del>`` parent.
    """
    element = getattr(paragraph, "_p", None)
    if element is None or not hasattr(element, "iter"):
        return getattr(paragraph, "text", "") or ""
    try:
        from lxml import etree
    except ImportError:  # pragma: no cover — lxml ships with python-docx
        return getattr(paragraph, "text", "") or ""
    pieces: list[str] = []
    for node in element.iter():
        if etree.QName(node).localname != "t":
            continue
        if _node_inside_del(node, etree):
            continue
        if node.text:
            pieces.append(node.text)
    return "".join(pieces)


def _node_inside_del(node: Any, etree_module: Any) -> bool:
    """Return ``True`` iff any ancestor of ``node`` is a ``<w:del>``."""
    parent = node.getparent()
    while parent is not None:
        if etree_module.QName(parent).localname == "del":
            return True
        parent = parent.getparent()
    return False


def _table_to_rows(table: Any) -> list[list[str]]:
    """Convert a python-docx ``Table`` into a list-of-list-of-strings.

    Each row maps to the inner list; each cell becomes one string.
    Cell text consumes python-docx's ``.text`` property which already
    flattens runs into a single string per cell.
    """
    rows: list[list[str]] = []
    for row in getattr(table, "rows", []):
        cells = [getattr(cell, "text", "") for cell in getattr(row, "cells", [])]
        rows.append(cells)
    return rows


def _has_any_heading(markdown: str) -> bool:
    """``True`` iff ``markdown`` contains at least one heading line.

    A heading line starts (after any leading whitespace) with one or
    more ``#`` characters followed by a space.
    """
    for line in markdown.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") and " " in stripped:
            return True
    return False


def _doc_metadata(document: _DocxDocument) -> DocMetadata:
    """Adapt the docx ``core_properties`` to :class:`DocMetadata`."""
    props = getattr(document, "core_properties", None)
    title = _clean_string(getattr(props, "title", None)) if props is not None else None
    author = _clean_string(getattr(props, "author", None)) if props is not None else None
    created = getattr(props, "created", None) if props is not None else None
    created_iso: str | None
    if created is None:
        created_iso = None
    else:
        try:
            created_iso = created.isoformat()
        except AttributeError:
            created_iso = None
    return DocMetadata(
        title=title,
        author=author,
        created_date=created_iso,
        language=None,
        page_count=None,
    )


def _clean_string(value: Any) -> str | None:
    """Coerce a metadata value to a non-empty string or ``None``."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _confidence_heuristic(markdown: str, raw_byte_count: int) -> float:
    """Byte-recovery ratio (chars-out / bytes-in), capped at 1.0.

    A clean Word document with headings + body returns >0.01; an
    empty / unparseable document returns ~0. The float is surfaced
    for observability; the orchestrator's escalation decision is
    made by :meth:`quality_ok`, not by this ratio.
    """
    if raw_byte_count <= 0:
        return 0.0
    return min(len(markdown) / raw_byte_count, 1.0)


class DocxExtractor:
    """:class:`Extractor` impl that delegates Word parsing to python-docx.

    The instance carries the :data:`version` declared in the package
    ``__init__`` so the value flows from one canonical declaration
    site (F40) through to ``documents_media.extractor_version`` on
    every produced document. Re-extraction sweeps trigger off a
    version diff per spec §5.6.

    Test seam: the constructor accepts ``document_opener=`` so a
    contract / unit test can pass a synthetic opener returning an
    in-memory fake document without monkeypatching :mod:`docx`
    (F1-clean).

    Side channel for the track-changes flag — :data:`DocMetadata` is
    a frozen dataclass with a fixed shape, so the boolean lives on
    the extractor instance as ``last_extract_had_tracked_changes``
    and is updated each :meth:`extract` call. Callers that need the
    flag inspect it immediately after ``extract`` returns; downstream
    persistence wiring will surface the same boolean through the
    Silver layer once OF-2 follow-up extends ``documents_media`` with
    a ``has_tracked_changes`` column.
    """

    def __init__(
        self,
        *,
        version: str,
        document_opener: Callable[[], DocxOpener] = _default_document_opener,
    ) -> None:
        """Construct the extractor with explicit ``version`` + opener factory.

        ``document_opener`` is a factory returning the callable that
        opens a path-backed docx — defaults to the upstream
        ``docx.Document`` constructor wrapped in an ImportError-mapping
        shim. Tests pass a lambda returning a fake opener.
        """
        self.name: str = PLUGIN_NAME
        self.version: str = version
        self._document_opener_factory = document_opener
        self.last_extract_had_tracked_changes: bool = False

    def can_extract(self, mime: MimeType, magic_bytes: bytes) -> bool:
        """``True`` for the docx mime, OR PK magic + mime ending with "document".

        The mime hint is the primary signal — a well-formed
        Content-Type header dispatches directly. The magic-byte
        sniff catches a docx served with ``application/octet-stream``
        only when the caller's mime hint still ends with "document"
        (so a bare ZIP archive does not collide with this extractor).
        """
        if isinstance(mime, str) and mime == _DOCX_MIME:
            return True
        if magic_bytes.startswith(_MAGIC_ZIP) and isinstance(mime, str) and mime.endswith("document"):
            return True
        return False

    def extract(self, raw: bytes, _mime: MimeType) -> ExtractedDocument:
        """Open ``raw`` via python-docx and build the :class:`ExtractedDocument`.

        Heading-hierarchy is preserved (H1 → ``#``, H2 → ``##``,
        H3 → ``###``). Bullet / numbered lists render as ``-`` and
        ``1.`` items respectively. Tables render below the body
        paragraphs in document order. Track-changes are accepted in
        place — ``<w:ins>`` content stays, ``<w:del>`` content is
        skipped — and the boolean ``has_tracked_changes`` is recorded
        on the extractor instance for the caller to consume.

        ``_mime`` is ``_``-prefixed (F19) because :meth:`can_extract`
        already filtered — the value carries no extra information
        post-dispatch.

        ``confidence`` is the byte-recovery heuristic per the spec —
        chars-out / bytes-in, capped at 1.0. The orchestrator
        consults :meth:`quality_ok` to decide on escalation; the
        float is surfaced for observability and downstream signal-
        extraction.
        """
        opener = self._document_opener_factory()
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(raw)
        try:
            document = opener(str(tmp_path))
            markdown, has_changes = _render_document(document)
            metadata = _doc_metadata(document)
        finally:
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover — best-effort cleanup
                pass
        self.last_extract_had_tracked_changes = has_changes
        confidence = _confidence_heuristic(markdown, len(raw))
        return ExtractedDocument(
            markdown=markdown,
            pages=(),
            images=(),
            metadata=metadata,
            confidence=confidence,
        )

    def quality_ok(self, doc: ExtractedDocument) -> bool:
        """Escalation gate per spec §10.

        Returns ``True`` only when:

          * the extracted markdown has at least
            :data:`_QUALITY_MIN_CHARS` characters of content, AND
          * the markdown contains at least one heading line — the
            OF-2 heuristic that the document carried real structure.

        An empty or pathological docx (no headings, ~0 body) clears
        neither condition; the orchestrator routes those documents
        back to the dispatch chain. A ``False`` here is a soft
        escalation signal, not a hard error.
        """
        if len(doc.markdown) < _QUALITY_MIN_CHARS:
            return False
        return _has_any_heading(doc.markdown)

    def metadata_for(self, _raw: bytes, _mime: MimeType) -> SourceMetadata:
        """Return empty :class:`SourceMetadata`.

        ADR-021 (Wave E.5): docx core-property extraction (author /
        last_modified_by / created / keywords) lands in a follow-up
        commit that reads ``docProps/core.xml`` directly. Stub keeps
        the Protocol surface satisfied.
        """
        return SourceMetadata()


def _render_document(document: _DocxDocument) -> tuple[str, bool]:
    """Walk paragraphs + tables; return ``(markdown, has_tracked_changes)``.

    Free function to keep :meth:`DocxExtractor.extract` under the F16
    cognitive-complexity ceiling — the document-walk logic is
    independent of extractor state and benefits from being unit-
    testable in isolation.
    """
    body_xml = _safe_body_xml(document)
    has_changes = _has_tracked_changes(body_xml)
    lines: list[str] = []
    for paragraph in getattr(document, "paragraphs", []):
        style_name = _paragraph_style_name(paragraph)
        text = _paragraph_accepted_text(paragraph) if has_changes else (getattr(paragraph, "text", "") or "")
        rendered = _paragraph_to_markdown(style_name, text)
        if rendered:
            lines.append(rendered)
    for table in getattr(document, "tables", []):
        rows = _table_to_rows(table)
        rendered_table = _table_to_markdown(rows)
        if rendered_table:
            lines.append(rendered_table)
    markdown = "\n\n".join(lines)
    return markdown, has_changes


def _paragraph_style_name(paragraph: Any) -> str:
    """Return the paragraph's style name (e.g. ``"Heading 1"``).

    Falls back to ``"Normal"`` when the style or its name is missing
    — a robust default that routes the paragraph through the
    plain-body branch of :func:`_paragraph_to_markdown`.
    """
    style = getattr(paragraph, "style", None)
    if style is None:
        return "Normal"
    name = getattr(style, "name", None)
    if not isinstance(name, str):
        return "Normal"
    return name


def _safe_body_xml(document: _DocxDocument) -> str:
    """Return the body XML as a string, or empty if not available.

    python-docx exposes ``document.element.body.xml`` as the raw
    serialised XML. Some fakes don't carry the chain; we degrade to
    empty string so the track-changes scan returns ``False``.
    """
    element = getattr(document, "element", None)
    if element is None:
        return ""
    body = getattr(element, "body", None)
    if body is None:
        return ""
    xml = getattr(body, "xml", None)
    if not isinstance(xml, str):
        return ""
    return xml
