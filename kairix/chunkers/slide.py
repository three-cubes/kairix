"""SlideChunker — one slide = one chunk (ADR-028 §"PPTX — `SlideChunker`").

The canonical PPTX chunking strategy: every slide becomes exactly one
:class:`~kairix.core.protocols.Chunk`. The slide boundary IS the unit
— no overlap, no sub-splitting, no merging across slides. Consensus
across Unstructured's ``by_page``, LlamaIndex's PPTX parser, and
practitioner guides.

**Used by:** SharePoint ``.pptx`` + Google Drive ``.pptx``.

**Size / overlap:** whole slide (~50-300 tokens typical) / none -
slide boundary is the natural unit.

**Failure mode of flat splitting that this fixes:** "a slide titled
'Q3 Architecture Decision' with a diagram carries most of its meaning
in the diagram, not in the six bullets underneath" — a flat splitter
that ignores slide boundaries returns chunks with no way to recover
*which slide* or *which deck*. Visual context is unrecoverable.

**Protocol contract:** :class:`~kairix.core.protocols.Chunker` —
``chunk(text=, section_kind=, source_uri=) -> tuple[Chunk, ...]``.
Each emitted Chunk carries:
  * ``text`` — slide markdown (title + body + speaker notes).
  * ``source_uri`` — propagated from input.
  * ``chunker_version=self.version`` — F55.
  * ``metadata["slide_number"]`` — 1-indexed slide ordinal.
  * ``metadata["slide_title"]`` — slide title (empty for untitled).

Input contract: ``text`` is the already-rendered slide markdown coming
out of :class:`~kairix.extractors.pptx.PptxExtractor` (one slide per
``Page``). The chunker accepts the multi-slide markdown that
:meth:`PptxExtractor.extract` produces (``## Slide N: <title>`` headers
between slides) and splits on those slide headers; for single-page
calls (Silver's per-page driver) it produces exactly one chunk.

See ``tests/bdd/features/chunker_slide.feature`` for the behaviour spec.
"""

from __future__ import annotations

import hashlib
import re

from kairix.core.protocols import Chunk

#: F55-mandated module-level version. Bump when the per-slide
#: rendering rules change in a way that affects downstream embeddings.
version: str = "0.1.0"

#: Canonical plugin name surfaced to the chunker registry.
PLUGIN_NAME = "slide"

#: Regex matching the slide-header line PptxExtractor emits per slide:
#: ``## Slide <number>: <title>``. Capturing groups: (number, title).
#: Only same-line spaces are allowed between the tokens, so an untitled slide
#: (``## Slide 3:``) gets an empty title instead of borrowing its first body
#: line (or the next slide's header). Linear-time: no overlapping quantifiers.
_SLIDE_HEADER_RE = re.compile(r"^##[^\S\n]+Slide[^\S\n]+(\d+):[^\S\n]*((?:\S.*)?)$", re.MULTILINE)


class SlideChunker:
    """One-slide-per-chunk :class:`~kairix.core.protocols.Chunker` for PPTX.

    Declares ``version: str = "0.1.0"`` (F55). Every emitted
    :class:`Chunk` carries ``chunker_version=self.version`` (also F55).

    No constructor args today — the slide-as-chunk rule is the entire
    behaviour. Sub-classes can override ``version`` per the F55 module
    discipline.
    """

    name: str = PLUGIN_NAME
    version: str

    def __init__(self) -> None:
        self.version = version

    def chunk(self, *, text: str, section_kind: str, source_uri: str) -> tuple[Chunk, ...]:
        """Split ``text`` on slide-header boundaries; one Chunk per slide.

        ``section_kind`` is accepted for Protocol compliance and read
        once for F19; the slide-as-chunk rule is uniform across section
        kinds (a deck is a deck).

        Single-slide input (no ``## Slide`` headers, e.g. when Silver
        drives one Page at a time) collapses to exactly one Chunk
        carrying the full input text.
        """
        if not section_kind:
            section_kind = "text"  # defensive default for F19
        del section_kind
        stripped = text.strip()
        if not stripped:
            return ()
        slides = _split_on_slide_headers(stripped)
        return tuple(
            _build_slide_chunk(
                slide_text=slide_text,
                slide_number=slide_number,
                slide_title=slide_title,
                source_uri=source_uri,
                chunker_version=self.version,
            )
            for slide_text, slide_number, slide_title in slides
        )


def _split_on_slide_headers(text: str) -> list[tuple[str, int, str]]:
    """Return ``[(slide_text, slide_number, slide_title), ...]``.

    Splits ``text`` on the ``## Slide N: <title>`` header lines emitted
    by :class:`PptxExtractor`. The header IS part of the emitted chunk
    text — retrieval citations need the slide number visible in the
    chunk body. When ``text`` does not contain any slide header, the
    whole input collapses to one entry with ``slide_number=1`` and an
    empty title (Silver's per-page driver).
    """
    matches = list(_SLIDE_HEADER_RE.finditer(text))
    if not matches:
        return [(text.strip(), 1, "")]
    out: list[tuple[str, int, str]] = []
    for i, match in enumerate(matches):
        slide_number = int(match.group(1))
        slide_title = match.group(2).strip()
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        slide_text = text[start:end].strip()
        out.append((slide_text, slide_number, slide_title))
    return out


def _build_slide_chunk(
    *,
    slide_text: str,
    slide_number: int,
    slide_title: str,
    source_uri: str,
    chunker_version: str,
) -> Chunk:
    """Construct one F39 + F55 clean :class:`Chunk` for a slide.

    ``source_name``, ``source_modified_at``, ``sensitivity`` carry safe
    defaults at the Chunker.chunk() boundary because the Protocol only
    surfaces ``text + section_kind + source_uri``; Silver's composition
    site wraps each chunk with the full per-document context before
    persistence.
    """
    return Chunk(
        text=slide_text,
        content_hash=hashlib.sha256(slide_text.encode("utf-8")).hexdigest(),
        source_name="",
        source_uri=source_uri,
        source_modified_at="",
        source_page=slide_number,
        sensitivity="internal",
        chunker_version=chunker_version,
        metadata={
            "slide_number": str(slide_number),
            "slide_title": slide_title,
        },
    )


def make_chunker() -> SlideChunker:
    """Construct the SlideChunker for entry-point discovery."""
    return SlideChunker()


__all__ = [
    "PLUGIN_NAME",
    "SlideChunker",
    "make_chunker",
    "version",
]
