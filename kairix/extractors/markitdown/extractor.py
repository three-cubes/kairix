"""Markitdown-backed extractor for PDF / DOCX / PPTX / XLSX / HTML.

Wraps the `markitdown <https://github.com/microsoft/markitdown>`_
library (MIT-licensed, Microsoft) and adapts its
``DocumentConverterResult`` to the kairix
:class:`kairix.extractors.Extractor` Protocol. Markitdown is the
default extractor for the rich-document formats Wave 2 ingests; the
escalation chain (markitdown → pdf_fallback → ocr → vision) sits
above this plugin once Waves 3-4 ship their members.

Spec ref: ``docs/architecture/connector-ingestion-architecture.md``
§2 ("extractors tree"), §3 ("Extractor Protocol"), §10 (Wave 2 IM-4),
and §4 ("Three failures map to three behaviours") for the
``quality_ok`` escalation gate.
"""

from __future__ import annotations

import logging
import tempfile
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from kairix.core.protocols import SourceMetadata
from kairix.extractors import (
    DocMetadata,
    ExtractedDocument,
    MimeType,
)

# Silence noisy third-party informational logging at module import time.
# Markitdown drives openpyxl (xlsx), python-docx, python-pptx, and
# pdfminer (via its PDF converter); each emits per-document warnings
# for unsupported features that kairix doesn't extract anyway. The
# noise floods worker logs during SharePoint backfills without changing
# behaviour. ERROR keeps real load failures visible.
for _noisy in ("pdfminer", "openpyxl", "pdfminer.psparser", "pdfminer.pdfinterp"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)
warnings.filterwarnings("ignore", module="openpyxl.*")

#: Canonical plugin name surfaced by the extractor registry.
PLUGIN_NAME = "markitdown"

#: Minimum decoded-markdown length the plugin treats as "quality ok"
#: per spec §10 (Wave 3 escalation notes). Anything shorter is treated
#: as a parse failure and escalates to ``pdf_fallback`` / ``ocr``.
_QUALITY_MIN_CHARS = 50

#: Minimum byte-recovery ratio (decoded markdown chars / raw bytes)
#: the plugin treats as "quality ok". Markitdown on a scanned-PDF
#: (image-only) typically returns ~0% of the byte count; the
#: ``ocr`` plugin recovers those documents on escalation. The 10%
#: floor is the spec §10 wave-3 escalation gate.
_QUALITY_MIN_BYTE_RATIO = 0.10

#: Mime types markitdown handles natively (per the library's
#: README + ``_converters/`` directory listing). Operators routing
#: novel mime types add them here; the magic-byte sniff below catches
#: the common case of a missing / wrong server-declared mime.
_MARKITDOWN_MIMES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/html",
        "application/xhtml+xml",
    }
)

#: File extensions per mime — used to seed the tmp file we hand to
#: markitdown, because the library sniffs the file format from the
#: extension first and only falls back to magika magic-bytes if the
#: extension is missing or unrecognised.
_MIME_TO_EXTENSION: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/html": ".html",
    "application/xhtml+xml": ".xhtml",
}

#: PDF magic header (``%PDF``).
_MAGIC_PDF = b"%PDF"
#: ZIP-archive header — DOCX / PPTX / XLSX all wrap content in a ZIP
#: (Office Open XML). We can't distinguish them from the leading
#: bytes alone, but ``PK\x03\x04`` lets us recognise the family and
#: defer the final dispatch to the mime hint.
_MAGIC_ZIP = b"PK\x03\x04"


class _MarkitdownConverter(Protocol):
    """Wire-shape Protocol for the upstream ``MarkItDown`` class.

    Markitdown's runtime API is ``MarkItDown().convert(<path>)`` →
    object with ``.markdown`` / ``.text_content`` / ``.title``. We
    declare the Protocol here so a test can pass an in-memory fake
    without monkeypatching the upstream module (F1-clean).
    """

    def convert(self, source: Any, **kwargs: Any) -> Any:
        """Convert a source (path / stream) into a result with ``.markdown``."""


