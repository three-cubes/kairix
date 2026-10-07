"""Contract test for the ``docx`` extractor plugin (F43).

Imports the canonical fake AND the real implementation, then runs the
same :class:`Extractor` Protocol assertions against both. The fake
proves the test seam is real; the real impl proves the production
class satisfies the same shape.

The real :class:`DocxExtractor` is constructed with a scripted
``document_opener`` so the upstream library is not driven against
disk during the contract test — the scripted opener yields an in-
memory fake document that satisfies the wire-shape Protocol
:class:`kairix.extractors.docx.extractor._DocxDocument`. The
library-level import is exercised by the unit tests under
``tests/extractors/test_docx.py`` when the optional ``docx`` extra
is installed.

Sabotage-proofs:

  * Deleting ``version`` from :mod:`kairix.extractors.docx` breaks
    ``test_extractor_declares_version``.
  * Flipping ``can_extract`` to ``return True`` for ``text/plain``
    on the real impl breaks ``test_real_rejects_plain_text[real]``.
  * The ``make_extractor`` concrete-type check lives in
    ``tests/extractors/test_docx.py::test_factory_returns_docx_instance``.
  * Flipping the quality gate to always pass (char threshold ``0`` AND
    the heading check to ``True``) breaks
    ``test_quality_ok_false_on_short_output[real]`` (executed).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.extractors import ExtractedDocument, Extractor
from kairix.extractors.docx import (
    DocxExtractor,
)
from kairix.extractors.docx import (
    version as docx_version,
)
from tests.fakes import FakeDocxExtractor

pytestmark = pytest.mark.contract

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass
class _StubStyle:
    name: str = "Normal"


@dataclass
class _StubParagraph:
    """Stub of the python-docx :class:`Paragraph` shape."""

    text: str
    style: _StubStyle = field(default_factory=_StubStyle)
    # python-docx exposes ``_p`` as the underlying lxml element; the
    # extractor's track-changes walker only dereferences it when
    # ``_has_tracked_changes`` reports True, so a ``None`` here is
    # safe for the no-track-changes scenarios this contract test
    # exercises.
    _p: Any = None


@dataclass
class _StubCell:
    text: str


@dataclass
class _StubRow:
    cells: list[_StubCell]


@dataclass
class _StubTable:
    rows: list[_StubRow]


@dataclass
class _StubBody:
    xml: str = ""


@dataclass
class _StubElement:
    body: _StubBody = field(default_factory=_StubBody)


@dataclass
class _StubCoreProperties:
    title: str | None = "Stub Title"
    author: str | None = "agent-alpha"
    created: Any = None


@dataclass
class _StubDocument:
    """Stub of :class:`docx.document.Document` — enough surface for the
    extractor's render walk."""

    paragraphs: list[_StubParagraph]
    tables: list[_StubTable] = field(default_factory=list)
    element: _StubElement = field(default_factory=_StubElement)
    core_properties: _StubCoreProperties = field(default_factory=_StubCoreProperties)


def _default_stub_document() -> _StubDocument:
    """Construct an in-memory docx-shaped document with a heading + body."""
    paragraphs = [
        _StubParagraph(text="Section One", style=_StubStyle(name="Heading 1")),
        _StubParagraph(text="Body paragraph one with plenty of content to clear the quality gate floor."),
        _StubParagraph(text="Subsection", style=_StubStyle(name="Heading 2")),
        _StubParagraph(text="Body paragraph two with additional content to keep the markdown blob substantial."),
    ]
    return _StubDocument(paragraphs=paragraphs)


def _make_real_with_stub(*, doc_factory: Callable[[], _StubDocument] | None = None) -> Extractor:
    """Construct the real :class:`DocxExtractor` with a scripted document opener."""
    factory = doc_factory or _default_stub_document

    def _opener_factory() -> Callable[[str], _StubDocument]:
        def _open(_path: str) -> _StubDocument:
            return factory()

        return _open

    return DocxExtractor(version=docx_version, document_opener=_opener_factory)


_Factory = Callable[[], Extractor]


@pytest.fixture(
    params=[
        pytest.param(lambda: FakeDocxExtractor(), id="fake"),
        pytest.param(_make_real_with_stub, id="real"),
    ]
)
def _extractor(request: pytest.FixtureRequest) -> Extractor:
    factory: _Factory = request.param
    return factory()


@pytest.mark.contract
def test_docx_extractor_satisfies_protocol(_extractor: Extractor) -> None:
    """Both fake and real are runtime ``Extractor`` instances named ``docx``."""
    assert isinstance(_extractor, Extractor)
    assert getattr(_extractor, "name", None) == "docx"


@pytest.mark.contract
def test_extractor_declares_version(_extractor: Extractor) -> None:
    """F40 requirement — the extractor carries a non-empty ``version``
    (the real one threads the module-level declaration through)."""
    ver = getattr(_extractor, "version", None)
    assert isinstance(ver, str)
    assert ver.strip() != ""


@pytest.mark.contract
def test_can_extract_claims_docx_mime(_extractor: Extractor) -> None:
    """Both fake and real claim the docx Office Open XML mime."""
    assert _extractor.can_extract(_DOCX_MIME, b"PK\x03\x04") is True


@pytest.mark.contract
def test_can_extract_rejects_octet_stream_without_document_hint(_extractor: Extractor) -> None:
    """ZIP magic alone (no docx-shaped mime hint) is not claimed."""
    assert _extractor.can_extract("application/octet-stream", b"PK\x03\x04") is False


@pytest.mark.contract
def test_real_rejects_plain_text(_extractor: Extractor) -> None:
    """Both impls refuse ``text/plain`` — that's passthrough's job."""
    assert _extractor.can_extract("text/plain", b"hello") is False


@pytest.mark.contract
def test_extract_returns_document_with_non_empty_markdown(_extractor: Extractor) -> None:
    """``extract`` produces an :class:`ExtractedDocument` with markdown text."""
    doc = _extractor.extract(b"PK\x03\x04" + b"x" * 256, _DOCX_MIME)
    assert isinstance(doc, ExtractedDocument)
    assert doc.markdown.strip() != ""


@pytest.mark.contract
def test_quality_ok_true_on_substantive_output(_extractor: Extractor) -> None:
    """Quality gate passes when markdown carries headings + body."""
    doc = _extractor.extract(b"PK\x03\x04" + b"x" * 256, _DOCX_MIME)
    assert _extractor.quality_ok(doc) is True


def _short_doc() -> _StubDocument:
    return _StubDocument(paragraphs=[_StubParagraph(text="x")])


_SHORT_OUTPUT_IMPLEMENTATIONS: list[tuple[str, _Factory]] = [
    ("real", lambda: _make_real_with_stub(doc_factory=_short_doc)),
    ("fake", lambda: FakeDocxExtractor(scripted_markdown="x")),
]


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _SHORT_OUTPUT_IMPLEMENTATIONS)
def test_quality_ok_false_on_short_output(name: str, factory: _Factory) -> None:
    """Quality gate fails when the document is essentially empty."""
    extractor = factory()
    doc = extractor.extract(b"PK\x03\x04" + b"y" * 4096, _DOCX_MIME)
    assert extractor.quality_ok(doc) is False, name
