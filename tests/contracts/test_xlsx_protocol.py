"""Contract test for the ``xlsx`` extractor plugin (F43).

Imports the canonical fake AND the real implementation, then runs the
same :class:`Extractor` Protocol assertions against both. The fake
proves the test seam is real; the real impl proves the production
class satisfies the same shape — driven against a real openpyxl-built
workbook (synthesised in-memory) without monkeypatching the upstream
library (F1-clean).

Every test runs ONE body over both impls (F43 limb 2). Workbook-shaped
tests use ``_workbook_extractor``: the real impl parses openpyxl-built
bytes, the fake models the same sheet spec via ``scripted_sheets=``.

Sabotage-proofs:

  * Deleting ``version`` from :mod:`kairix.extractors.xlsx`
    breaks ``test_extractor_declares_version``.
  * Flipping ``can_extract`` to ``return True`` for ``text/plain``
    on the real impl breaks ``test_rejects_plain_text``.
  * Flipping the quality gate's char threshold to ``0`` breaks
    ``test_quality_ok_false_on_empty_workbook``.
  * Disabling :func:`_render_workbook`'s empty-sheet skip breaks
    ``test_skips_empty_sheets``.
"""

from __future__ import annotations

import io
from collections.abc import Callable

import openpyxl
import pytest

from kairix.extractors import ExtractedDocument, Extractor
from kairix.extractors.xlsx import (
    make_extractor as make_real_extractor,
)
from kairix.extractors.xlsx import (
    version as xlsx_version,
)
from tests.fakes import FakeXlsxExtractor

pytestmark = pytest.mark.contract


# Test fixture bytes — synthesised in-memory by openpyxl from a sheet spec.
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


_Sheets = tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]

#: Mirrors the spec's "sample.xlsx" expectation (3 sheets, Data + Empty +
#: Charts; only Data + Charts survive the empty-sheet skip).
_THREE_SHEETS: _Sheets = (
    ("Data", (("product", "units"), ("widget", 10))),
    ("Empty", ()),
    ("Charts", (("region", "value"), ("north", 42))),
)
_ONE_BLANK_SHEET: _Sheets = (("Blank", ()),)


def _workbook_bytes(sheets: _Sheets) -> bytes:
    """Build a real xlsx (in-memory, openpyxl) from ``(title, rows)`` pairs."""
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets:
        sheet = workbook.create_sheet(title=title)
        for row in rows:
            sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


_THREE_SHEET_BYTES = _workbook_bytes(_THREE_SHEETS)


_Factory = Callable[[], Extractor]


@pytest.fixture(
    params=[
        pytest.param(lambda: FakeXlsxExtractor(), id="fake"),
        pytest.param(lambda: make_real_extractor(), id="real"),
    ]
)
def _extractor(request: pytest.FixtureRequest) -> Extractor:
    factory: _Factory = request.param
    return factory()


_WorkbookFactory = Callable[[_Sheets], tuple[Extractor, bytes]]


def _real_for_workbook(sheets: _Sheets) -> tuple[Extractor, bytes]:
    return make_real_extractor(), _workbook_bytes(sheets)


def _fake_for_workbook(sheets: _Sheets) -> tuple[Extractor, bytes]:
    return FakeXlsxExtractor(scripted_sheets=sheets), _workbook_bytes(sheets)


@pytest.fixture(
    params=[
        pytest.param(_fake_for_workbook, id="fake"),
        pytest.param(_real_for_workbook, id="real"),
    ]
)
def _workbook_extractor(request: pytest.FixtureRequest) -> _WorkbookFactory:
    """Per-workbook factory: (extractor, raw xlsx bytes) for a sheet spec.

    The real impl parses the openpyxl-built bytes; the fake models the
    same workbook from the spec (``scripted_sheets=``).
    """
    factory: _WorkbookFactory = request.param
    return factory


@pytest.mark.contract
def test_xlsx_extractor_satisfies_protocol(_extractor: Extractor) -> None:
    """Both impls are runtime ``Extractor`` instances registered as ``xlsx``.

    Sabotage proof: in :class:`XlsxExtractor.__init__` set
    ``self.name = "xlsx-x"``; the real leg fails. Restored.
    """
    assert isinstance(_extractor, Extractor)
    assert _extractor.name == "xlsx"


