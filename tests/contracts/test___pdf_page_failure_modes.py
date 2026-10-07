"""F68 (ADR-024 Bundle A) — failure-mode contract for ``_PdfPage``.

``_PdfPage`` is the ``pdfplumber.Page`` wire shape the ``pdf_fallback``
extractor reads (``extract_text`` / ``extract_tables``). The failure
shapes that matter to the connector pipeline:

* an image-only (scanned) page whose text layer is empty — the extractor
  must still emit the page (with empty text) and fail ``quality_ok`` so
  the escalation chain hands the document to OCR;
* a page with no tables — the markdown carries the page text only, never
  an empty ``| --- |`` table skeleton.

One body per method, two page implementations (F43 limb 2), both driven
through the real :class:`PdfFallbackExtractor`:

* real ``pdfplumber`` pages from the recorded fixtures (opened through the
  extractor's default opener);
* :class:`tests.fakes.FakePdfPage` injected through the ``pdf_opener`` seam.
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
_SAMPLE_TEXT = (
    "Hello PDF text extraction.\n"
    "This is the second line of the fixture document.\n"
    "We want enough characters to pass the quality gate.\n"
    "Markitdown should recover all of these lines as markdown."
)

_Case = tuple[PdfFallbackExtractor, bytes]


def _real(fixture: str) -> _Case:
    return PdfFallbackExtractor(version=pdf_fallback_version), (_FIXTURES / fixture).read_bytes()


def _fake(page: FakePdfPage) -> _Case:
    document = FakePdfDocument(pages=[page])
    extractor = PdfFallbackExtractor(version=pdf_fallback_version, pdf_opener=lambda: lambda _path: document)
    return extractor, b"%PDF-1.4 fake"


_SCANNED: list[Callable[[], _Case]] = [
    lambda: _real("scanned_sample.pdf"),
    lambda: _fake(FakePdfPage(text=None)),
]
_NO_TABLES: list[Callable[[], _Case]] = [
    lambda: _real("sample.pdf"),
    lambda: _fake(FakePdfPage(text=_SAMPLE_TEXT, tables=[])),
]
_IDS = ["real-pdfplumber", "fake"]


@pytest.mark.parametrize("factory", _SCANNED, ids=_IDS)
def test_extract_text_returns_empty_for_scanned_page_document_escalates(factory: Callable[[], _Case]) -> None:
    """An empty text layer yields one page with ``text == ""``, empty
    markdown, and ``quality_ok`` False (escalate to OCR).

    Sabotage proof: in ``kairix/extractors/pdf_fallback/extractor.py::_build_page``
    change ``raw_text = page.extract_text() or ""`` to
    ``raw_text = page.extract_text() or "[image-only page]"``. Re-run: both
    cases fail on the page-text / markdown assertions. Restored.
    """
    extractor, raw = factory()

    doc = extractor.extract(raw, "application/pdf")

    assert [page.text for page in doc.pages] == [""]
    assert doc.markdown == ""
    assert extractor.quality_ok(doc) is False


@pytest.mark.parametrize("factory", _NO_TABLES, ids=_IDS)
def test_extract_tables_returns_empty_markdown_carries_text_only(factory: Callable[[], _Case]) -> None:
    """No tables: the markdown is exactly the page text — no empty table
    skeleton — and the text-bearing document passes the quality gate.

    Sabotage proof: in ``_render_page_markdown`` append
    ``chunks.append(_table_to_markdown([[None]]))`` after the table loop.
    Re-run: both cases fail because a ``|  |`` skeleton is appended.
    Restored.
    """
    extractor, raw = factory()

    doc = extractor.extract(raw, "application/pdf")

    assert doc.markdown == _SAMPLE_TEXT
    assert "|" not in doc.markdown
    assert extractor.quality_ok(doc) is True
