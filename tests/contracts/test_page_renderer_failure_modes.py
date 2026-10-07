"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`PageRenderer`.

``PageRenderer.render(raw, mime)`` turns the source bytes into one image
per page before OCR. Two failure shapes matter to the connector pipeline
calling ``OcrExtractor.extract``:

* undecodable bytes — the decoder's typed error must propagate out of
  ``extract`` (the orchestrator dead-letters on it) instead of producing
  an empty document that looks like a blank scan;
* a document with no pages — ``extract`` returns an empty document with
  no pages that fails ``quality_ok``.

One body per method, two implementations (F43 limb 2), both driven
through the real :class:`OcrExtractor`:

* the real default renderer (Pillow for images, pdfplumber for PDFs) fed
  undecodable image bytes / a structurally valid zero-page PDF;
* the canonical :class:`tests.fakes.FakePageRenderer`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from PIL import UnidentifiedImageError

from kairix.extractors.ocr import OcrExtractor
from kairix.extractors.ocr import version as ocr_version
from tests.fakes import FakeOcrRunner, FakePageRenderer

pytestmark = pytest.mark.contract


def _zero_page_pdf() -> bytes:
    """A structurally valid PDF whose page tree has no pages."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [] /Count 0 >>"]
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref_at)
    return out


def _extractor(renderer_factory: Callable[[], Any] | None) -> OcrExtractor:
    if renderer_factory is None:
        # Production default renderer — no page_renderer override.
        return OcrExtractor(version=ocr_version, ocr_runner=FakeOcrRunner, preprocessor=lambda img: img)
    return OcrExtractor(
        version=ocr_version,
        page_renderer=renderer_factory,
        ocr_runner=FakeOcrRunner,
        preprocessor=lambda img: img,
    )


_UNDECODABLE: list[Callable[[], OcrExtractor]] = [
    lambda: _extractor(None),
    lambda: _extractor(lambda: FakePageRenderer(raises=UnidentifiedImageError("cannot identify image file"))),
]
_NO_PAGES: list[Callable[[], OcrExtractor]] = [
    lambda: _extractor(None),
    lambda: _extractor(lambda: FakePageRenderer(pages=())),
]
_IDS = ["real-default-renderer", "fake"]


@pytest.mark.parametrize("factory", _UNDECODABLE, ids=_IDS)
def test_render_raises_on_undecodable_image_propagates_out_of_extract(factory: Callable[[], OcrExtractor]) -> None:
    """Undecodable image bytes raise Pillow's ``UnidentifiedImageError`` out
    of ``extract``.

    Sabotage proof: in ``kairix/extractors/ocr/extractor.py::_decode_image_bytes``
    wrap the ``Image.open`` block in ``try`` / ``except OSError: return
    np.zeros((1, 1), dtype=np.uint8)``. Re-run: the real case fails because
    ``extract`` returns a document instead of raising. Restored.
    """
    extractor = factory()

    with pytest.raises(UnidentifiedImageError, match="cannot identify image file"):
        extractor.extract(b"definitely-not-a-png", "image/png")


@pytest.mark.parametrize("factory", _NO_PAGES, ids=_IDS)
def test_render_returns_empty_for_pageless_pdf_extract_fails_quality_gate(
    factory: Callable[[], OcrExtractor],
) -> None:
    """A page-less PDF renders to no images: ``extract`` returns an empty,
    page-less, zero-confidence document that ``quality_ok`` escalates.

    Sabotage proof: in ``_aggregate_confidence`` change ``if not pages:
    return 0.0`` to ``return 1.0``. Re-run: both cases fail on the
    confidence assertion. Restored.
    """
    extractor = factory()

    doc = extractor.extract(_zero_page_pdf(), "application/pdf")

    assert (doc.markdown, doc.pages, doc.confidence) == ("", (), 0.0)
    assert doc.metadata.page_count is None
    assert extractor.quality_ok(doc) is False