@pytest.mark.contract
def test_extractor_declares_version(_extractor: Extractor) -> None:
    """F40 requirement — module-level ``version`` is non-empty and is the
    version both impls carry (the fake pins the same openpyxl version).

    Sabotage proof: in :func:`kairix.extractors.xlsx.make_extractor` pass
    ``version="0"``; the real leg fails. Restored.
    """
    assert isinstance(xlsx_version, str)
    assert xlsx_version.strip() != ""
    assert _extractor.version == xlsx_version


@pytest.mark.contract
def test_can_extract_claims_xlsx_mime(_extractor: Extractor) -> None:
    """Both fake and real claim the spreadsheetml.sheet mime."""
    assert _extractor.can_extract(_XLSX_MIME, b"PK\x03\x04") is True


@pytest.mark.contract
def test_can_extract_claims_sheet_suffix_by_magic(_extractor: Extractor) -> None:
    """Magic-byte + mime-ending-with-'sheet' catches provider-specific suffixes."""
    assert _extractor.can_extract("application/x-vnd-acme.sheet", b"PK\x03\x04") is True


@pytest.mark.contract
def test_rejects_plain_text(_extractor: Extractor) -> None:
    """Both impls refuse ``text/plain`` — that's passthrough's job.

    Sabotage proof: in :meth:`XlsxExtractor.can_extract` return ``True``
    up front; the real leg fails. Restored.
    """
    assert _extractor.can_extract("text/plain", b"hello") is False


@pytest.mark.contract
def test_rejects_bare_zip_without_sheet_mime(_extractor: Extractor) -> None:
    """ZIP magic alone is ambiguous; both impls wait for a sheet mime.

    Sabotage proof: in :meth:`XlsxExtractor.can_extract` replace the final
    ``mime.endswith("sheet")`` check with ``True``; the real leg fails.
    Restored.
    """
    assert _extractor.can_extract("application/octet-stream", b"PK\x03\x04") is False


@pytest.mark.contract
def test_extract_returns_document_with_pages(_extractor: Extractor) -> None:
    """``extract`` produces an :class:`ExtractedDocument` with at least one Page."""
    doc = _extractor.extract(_THREE_SHEET_BYTES, _XLSX_MIME)
    assert isinstance(doc, ExtractedDocument)
    assert len(doc.pages) >= 1


@pytest.mark.contract
def test_quality_ok_true_on_substantive_workbook(_extractor: Extractor) -> None:
    """Quality gate passes when the workbook produces enough markdown."""
    doc = _extractor.extract(_THREE_SHEET_BYTES, _XLSX_MIME)
    assert _extractor.quality_ok(doc) is True


@pytest.mark.contract
def test_quality_ok_false_on_empty_workbook(_workbook_extractor: _WorkbookFactory) -> None:
    """Quality gate fails when the workbook yields no sheet content.

    Sabotage proof: in :mod:`kairix.extractors.xlsx.extractor` set
    ``_QUALITY_MIN_CHARS = 0`` and drop the ``len(doc.pages) < 1`` guard;
    the real leg fails. Restored.
    """
    extractor, raw = _workbook_extractor(_ONE_BLANK_SHEET)
    doc = extractor.extract(raw, _XLSX_MIME)
    assert extractor.quality_ok(doc) is False


@pytest.mark.contract
def test_skips_empty_sheets(_workbook_extractor: _WorkbookFactory) -> None:
    """Empty sheets contribute no Page — exactly two pages survive (Data + Charts),
    numbered by their 1-based sheet index.

    Sabotage proof: in :func:`_render_workbook` remove the
    ``if _sheet_is_skippable(sheet): continue`` skip; the real leg fails
    (three pages, ``## Sheet: Empty`` present). Restored.
    """
    extractor, raw = _workbook_extractor(_THREE_SHEETS)
    doc = extractor.extract(raw, _XLSX_MIME)
    assert len(doc.pages) == 2
    assert [page.page_number for page in doc.pages] == [1, 3]
    assert "## Sheet: Data" in doc.markdown
    assert "## Sheet: Charts" in doc.markdown
    assert "## Sheet: Empty" not in doc.markdown
    assert "| product | units |" in doc.markdown
