"""F68 (ADR-024 Bundle A) — failure-mode contract for ``_PptxPresentation``.

``_PptxPresentation`` is the ``pptx.Presentation`` wire shape the
``pptx`` extractor reads (``slides`` / ``core_properties``). The failure
shapes that matter to the connector pipeline calling
``PptxExtractor.extract``:

* a deck with no slides — an empty document recording ``page_count == 0``
  that fails ``quality_ok`` (escalate), never an invented slide;
* blank core properties — metadata degrades to ``None`` fields (never
  ``""`` indexed as a blank title / author).

One body per method, two presentation implementations (F43 limb 2), both
driven through the real :class:`PptxExtractor`:

* a real ``python-pptx`` deck built in memory, opened through the
  extractor's default loader;
* :class:`tests.fakes.FakePptxPresentation` injected through
  ``presentation_loader``.
"""

from __future__ import annotations

import io
from collections.abc import Callable

import pptx
import pytest

from kairix.extractors.pptx import PptxExtractor
from kairix.extractors.pptx import version as pptx_version
from tests.fakes import FakePptxPresentation

pytestmark = pytest.mark.contract

_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

_Case = tuple[PptxExtractor, bytes]


def _blank_deck_bytes() -> bytes:
    """A real deck with no slides and blank title / author core properties."""
    buffer = io.BytesIO()
    pptx.Presentation().save(buffer)
    return buffer.getvalue()


def _real() -> _Case:
    return PptxExtractor(version=pptx_version), _blank_deck_bytes()


def _fake() -> _Case:
    presentation = FakePptxPresentation(slides=[])
    return PptxExtractor(version=pptx_version, presentation_loader=lambda _path: presentation), b"PK\x03\x04fake"


_FACTORIES: list[Callable[[], _Case]] = [_real, _fake]
_IDS = ["real-python-pptx", "fake"]


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_slides_returns_empty_deck_records_zero_pages_and_escalates(factory: Callable[[], _Case]) -> None:
    """No slides: empty markdown, no pages, ``page_count == 0``, and
    ``quality_ok`` False.

    Sabotage proof: in ``kairix/extractors/pptx/extractor.py::_metadata_from``
    change ``page_count = len(slides_attr) if slides_attr is not None else
    None`` to ``page_count = len(slides_attr) or None``. Re-run: both cases
    fail because the zero page count is lost. Restored.
    """
    extractor, raw = factory()

    doc = extractor.extract(raw, _PPTX_MIME)

    assert (doc.markdown, doc.pages) == ("", ())
    assert doc.metadata.page_count == 0
    assert extractor.quality_ok(doc) is False


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_core_properties_returns_empty_metadata_degrades_to_none(factory: Callable[[], _Case]) -> None:
    """Blank title / author core properties become ``None``, not ``""``.

    Sabotage proof: in ``_coerce_optional_str`` change the final
    ``return None`` to ``return ""``. Re-run: both cases fail because
    title / author come back as ``""``. Restored.
    """
    extractor, raw = factory()

    meta = extractor.extract(raw, _PPTX_MIME).metadata

    assert (meta.title, meta.author) == (None, None)
