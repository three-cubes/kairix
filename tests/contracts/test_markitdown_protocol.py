"""Contract test for the ``markitdown`` extractor plugin (F43).

Imports the canonical fake AND the real implementation, then runs the
same :class:`Extractor` Protocol assertions against both. The fake
proves the test seam is real; the real impl proves the production
class satisfies the same shape — without requiring the upstream
``markitdown`` library to be present in the contract-test environment.

The real :class:`MarkitdownExtractor` is constructed with a scripted
``converter_factory`` so the upstream library is not imported during
the contract test. The library-level import is exercised by the unit
tests under ``tests/extractors/`` when the optional ``markitdown``
extra is installed.

Sabotage-proofs:

  * Deleting ``version`` from :mod:`kairix.extractors.markitdown`
    breaks ``test_extractor_declares_version``.
  * Flipping ``can_extract`` to ``return True`` for ``text/plain``
    on the real impl breaks ``test_real_rejects_plain_text``.
  * Flipping the quality gate's char threshold to ``0`` breaks
    ``test_quality_ok_false_on_short_output``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from kairix.extractors import ExtractedDocument, Extractor
from kairix.extractors.markitdown import (
    MarkitdownExtractor,
)
from kairix.extractors.markitdown import (
    make_extractor as make_real_extractor,
)
from kairix.extractors.markitdown import (
    version as markitdown_version,
)
from tests.fakes import FakeMarkitdownExtractor

pytestmark = pytest.mark.contract


@dataclass
class _StubResult:
    """Stub of the upstream ``DocumentConverterResult`` shape."""

    markdown: str

    @property
    def text_content(self) -> str:
        return self.markdown


class _StubConverter:
    """Stub of the upstream :class:`MarkItDown` shape — returns a fixed result."""

    def __init__(self, markdown: str) -> None:
        self.markdown = markdown

    def convert(self, source: Any, **kwargs: Any) -> _StubResult:
        return _StubResult(markdown=self.markdown)


def _make_real_with_stub(*, markdown: str = "# Recovered\n" + ("body line\n" * 12)) -> Extractor:
    """Construct the real :class:`MarkitdownExtractor` with a stub converter."""
    return MarkitdownExtractor(
        version=markitdown_version,
        converter_factory=lambda: _StubConverter(markdown=markdown),
    )


_Factory = Callable[[], Extractor]


@pytest.fixture(
    params=[
        pytest.param(lambda: FakeMarkitdownExtractor(), id="fake"),
        pytest.param(_make_real_with_stub, id="real"),
    ]
)
def _extractor(request: pytest.FixtureRequest) -> Extractor:
    factory: _Factory = request.param
    return factory()


@pytest.mark.contract
def test_markitdown_extractor_satisfies_protocol(_extractor: Extractor) -> None:
    """Both fake and real are runtime ``Extractor`` instances named ``markitdown``.

    Sabotage proof: renamed ``MarkitdownExtractor.quality_ok`` to
    ``_quality_ok`` → the real leg's isinstance(Extractor) failed. Restored.
    """
    assert isinstance(_extractor, Extractor)
    assert _extractor.name == "markitdown"


@pytest.mark.contract
def test_extractor_declares_version(_extractor: Extractor) -> None:
    """F40 requirement — module-level ``version`` is non-empty and both
    impls carry it (the fake pins the same lockfile version).

    Sabotage proof: in ``MarkitdownExtractor.__init__`` changed
    ``self.version = version`` to ``self.version = version + "-x"`` → the
    real leg failed the version-equality assertion. Restored.
    """
    assert isinstance(markitdown_version, str)
    assert markitdown_version.strip() != ""
    assert _extractor.version == markitdown_version


@pytest.mark.contract
@pytest.mark.parametrize(
    ("factory", "expected_cls"),
    [
        (make_real_extractor, MarkitdownExtractor),
        (FakeMarkitdownExtractor, FakeMarkitdownExtractor),
    ],
    ids=["real", "fake"],
)
def test_real_factory_returns_markitdown_instance(factory: _Factory, expected_cls: type) -> None:
    """The factory returns its concrete markitdown extractor.

    Sabotage proof: changed ``MarkitdownExtractor.__init__`` to set
    ``self.name = "markitdown-x"`` → the real leg's name assertion failed.
    Restored.
    """
    extractor = factory()
    assert isinstance(extractor, expected_cls)
    assert isinstance(extractor, Extractor)
    assert extractor.name == "markitdown"


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
    """Both impls refuse ``text/plain`` — that's passthrough's job.

    Sabotage proof: added ``"text/plain"`` to ``_MARKITDOWN_MIMES`` → the
    real leg's ``is False`` assertion failed. Restored.
    """
    assert _extractor.can_extract("text/plain", b"hello") is False


@pytest.mark.contract
def test_extract_returns_document_with_non_empty_markdown(_extractor: Extractor) -> None:
    """``extract`` produces an :class:`ExtractedDocument` with markdown text."""
    doc = _extractor.extract(b"%PDF-1.4\n" + b"x" * 64, "application/pdf")
    assert isinstance(doc, ExtractedDocument)
    assert doc.markdown.strip() != ""


@pytest.mark.contract
def test_quality_ok_true_on_substantive_output(_extractor: Extractor) -> None:
    """Quality gate passes when markdown has enough content."""
    doc = _extractor.extract(b"%PDF-1.4\n" + b"x" * 32, "application/pdf")
    assert _extractor.quality_ok(doc) is True


@pytest.mark.contract
@pytest.mark.parametrize(
    "factory",
    [
        lambda: _make_real_with_stub(markdown="x"),
        lambda: FakeMarkitdownExtractor(scripted_markdown="x"),
    ],
    ids=["real", "fake"],
)
def test_quality_ok_false_on_short_output(factory: _Factory) -> None:
    """Quality gate fails when markitdown returns near-empty markdown.

    Sabotage proof: set ``_QUALITY_MIN_CHARS`` to ``0`` → the real leg's
    ``is False`` assertion failed. Restored.
    """
    extractor = factory()
    doc = extractor.extract(b"%PDF-1.4\n" + b"y" * 4096, "application/pdf")
    assert extractor.quality_ok(doc) is False
