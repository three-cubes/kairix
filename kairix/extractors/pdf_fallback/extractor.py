"""pdfplumber-backed PDF fallback extractor (MM-1, Wave 3).

Catches PDFs that the default markitdown extractor recovers poorly.
Markitdown's PDF support has known weaknesses with complex tables
(multi-row headers, merged cells) and scanned / image-only documents;
``pdf_fallback`` runs the document through `pdfplumber
<https://github.com/jsvine/pdfplumber>`_ (MIT-licensed, commercial-
safe) which preserves table structure and produces per-page
:class:`Page` value objects so chunks can cite back to a specific
page (MM-3 builds on the per-page surface).

The plugin is the second hop in the escalation chain wired in spec
§10 (Wave 3 notes)::

    markitdown   (default)
        ↓ quality_ok = False
    pdf_fallback (this plugin — MIT, table-aware)
        ↓ quality_ok = False
    ocr          (MM-2 — Tesseract for image-only PDFs)
        ↓ quality_ok = False
    dead-letter

**Licence decision** (Decision 4): pdfplumber's MIT licence makes the
plugin commercial-safe by default. The alternative pymupdf wraps MuPDF
which is AGPL-licensed; shipping pymupdf would force kairix's
distribution into AGPL territory, which is a no-go for the operator
deployments in scope. pymupdf is **not** shipped. See
``docs/architecture/connector-ingestion-architecture.md`` §10 for the
full ruling.

Spec ref: ``docs/architecture/connector-ingestion-architecture.md``
§2 ("extractors tree"), §3 ("Extractor Protocol"), §4 ("Three failures
map to three behaviours") for ``quality_ok`` semantics, and §10
(Wave 3 MM-1) for the ship plan.
"""

from __future__ import annotations

import logging
import re
import tempfile
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from kairix.core.protocols import SourceMetadata
from kairix.extractors import (
    DocMetadata,
    ExtractedDocument,
    MimeType,
    Page,
)

# Silence pdfminer's informational chatter at module import time. The
# library emits a WARNING for every PDF whose /Encrypt /P bit-5 flag
# declares "no text extraction" (Microsoft partner content, Adobe
# Restrict-Editing exports, vendor whitepapers), plus codec-fallback
# and font-substitution notes. We already ignore the extraction-denied
# flag by design — pdfplumber's default behaviour is to extract anyway —
# and the warning floods the worker logs without changing behaviour.
# Lifting the level to ERROR keeps real failures visible while dropping
# the per-PDF noise.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

#: Canonical plugin name surfaced by the extractor registry.
PLUGIN_NAME = "pdf_fallback"

#: Minimum decoded-markdown length the plugin treats as "quality ok".
#: Below this, the orchestrator escalates to OCR. Scanned (image-only)
#: PDFs typically return ~0 chars from pdfplumber's text layer — the
#: ``ocr`` plugin recovers those documents on escalation. The 100-char
#: floor is the Wave-3 §10 escalation gate.
_QUALITY_MIN_CHARS = 100

#: PDF magic header (``%PDF``).
_MAGIC_PDF = b"%PDF"

#: Mime types the plugin claims natively. A PDF served as
#: ``application/octet-stream`` falls through to the magic-byte sniff.
_PDF_MIMES = frozenset({"application/pdf"})


class _PdfPage(Protocol):
    """Wire-shape Protocol for the ``pdfplumber.Page`` runtime API.

    Declared locally so a unit test can pass an in-memory fake without
    monkeypatching the upstream module (F1-clean). The real
    ``pdfplumber.Page`` carries the same surface plus a long tail of
    layout helpers we don't consult here.
    """

    def extract_text(self) -> str | None:
        """Return the page's text layer (or ``None`` if empty)."""

    def extract_tables(self) -> list[list[list[str | None]]]:
        """Return a list of table grids (rows of cells)."""


