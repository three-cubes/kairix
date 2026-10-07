"""Contract test for the ``pdf_fallback`` extractor plugin (F43).

Imports the canonical fake AND the real implementation, then runs the
same :class:`Extractor` Protocol assertions against both. The fake
proves the test seam is real; the real impl proves the production
class satisfies the same shape — without requiring the upstream
``pdfplumber`` library to be present in the contract-test environment.

The real :class:`PdfFallbackExtractor` is constructed with a scripted
``pdf_opener`` so the upstream library is not imported during the
contract test. The library-level import is exercised by the unit
tests under ``tests/extractors/test_pdf_fallback.py`` against the
recorded ``tests/fixtures/extractors/sample.pdf`` fixture.

Sabotage-proofs:

  * Deleting ``version`` from :mod:`kairix.extractors.pdf_fallback`
    breaks ``test_extractor_declares_version``.
  * Flipping ``can_extract`` to ``return True`` for ``text/plain``
    on the real impl breaks ``test_real_rejects_plain_text``.
  * Flipping the quality gate's char threshold to ``0`` breaks
    ``test_quality_ok_false_on_image_only_output``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.extractors import ExtractedDocument, Extractor
from kairix.extractors.pdf_fallback import (
    PdfFallbackExtractor,
)
from kairix.extractors.pdf_fallback import (
    make_extractor as make_real_extractor,
)
from kairix.extractors.pdf_fallback import (
    version as pdf_fallback_version,
)
from tests.fakes import FakePdfFallbackExtractor

pytestmark = pytest.mark.contract


@dataclass
class _StubPage:
    """Stub of the upstream ``pdfplumber.Page`` shape."""

    text: str = ""
    tables: list[list[list[str | None]]] = field(default_factory=list)

    def extract_text(self) -> str | None:
        return self.text or None

    def extract_tables(self) -> list[list[list[str | None]]]:
        return list(self.tables)


@dataclass
class _StubPdf:
    """Stub of the upstream ``pdfplumber.PDF`` shape — context-managed."""

    pages: list[_StubPage] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __enter__(self) -> _StubPdf:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _make_real_with_stub(*, page_text: str | None = None) -> Extractor:
    """Construct the real :class:`PdfFallbackExtractor` with a stub opener."""
    body = page_text if page_text is not None else ("Recovered body text from PDF page.\n" * 6)
    pdf = _StubPdf(pages=[_StubPage(text=body)], metadata={"Title": "stub"})

    def _opener(_path: str) -> _StubPdf:
        return pdf

    return PdfFallbackExtractor(
        version=pdf_fallback_version,
        pdf_opener=lambda: _opener,
    )


_Factory = Callable[[], Extractor]


@pytest.fixture(
    params=[
        pytest.param(lambda: FakePdfFallbackExtractor(), id="fake"),
        pytest.param(_make_real_with_stub, id="real"),
    ]
)
def _extractor(request: pytest.FixtureRequest) -> Extractor:
    factory: _Factory = request.param
    return factory()


@pytest.fixture(
    params=[
        pytest.param(lambda: FakePdfFallbackExtractor(scripted_markdown="", scripted_page_text=""), id="fake"),
        pytest.param(lambda: _make_real_with_stub(page_text=""), id="real"),
    ]
)
def _image_only_extractor(request: pytest.FixtureRequest) -> Extractor:
    """Extractor facing a scanned (image-only) PDF — every page's text layer is empty."""
    factory: _Factory = request.param
    return factory()


@pytest.mark.contract
def test_pdf_fallback_extractor_satisfies_protocol(_extractor: Extractor) -> None:
    """Fake and real (stubbed) instances are runtime ``Extractor``s.

    Sabotage proof: rename ``PdfFallbackExtractor.quality_ok`` in
    kairix/extractors/pdf_fallback/extractor.py — the real leg's runtime
    probe fails.
    """
    assert isinstance(_extractor, Extractor)