def _default_converter_factory() -> _MarkitdownConverter:
    """Lazy-import the upstream :class:`markitdown.MarkItDown` class.

    Markitdown is declared as an *optional* dependency in
    ``pyproject.toml`` — operators not ingesting rich documents skip
    the install. Resolving the import inside the factory means
    ``import kairix.extractors.markitdown`` succeeds in environments
    without the upstream library; the ``ImportError`` only fires when
    ``make_extractor()`` is actually called.
    """
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise RuntimeError(
            "markitdown: the upstream 'markitdown' package is not installed. "
            "fix: pip install 'Kairix-agentic-knowledge-mgt[markitdown]' "
            "to opt into the rich-document extractor. "
            "next: re-run the connector sync; markitdown will then resolve."
        ) from exc
    return cast("_MarkitdownConverter", MarkItDown())


class MarkitdownExtractor:
    """:class:`Extractor` impl that delegates to the markitdown library.

    The instance carries the :data:`version` declared in the package
    ``__init__`` so the value flows from one canonical declaration
    site (F40) through to ``documents_media.extractor_version`` on
    every produced document. Re-extraction sweeps trigger off a
    version diff per spec §5.6.

    Test seam: the constructor accepts ``converter_factory=`` so a
    contract / unit test passes a synthetic converter without
    monkeypatching :mod:`markitdown` (F1-clean).
    """

    def __init__(
        self,
        *,
        version: str,
        converter_factory: Callable[[], _MarkitdownConverter] = _default_converter_factory,
        scratch_dir: Path | None = None,
    ) -> None:
        """Construct the extractor with explicit ``version`` + factory.

        ``converter_factory`` defaults to the upstream
        ``markitdown.MarkItDown`` constructor wrapped in an
        ImportError-mapping shim; tests pass a lambda returning a
        fake converter.

        ``scratch_dir`` controls where the per-extract temp file lives.
        Default ``None`` lets :mod:`tempfile` pick the platform default
        (``$TMPDIR`` / ``/tmp``). Tests pass an explicit ``tmp_path`` so
        the cleanup-on-failure regression tests can list the directory
        before and after :meth:`extract` to prove no placeholder
        leaks. F6-clean seam: production callers omit ``scratch_dir``
        and get the platform default.
        """
        self.name: str = PLUGIN_NAME
        self.version: str = version
        self._converter_factory = converter_factory
        self._scratch_dir = scratch_dir

    def can_extract(self, mime: MimeType, magic_bytes: bytes) -> bool:
        """``True`` for any mime markitdown handles, with a magic-byte fallback.

        The mime hint is the primary signal — operators routing a
        well-formed Content-Type header want the dispatch to match
        the declared format. If the mime is missing / generic
        (``application/octet-stream``), the magic-byte sniff catches
        PDF (``%PDF``) and ZIP-family Office Open XML (``PK\\x03\\x04``).
        ``application/zip`` alone is NOT claimed — without the mime
        hint we can't tell DOCX from any other zip-wrapped file.
        """
        if isinstance(mime, str) and mime in _MARKITDOWN_MIMES:
            return True
        if magic_bytes.startswith(_MAGIC_PDF):
            return True
        # ZIP-family: only claim when the mime hint disambiguates.
        return bool(magic_bytes.startswith(_MAGIC_ZIP) and isinstance(mime, str) and mime in _MARKITDOWN_MIMES)

    def extract(self, raw: bytes, mime: MimeType) -> ExtractedDocument:
        """Write ``raw`` to a tmp file and invoke markitdown.

        Markitdown's ``convert`` accepts a path or a binary stream;
        we use a path because the library's format sniffing inspects
        the file extension first. We seed the tmp file with the
        mime-mapped extension so a PDF served as
        ``application/octet-stream`` still dispatches correctly.

        Confidence is heuristic per the spec — full text length and
        whether a table-like structure (``|`` columns) is present
        drive the value. The orchestrator consults
        :meth:`quality_ok` to decide on escalation; the float is
        surfaced for observability and downstream signal-extraction
        only.
        """
        suffix = _MIME_TO_EXTENSION.get(mime, "")
        # tempfile.NamedTemporaryFile creates the file on disk BEFORE
        # we get a path back, so a write() that fails (e.g. ENOSPC on
        # a full tmpfs) leaves an empty placeholder on disk if
        # delete=False is used. We need delete=False because some
        # platforms (Windows) refuse to re-open a still-open
        # NamedTemporaryFile by path, but the cleanup must wrap the
        # write step too. The outer try/finally below covers BOTH the
        # write-to-tmp step and the convert step so a failure at
        # either point unlinks the file rather than orphaning it.
        # Without this, a 2GB pathological PPTX expansion fills tmpfs
        # and every subsequent extraction fails with ENOSPC on the
        # write step itself (observed 2026-05-26 on the dogfood VM:
        # 8,087 zero-byte tmpfile stubs + one 2GB orphan).
        scratch_dir = str(self._scratch_dir) if self._scratch_dir is not None else None
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=scratch_dir) as tmp:
            tmp_path = Path(tmp.name)
        try:
            tmp_path.write_bytes(raw)
            converter = self._converter_factory()
            result = converter.convert(str(tmp_path))
            markdown = _result_markdown(result)
            title = _result_title(result)
        finally:
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover — best-effort cleanup
                pass
        confidence = _confidence_heuristic(markdown, raw)
        return ExtractedDocument(
            markdown=markdown,
            pages=(),
            images=(),
            metadata=DocMetadata(
                title=title,
                author=None,
                created_date=None,
                language=None,
                page_count=None,
            ),
            confidence=confidence,
        )

    def quality_ok(self, doc: ExtractedDocument) -> bool:
        """Escalation gate per spec §10 (Wave 3 notes).

        Returns ``True`` only when:

          * the extracted markdown has at least
            :data:`_QUALITY_MIN_CHARS` characters of content, AND
          * the byte-recovery ratio (chars-out / bytes-in proxy via
            the embedded ``confidence`` heuristic) is at least
            :data:`_QUALITY_MIN_BYTE_RATIO`.

        Markitdown returns near-empty output on scanned PDFs (image-
        only); the orchestrator's escalation chain hands those to
        ``ocr`` once Wave 3 lands. A False here is a soft escalation
        signal, not a hard error.
        """
        text = doc.markdown.strip()
        if len(text) < _QUALITY_MIN_CHARS:
            return False
        return doc.confidence >= _QUALITY_MIN_BYTE_RATIO

    def metadata_for(self, _raw: bytes, _mime: MimeType) -> SourceMetadata:
        """Return empty :class:`SourceMetadata`.

        ADR-021 (Wave E.5): Office core-property + PDF XMP extraction
        lands in a follow-up commit (the markitdown library does not
        expose them in a single round-trip; a separate openpyxl /
        python-docx / pypdf-XMP pass is needed). The stub keeps the
        Protocol surface satisfied so the plugin remains shippable.
        """
        return SourceMetadata()