class _PdfDocument(Protocol):
    """Wire-shape Protocol for the upstream ``pdfplumber.PDF`` object.

    The runtime API is ``pdfplumber.open(<path>)`` → context-managed
    PDF with ``.pages`` iterable and ``.metadata`` dict.
    """

    @property
    def pages(self) -> list[_PdfPage]:
        """Sequence of page objects."""

    @property
    def metadata(self) -> dict[str, Any]:
        """Document-level metadata (Title, Author, CreationDate, …)."""

    def __enter__(self) -> _PdfDocument:
        """Enter the context manager and return the PDF."""

    def __exit__(self, *args: Any) -> None:
        """Exit the context manager and release file handles."""


#: Type of the factory callable that opens a PDF file on disk. Takes
#: the path string, returns a context-managed :class:`_PdfDocument`.
PdfOpener = Callable[[str], _PdfDocument]


def _default_pdf_opener() -> PdfOpener:
    """Lazy-import the upstream :func:`pdfplumber.open` callable.

    pdfplumber is declared as an *optional* dependency in
    ``pyproject.toml`` (extra ``pdf_fallback``) — operators ingesting
    only markdown / plain-text content skip the install. Resolving the
    import inside the factory means
    ``import kairix.extractors.pdf_fallback`` succeeds in environments
    without the upstream library; the ``RuntimeError`` only fires when
    ``extract()`` is actually called.

    Markitdown's ``[pdf]`` extra already pulls pdfplumber, so operators
    on the default rich-document extra get the fallback for free.

    A thin adapter narrows pdfplumber's permissive runtime signature
    (which accepts paths, streams, and a dozen keyword options) to the
    single-string interface this plugin uses, so the typed
    :class:`PdfOpener` Protocol stays accurate.
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError(
            "pdf_fallback: the upstream 'pdfplumber' package is not installed. "
            "fix: pip install 'Kairix-agentic-knowledge-mgt[pdf_fallback]' "
            "to opt into the PDF fallback extractor (MIT licence). "
            "next: re-run the connector sync; pdf_fallback will then resolve."
        ) from exc

    def _open_path(path: str) -> _PdfDocument:
        # The runtime ``pdfplumber.PDF`` object satisfies the
        # :class:`_PdfDocument` Protocol structurally (``pages``,
        # ``metadata``, ``__enter__`` / ``__exit__``).
        return cast("_PdfDocument", pdfplumber.open(path))

    return _open_path


def _table_row_to_markdown(row: Iterable[str | None]) -> str:
    """Render one table row as a pipe-delimited markdown row.

    ``None`` cells (empty / unparseable) are rendered as the empty
    string so column alignment stays consistent.
    """
    cells = ["" if cell is None else str(cell).strip().replace("\n", " ") for cell in row]
    return "| " + " | ".join(cells) + " |"


def _table_to_markdown(table: list[list[str | None]]) -> str:
    """Render a pdfplumber table grid as a pipe-syntax markdown table.

    pdfplumber returns ``list[list[str | None]]`` — one row per inner
    list, one cell per element. The first row is treated as the
    header; a ``| --- |`` separator row is injected between header
    and body so the result is a valid GitHub-Flavored Markdown table.

    Empty tables (no rows) produce an empty string.
    """
    if not table:
        return ""
    rows = [_table_row_to_markdown(row) for row in table]
    if not rows:
        return ""
    header = rows[0]
    separator_cells = ["---"] * len(table[0])
    separator = "| " + " | ".join(separator_cells) + " |"
    body = rows[1:]
    return "\n".join([header, separator, *body])


def _render_page_markdown(text: str, tables: list[list[list[str | None]]]) -> str:
    """Combine a page's text and rendered tables into a markdown blob."""
    chunks: list[str] = []
    if text.strip():
        chunks.append(text.strip())
    for table in tables:
        rendered = _table_to_markdown(table)
        if rendered:
            chunks.append(rendered)
    return "\n\n".join(chunks)


def _build_page(page_number: int, page: _PdfPage) -> tuple[Page, str]:
    """Extract text + tables from one pdfplumber page.

    Returns the value-object :class:`Page` plus the rendered markdown
    blob for combining into the document-level markdown stream.
    pdfplumber's ``extract_text`` returns ``None`` for image-only
    pages — we coerce to empty string so callers don't have to handle
    the optional.
    """
    raw_text = page.extract_text() or ""
    tables = page.extract_tables() or []
    rendered = _render_page_markdown(raw_text, tables)
    return (
        Page(
            page_number=page_number,
            text=raw_text,
            has_images=False,
        ),
        rendered,
    )


