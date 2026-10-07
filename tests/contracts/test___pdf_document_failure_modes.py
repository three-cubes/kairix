"""F68 (ADR-024 Bundle A) — failure-mode contract for ``_PdfDocument``.

``_PdfDocument`` is the context-managed ``pdfplumber.PDF`` wire shape the
``pdf_fallback`` extractor reads (``pages`` / ``metadata``). The failure
shapes that matter to the connector pipeline:

* a structurally valid PDF whose page tree is empty — the extractor must
  return an empty document recording ``page_count == 0`` and fail
  ``quality_ok`` rather than raise or invent a page;
* a PDF with no Info dictionary — metadata degrades to ``None`` fields
  (never empty strings that would be indexed as a blank title / author).

One body per method, two document implementations (F43 limb 2), both
driven through the real :class:`PdfFallbackExtractor`:

* real ``pdfplumber`` documents (a generated zero-page PDF and the
  recorded ``sample.pdf`` fixture, which carries no Info dict) opened
  through the extractor's default opener;
* :class:`tests.fakes.FakePdfDocument` injected through ``pdf_opener``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kairix.extractors.pdf_fallback import PdfFallbackExtractor
from kairix.extractors.pdf_fallback import version as pdf_fallback_version
from tests.fakes import FakePdfDocument, FakePdfPage

pytestmark = pytest.mark.contract

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "extractors"

_Case = tuple[PdfFallbackExtractor, bytes]


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


def _real(raw: bytes) -> _Case:
    return PdfFallbackExtractor(version=pdf_fallback_version), raw


def _fake(document: FakePdfDocument) -> _Case:
    extractor = PdfFallbackExtractor(version=pdf_fallback_version, pdf_opener=lambda: lambda _path: document)
    return extractor, b"%PDF-1.4 fake"


_PAGELESS: list[Callable[[], _Case]] = [
    lambda: _real(_zero_page_pdf()),
    lambda: _fake(FakePdfDocument(pages=[])),
]
_NO_INFO: list[Callable[[], _Case]] = [
    lambda: _real((_FIXTURES / "sample.pdf").read_bytes()),
    lambda: _fake(FakePdfDocument(pages=[FakePdfPage(text="Body text.")], metadata={})),
]
_IDS = ["real-pdfplumber", "fake"]


@pytest.mark.parametrize("factory", _PAGELESS, ids=_IDS)
def test_pages_returns_empty_document_records_zero_pages_and_escalates(factory: Callable[[], _Case]) -> None:
    """No pages: empty markdown, no ``Page`` objects, ``page_count == 0``,
    zero confidence, and ``quality_ok`` False.

    Sabotage proof: in ``_pdf_metadata_to_doc_metadata`` change
    ``page_count=page_count`` to ``page_count=page_count or None``. Re-run:
    both cases fail because the page count of an empty PDF is lost.
    Restored.
    """
    extractor, raw = factory()

    doc = extractor.extract(raw, "application/pdf")

    assert (doc.markdown, doc.pages, doc.confidence) == ("", (), 0.0)
    assert doc.metadata.page_count == 0
    assert extractor.quality_ok(doc) is False


@pytest.mark.parametrize("factory", _NO_INFO, ids=_IDS)
def test_metadata_returns_empty_info_dict_degrades_to_none_fields(factory: Callable[[], _Case]) -> None:
    """No Info dict: title / author / created are ``None`` (not ``""``) and
    the page count still reflects the parsed pages.

    Sabotage proof: in ``_pdf_metadata_to_doc_metadata`` change
    ``title=_clean_string(metadata.get("Title"))`` to
    ``title=metadata.get("Title", "")``. Re-run: both cases fail because
    the title comes back as ``""``. Restored.
    """
    extractor, raw = factory()

    meta = extractor.extract(raw, "application/pdf").metadata

    assert (meta.title, meta.author, meta.created_date) == (None, None, None)
    assert meta.page_count == 1