def _result_markdown(result: Any) -> str:
    """Extract the markdown string from a markitdown result.

    Markitdown 0.1.x exposes ``.markdown`` as the canonical attribute
    and ``.text_content`` as a soft-deprecated alias. We prefer the
    new name, fall back to the alias, and finally degrade to ``str``
    to stay compatible with older library minor versions during a
    transition window.
    """
    for attr in ("markdown", "text_content"):
        value = getattr(result, attr, None)
        if isinstance(value, str):
            return value
    return str(result)


def _result_title(result: Any) -> str | None:
    """Pull the optional ``title`` from a markitdown result, if present."""
    value = getattr(result, "title", None)
    return value if isinstance(value, str) and value.strip() else None


def _confidence_heuristic(markdown: str, raw: bytes) -> float:
    """Cheap "did markitdown actually recover content" signal.

    Returns the byte-recovery ratio (chars-out / bytes-in), capped
    at 1.0. A scanned PDF (image-only) returns ~0; a clean text-only
    PDF / DOCX often returns >0.5. A small bonus for table-like rows
    (``|`` columns) leans the heuristic toward markitdown's strength
    (structured table preservation).
    """
    if not raw:
        return 0.0
    ratio = len(markdown) / len(raw)
    if "|" in markdown and markdown.count("\n") >= 2:
        ratio += 0.05
    return min(ratio, 1.0)
