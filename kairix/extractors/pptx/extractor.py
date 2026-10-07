"""``python-pptx``-backed slide-aware extractor (Wave 4 OF-1).

Markitdown handles PPTX but flattens the slide structure into a single
markdown stream — fine for plain retrieval, lossy for the "cite the
specific slide" / "surface the speaker notes" journeys. This plugin
preserves both:

  * per-slide :class:`kairix.core.protocols.Page` value objects so
    chunks can cite back to slide number, and
  * speaker notes lifted into the markdown body as block-quoted text
    under the slide they belong to.

Spec ref: ``docs/architecture/connector-ingestion-architecture.md``
§10 (Wave 4 OF-1) and KFEAT-012 Phase 2 §PowerPoint.

The escalation contract is unchanged: markitdown is the default; when
the orchestrator's ``quality_ok`` gate fails OR the caller explicitly
asks for slide-level granularity, the registry routes to this plugin
instead.
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
    Page,
)

#: Canonical plugin name surfaced by the extractor registry.
PLUGIN_NAME = "pptx"

#: Office Open XML mime for ``.pptx`` presentations.
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

#: ZIP-archive magic header — every ``.pptx`` (Office Open XML) is a
#: ZIP wrapper. We claim files whose first four bytes match AND whose
#: declared mime hints at a presentation; bare ZIP alone is ambiguous.
_MAGIC_ZIP = b"PK\x03\x04"

#: Quality-gate floor — a useful PPTX extract returns >= 100 chars of
#: combined slide markdown. Anything shorter is escalated.
_QUALITY_MIN_CHARS = 100


class _PptxPresentation(Protocol):
    """Wire-shape Protocol for the upstream ``pptx.Presentation`` result.

    We declare the Protocol here so a test can pass an in-memory fake
    presentation without monkeypatching the upstream module (F1-clean).
    The real ``pptx.Presentation(<path>)`` returns an object exposing
    ``.slides`` and ``.core_properties``; both are read-only attributes
    so the Protocol declares them as read-only ``@property``-shaped
    members (variance-compatible with the upstream ``Presentation``
    class's slot-backed read-only attributes).
    """

    @property
    def slides(self) -> Any:
        """Iterable of slide objects — Protocol member, real impl provides."""

    @property
    def core_properties(self) -> Any:
        """Document core properties (Title / Author / Created) — Protocol member."""


class PptxExtractor:
    """Slide-aware :class:`Extractor` for ``.pptx`` files.

    The instance carries the :data:`version` declared in the package
    ``__init__`` so the value flows from one canonical declaration site
    (F40) through to ``documents_media.extractor_version`` on every
    produced document. Re-extraction sweeps trigger off a version diff
    per spec §5.6.

    Test seam: the constructor accepts ``presentation_loader=`` so a
    contract / unit test passes a synthetic presentation loader without
    monkeypatching :mod:`pptx` (F1-clean).
    """

    def __init__(
        self,
        *,
        version: str,
        presentation_loader: Callable[[str], _PptxPresentation] | None = None,
    ) -> None:
        """Construct the extractor with explicit ``version`` + loader.

        ``presentation_loader`` defaults to the upstream
        :func:`pptx.Presentation` constructor wrapped in an
        ImportError-mapping shim; tests pass a lambda returning a fake
        presentation.
        """
        self.name: str = PLUGIN_NAME
        self.version: str = version
        self._presentation_loader = presentation_loader or _default_presentation_loader

    def can_extract(self, mime: MimeType, magic_bytes: bytes) -> bool:
        """``True`` for the PPTX mime, or ZIP-magic + presentation-ish mime.

        The mime hint is the primary signal. The magic-byte sniff
        backstops the common case of a missing / generic mime — bare
        ``PK\\x03\\x04`` only counts when the mime hint *also* indicates
        a presentation (``mime.endswith("presentation")``); otherwise
        the file could be any of the ZIP-wrapped Office formats.
        """
        if isinstance(mime, str) and mime == _PPTX_MIME:
            return True
        if magic_bytes.startswith(_MAGIC_ZIP) and isinstance(mime, str) and mime.endswith("presentation"):
            return True
        return False

    def extract(self, raw: bytes, mime: MimeType) -> ExtractedDocument:
        """Write ``raw`` to a tmp file and walk the slide deck.

        ``mime`` is required by the :class:`Extractor` Protocol; this
        plugin only routes on the ``.pptx`` family so the value is not
        consulted directly (the suffix below is hard-coded).

        Returns an :class:`ExtractedDocument` whose ``markdown`` is the
        concatenated per-slide rendering (``## Slide <n>: <title>`` plus
        body text plus a ``> **Speaker notes**: ...`` blockquote when
        present) and whose ``pages`` carries one frozen :class:`Page`
        per slide.
        """
        _ = mime  # Mime is informational; this plugin only handles PPTX.
        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(raw)
        try:
            prs = self._presentation_loader(str(tmp_path))
            slides = _walk_slides(prs)
            metadata = _metadata_from(prs)
        finally:
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover — best-effort cleanup
                pass

        markdown = _slides_to_markdown(slides)
        pages = tuple(_slide_to_page(s) for s in slides)
        confidence = _confidence_heuristic(markdown, raw)

        return ExtractedDocument(
            markdown=markdown,
            pages=pages,
            images=(),
            metadata=metadata,
            confidence=confidence,
        )

    def quality_ok(self, doc: ExtractedDocument) -> bool:
        """Escalation gate per spec §10.

        Returns ``True`` only when the extracted markdown has at least
        :data:`_QUALITY_MIN_CHARS` characters AND at least one slide
        was extracted. A False here is a soft escalation signal, not a
        hard error — the orchestrator decides whether to retry with a
        different plugin (e.g. ``ocr`` for an image-heavy deck).
        """
        if len(doc.pages) == 0:
            return False
        return len(doc.markdown.strip()) >= _QUALITY_MIN_CHARS

    def metadata_for(self, _raw: bytes, _mime: MimeType) -> SourceMetadata:
        """Return empty :class:`SourceMetadata`.

        ADR-021 (Wave E.5): pptx core-property extraction lands in a
        follow-up commit reading ``docProps/core.xml`` from the zip.
        Stub keeps the Protocol surface satisfied.
        """
        return SourceMetadata()


class _Slide:
    """In-memory view of one extracted slide.

    Plain class (not a dataclass) because it carries mutable lists
    during construction; we convert to a frozen :class:`Page` once
    walking is done.
    """

    def __init__(self, index: int, title: str, body: str, notes: str, has_images: bool) -> None:
        self.index = index
        self.title = title
        self.body = body
        self.notes = notes
        self.has_images = has_images


def _default_presentation_loader(path: str) -> _PptxPresentation:
    """Lazy-import the upstream :mod:`pptx` package.

    ``python-pptx`` is declared as an *optional* dependency in
    ``pyproject.toml`` (extra ``pptx``). Resolving the import inside
    the loader means ``import kairix.extractors.pptx`` succeeds in
    environments without the upstream library; the ``ImportError``
    only fires when ``extract()`` is actually called.
    """
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise RuntimeError(
            "pptx: the upstream 'python-pptx' package is not installed. "
            "fix: pip install 'Kairix-agentic-knowledge-mgt[pptx]' "
            "to opt into the slide-aware extractor. "
            "next: re-run the connector sync; pptx will then resolve."
        ) from exc
    return cast("_PptxPresentation", Presentation(path))


def _walk_slides(prs: _PptxPresentation) -> list[_Slide]:
    """Walk ``prs.slides`` and lift title / body / notes / image-flag."""
    out: list[_Slide] = []
    for index, slide in enumerate(prs.slides):
        out.append(
            _Slide(
                index=index,
                title=_slide_title(slide),
                body=_slide_body(slide),
                notes=_slide_notes(slide),
                has_images=_slide_has_images(slide),
            )
        )
    return out


def _slide_title(slide: Any) -> str:
    """Return the slide's title text, or an empty string if absent."""
    shapes = getattr(slide, "shapes", None)
    title_shape = getattr(shapes, "title", None) if shapes is not None else None
    if title_shape is None:
        return ""
    text = getattr(title_shape, "text", "")
    return text.strip() if isinstance(text, str) else ""


def _slide_body(slide: Any) -> str:
    """Concatenate every non-title text run on the slide."""
    shapes = getattr(slide, "shapes", None)
    if shapes is None:
        return ""
    title_shape = getattr(shapes, "title", None)
    lines: list[str] = []
    for shape in shapes:
        if shape is title_shape:
            continue
        if not getattr(shape, "has_text_frame", False):
            continue
        text_frame = getattr(shape, "text_frame", None)
        text = getattr(text_frame, "text", "") if text_frame is not None else ""
        if isinstance(text, str) and text.strip():
            lines.append(text.strip())
    return "\n".join(lines)


def _slide_notes(slide: Any) -> str:
    """Return the speaker-notes text, or an empty string if absent."""
    notes_slide = getattr(slide, "notes_slide", None)
    if notes_slide is None:
        return ""
    text_frame = getattr(notes_slide, "notes_text_frame", None)
    if text_frame is None:
        return ""
    text = getattr(text_frame, "text", "")
    return text.strip() if isinstance(text, str) else ""


def _slide_has_images(slide: Any) -> bool:
    """``True`` if any shape on the slide is an image (shape_type 13)."""
    shapes = getattr(slide, "shapes", None)
    if shapes is None:
        return False
    for shape in shapes:
        # python-pptx exposes ``MSO_SHAPE_TYPE.PICTURE`` as integer 13.
        # We compare numerically to avoid importing the enum at module
        # load (keeps the upstream dependency lazy).
        shape_type = getattr(shape, "shape_type", None)
        if shape_type is not None and int(shape_type) == 13:
            return True
    return False


def _slide_to_page(slide: _Slide) -> Page:
    """Project a :class:`_Slide` into a frozen :class:`Page`."""
    return Page(
        page_number=slide.index + 1,
        text=_slide_to_markdown(slide),
        has_images=slide.has_images,
    )


def _slide_to_markdown(slide: _Slide) -> str:
    header_title = slide.title if slide.title else "(untitled)"
    parts = [f"## Slide {slide.index + 1}: {header_title}"]
    if slide.body:
        parts.append(slide.body)
    if slide.notes:
        parts.append(f"> **Speaker notes**: {slide.notes}")
    return "\n\n".join(parts)


def _slides_to_markdown(slides: list[_Slide]) -> str:
    """Combine every slide's markdown rendering into one document."""
    return "\n\n".join(_slide_to_markdown(s) for s in slides)


def _metadata_from(prs: _PptxPresentation) -> DocMetadata:
    """Lift Title / Author / Created from ``prs.core_properties``."""
    core = getattr(prs, "core_properties", None)
    title = _coerce_optional_str(getattr(core, "title", None)) if core is not None else None
    author = _coerce_optional_str(getattr(core, "author", None)) if core is not None else None
    created = _coerce_optional_iso(getattr(core, "created", None)) if core is not None else None
    slides_attr = getattr(prs, "slides", None)
    page_count: int | None
    try:
        page_count = len(slides_attr) if slides_attr is not None else None
    except TypeError:
        page_count = None
    return DocMetadata(
        title=title,
        author=author,
        created_date=created,
        language=None,
        page_count=page_count,
    )


def _coerce_optional_str(value: Any) -> str | None:
    """Return ``value`` as a non-empty stripped string, or ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _coerce_optional_iso(value: Any) -> str | None:
    """Return ``value.isoformat()`` when ``value`` is a datetime-like."""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            iso = isoformat()
        except (TypeError, ValueError):
            return None
        return iso if isinstance(iso, str) and iso.strip() else None
    return None


def _confidence_heuristic(markdown: str, raw: bytes) -> float:
    """Cheap "did pptx actually recover content" signal.

    Returns the byte-recovery ratio (chars-out / bytes-in), capped at
    1.0. A scanned slide deck (image-only, no text shapes, no notes)
    returns ~0; a normal text deck returns a small positive number
    because the ZIP-wrapped XML is many times bigger than the lifted
    text content.
    """
    if not raw:
        return 0.0
    ratio = len(markdown) / len(raw)
    return min(ratio, 1.0)