def _pdf_metadata_to_doc_metadata(metadata: dict[str, Any], page_count: int) -> DocMetadata:
    """Adapt the pdfplumber metadata dict to :class:`DocMetadata`.

    pdfplumber surfaces the raw PDF info dictionary (Title, Author,
    CreationDate, Subject, Producer, …). We extract the four fields
    the kairix value object models — anything else stays in the raw
    bytes the Bronze layer preserves.
    """
    return DocMetadata(
        title=_clean_string(metadata.get("Title")),
        author=_clean_string(metadata.get("Author")),
        created_date=_pdf_date_to_iso(_clean_string(metadata.get("CreationDate"))),
        language=None,
        page_count=page_count,
    )


#: PDF date string (PDF 32000-1 §7.9.4): ``D:YYYYMMDDHHmmSSOHH'mm'`` where every
#: field after the year is optional and O is ``+``, ``-`` or ``Z``. Fixed-width
#: digit groups keep the match linear.
_PDF_DATE = re.compile(
    r"(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(?:([Zz+-])(?:(\d{2})'?(?:(\d{2})'?)?)?)?"
)


def _pdf_date_to_iso(raw: str | None) -> str | None:
    """Convert a PDF date string to ISO-8601, matching the docx / pptx extractors.

    Missing month/day default to 1 and missing time fields to 0. A ``Z`` or an
    explicit offset yields an aware datetime; no offset stays naive. Anything
    that isn't a valid PDF date returns ``None`` rather than leaking the raw
    string into the ``created_date`` column.
    """
    if raw is None:
        return None
    match = _PDF_DATE.fullmatch(raw)
    if match is None:
        return None
    year, month, day, hour, minute, second, sign, off_h, off_m = match.groups()
    tz: timezone | None = None
    if sign in ("Z", "z"):
        tz = timezone.utc
    elif sign is not None:
        offset = timedelta(hours=int(off_h or 0), minutes=int(off_m or 0))
        tz = timezone(-offset if sign == "-" else offset)
    try:
        parsed = datetime(
            int(year),
            int(month or 1),
            int(day or 1),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            tzinfo=tz,
        )
    except ValueError:
        return None
    return parsed.isoformat()