@pytest.mark.contract
@pytest.mark.parametrize(
    "factory",
    [pytest.param(make_real_extractor, id="real"), pytest.param(FakePdfFallbackExtractor, id="fake")],
)
def test_extractor_declares_version(factory: _Factory) -> None:
    """F40 requirement — module-level ``version`` is non-empty and every
    impl carries it (the fake mirrors the plugin's declared version).

    Sabotage proof: make ``make_extractor`` in
    kairix/extractors/pdf_fallback/__init__.py pass ``version="0.0.0"`` —
    the real leg's equality fails.
    """
    assert isinstance(pdf_fallback_version, str)
    assert pdf_fallback_version.strip() != ""
    assert factory().version == pdf_fallback_version


@pytest.mark.contract
@pytest.mark.parametrize(
    "factory,expected_cls",
    [
        pytest.param(make_real_extractor, PdfFallbackExtractor, id="real"),
        pytest.param(FakePdfFallbackExtractor, FakePdfFallbackExtractor, id="fake"),
    ],
)
def test_real_factory_returns_pdf_fallback_instance(factory: _Factory, expected_cls: type) -> None:
    """``make_extractor`` returns a real :class:`PdfFallbackExtractor`; both
    impls are runtime ``Extractor``s under the ``pdf_fallback`` plugin name.

    Sabotage proof: change ``PLUGIN_NAME`` in
    kairix/extractors/pdf_fallback/extractor.py to ``"pdf"`` — the real
    leg's name assertion fails.
    """
    impl = factory()
    assert isinstance(impl, expected_cls)
    assert isinstance(impl, Extractor)
    assert impl.name == "pdf_fallback"


@pytest.mark.contract
def test_can_extract_claims_pdf(_extractor: Extractor) -> None:
    """Both fake and real claim ``application/pdf``."""
    assert _extractor.can_extract("application/pdf", b"%PDF-1.4") is True


@pytest.mark.contract
def test_can_extract_claims_pdf_by_magic_bytes(_extractor: Extractor) -> None:
    """Magic-byte sniff catches a PDF served as ``application/octet-stream``."""
    assert _extractor.can_extract("application/octet-stream", b"%PDF-1.7") is True


@pytest.mark.contract
def test_real_rejects_plain_text(_extractor: Extractor) -> None:
    """Fake and real refuse ``text/plain`` — that's passthrough's job.

    Sabotage proof: make ``PdfFallbackExtractor.can_extract`` return
    ``True`` for ``text/plain`` — the real leg fails.
    """
    assert _extractor.can_extract("text/plain", b"hello") is False


@pytest.mark.contract
def test_extract_returns_document_with_non_empty_markdown(_extractor: Extractor) -> None:
    """``extract`` produces an :class:`ExtractedDocument` with markdown text."""
    doc = _extractor.extract(b"%PDF-1.4\n" + b"x" * 64, "application/pdf")
    assert isinstance(doc, ExtractedDocument)
    assert doc.markdown.strip() != ""


@pytest.mark.contract
def test_extract_returns_document_with_at_least_one_page(_extractor: Extractor) -> None:
    """``extract`` populates ``pages`` so chunks can cite back per page."""
    doc = _extractor.extract(b"%PDF-1.4\n" + b"x" * 64, "application/pdf")
    assert len(doc.pages) >= 1


@pytest.mark.contract
def test_quality_ok_true_on_substantive_output(_extractor: Extractor) -> None:
    """Quality gate passes when markdown has enough content and a page carries text."""
    doc = _extractor.extract(b"%PDF-1.4\n" + b"x" * 32, "application/pdf")
    assert _extractor.quality_ok(doc) is True


@pytest.mark.contract
def test_quality_ok_false_on_image_only_output(_image_only_extractor: Extractor) -> None:
    """Quality gate fails when pdfplumber returns empty page text (scanned PDF).

    Sabotage proof: set ``_QUALITY_MIN_CHARS = 0`` and drop the per-page
    text check in ``PdfFallbackExtractor.quality_ok`` — the real leg's
    gate passes.
    """
    doc = _image_only_extractor.extract(b"%PDF-1.4\n" + b"y" * 4096, "application/pdf")
    assert _image_only_extractor.quality_ok(doc) is False