def _clean_string(value: Any) -> str | None:
    """Coerce a metadata value to a non-empty string or ``None``."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _confidence_heuristic(markdown: str, raw_byte_count: int) -> float:
    """Byte-recovery ratio (chars-out / bytes-in), capped at 1.0.

    A clean text-bearing PDF returns >0.05; an image-only PDF returns
    ~0. The float is surfaced for observability and is consumed by
    downstream signal-extraction; the orchestrator's escalation
    decision is made by :meth:`quality_ok`, not by this ratio.
    """
    if raw_byte_count <= 0:
        return 0.0
    return min(len(markdown) / raw_byte_count, 1.0)


class PdfFallbackExtractor:
    """:class:`Extractor` impl that delegates PDF parsing to pdfplumber.

    The instance carries the :data:`version` declared in the package
    ``__init__`` so the value flows from one canonical declaration
    site (F40) through to ``documents_media.extractor_version`` on
    every produced document. Re-extraction sweeps trigger off a
    version diff per spec §5.6.

    Test seam: the constructor accepts ``pdf_opener=`` so a contract
    / unit test can pass a synthetic opener returning an in-memory
    fake PDF without monkeypatching :mod:`pdfplumber` (F1-clean).
    """

    def __init__(
        self,
        *,
        version: str,
        pdf_opener: Callable[[], PdfOpener] = _default_pdf_opener,
    ) -> None:
        """Construct the extractor with explicit ``version`` + opener factory.

        ``pdf_opener`` is a factory returning the callable that opens a
        path-backed PDF — defaults to the upstream ``pdfplumber.open``
        wrapped in an ImportError-mapping shim. Tests pass a lambda
        returning a fake opener.
        """
        self.name: str = PLUGIN_NAME
        self.version: str = version
        self._pdf_opener_factory = pdf_opener

    def can_extract(self, mime: MimeType, magic_bytes: bytes) -> bool:
        """``True`` for ``application/pdf`` OR ``%PDF`` magic bytes.

        The mime hint is the primary signal; the magic-byte sniff
        catches a PDF served with a generic Content-Type. Together
        they cover both well-formed and lazily-typed sources.
        """
        if isinstance(mime, str) and mime in _PDF_MIMES:
            return True
        return magic_bytes[:4] == _MAGIC_PDF

    def extract(self, raw: bytes, _mime: MimeType) -> ExtractedDocument:
        """Open ``raw`` via pdfplumber and build the :class:`ExtractedDocument`.

        pdfplumber's ``open`` accepts a path or stream; we use a path
        because some PDF parsers expect random-access seek and a tmp
        file gives the cleanest behaviour across operating systems.
        The temp file is deleted in a ``finally`` block so a crash
        mid-extract still leaves /tmp clean.

        ``_mime`` is ``_``-prefixed (F19) because :meth:`can_extract`
        already filtered — the value carries no extra information
        post-dispatch.

        ``confidence`` is the byte-recovery heuristic per the spec —
        chars-out / bytes-in, capped at 1.0. The orchestrator
        consults :meth:`quality_ok` to decide on escalation; the
        float is surfaced for observability and downstream signal-
        extraction.
        """
        opener = self._pdf_opener_factory()
        # tempfile.NamedTemporaryFile cleans up on context exit; we
        # pass delete=False because some platforms (Windows) refuse
        # to re-open a still-open NamedTemporaryFile by path.
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(raw)
        try:
            with opener(str(tmp_path)) as pdf:
                pages, page_markdowns = _extract_pages(pdf.pages)
                metadata = _pdf_metadata_to_doc_metadata(dict(pdf.metadata or {}), len(pages))
        finally:
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover — best-effort cleanup
                pass
        combined_markdown = "\n\n".join(blob for blob in page_markdowns if blob)
        confidence = _confidence_heuristic(combined_markdown, len(raw))
        return ExtractedDocument(
            markdown=combined_markdown,
            pages=tuple(pages),
            images=(),
            metadata=metadata,
            confidence=confidence,
        )

    def quality_ok(self, doc: ExtractedDocument) -> bool:
        """Escalation gate per spec §10 (Wave 3 notes).

        Returns ``True`` only when:

          * the extracted markdown has at least
            :data:`_QUALITY_MIN_CHARS` characters of content, AND
          * at least one page carries non-empty text.

        A scanned (image-only) PDF clears neither condition — every
        page's text layer is empty and the markdown blob is too
        small. The orchestrator routes those documents to ``ocr``
        once MM-2 lands. A ``False`` here is a soft escalation
        signal, not a hard error.
        """
        if len(doc.markdown) < _QUALITY_MIN_CHARS:
            return False
        return any(page.text.strip() for page in doc.pages)

    def metadata_for(self, _raw: bytes, _mime: MimeType) -> SourceMetadata:
        """Return empty :class:`SourceMetadata`.

        ADR-021 (Wave E.5): PDF XMP / Info-dict extraction (Author /
        Title / Keywords / CreationDate) lands in a follow-up commit
        that reads the document trailer dictionary directly. Stub
        keeps the Protocol surface satisfied.
        """
        return SourceMetadata()


def _extract_pages(
    pages: Iterable[_PdfPage],
) -> tuple[list[Page], list[str]]:
    """Walk every page and return the value-objects + rendered markdown.

    Extracted as a free function to keep :meth:`PdfFallbackExtractor.extract`
    under the F16 cognitive-complexity ceiling — the loop logic is
    independent of the extractor state and benefits from being unit-
    testable in isolation.
    """
    value_objects: list[Page] = []
    markdowns: list[str] = []
    for index, page in enumerate(pages, start=1):
        page_obj, rendered = _build_page(index, page)
        value_objects.append(page_obj)
        markdowns.append(rendered)
    return value_objects, markdowns
