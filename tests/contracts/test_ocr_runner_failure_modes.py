"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`OcrRunner`.

``OcrRunner`` is the Tesseract seam the OCR extractor consumes
(``detect_orientation`` then ``recognise_text`` per page). Two failure
shapes matter to the connector pipeline that calls ``OcrExtractor.extract``:

* the engine crashes mid-page — the error must propagate out of
  ``extract`` (so the orchestrator dead-letters the item with the cause)
  rather than yield a silently-empty document;
* the engine recognises nothing — ``extract`` must return an empty,
  zero-confidence document that fails ``quality_ok`` so the escalation
  chain routes it onward.

One body per method, two implementations (F43 limb 2), both driven
through the real :class:`OcrExtractor`:

* the real :class:`TesseractRunner` whose ``pytesseract_module`` seam is
  a :class:`tests.fakes.FakePytesseractModule` (no Tesseract binary);
* the canonical :class:`tests.fakes.FakeOcrRunner`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.extractors.ocr import OcrExtractor
from kairix.extractors.ocr import version as ocr_version
from kairix.extractors.ocr.tesseract_runner import TesseractRunner
from tests.fakes import FakeOcrRunner, FakePageRenderer, FakePytesseractModule

pytestmark = pytest.mark.contract

_CRASH = "F68 tesseract process exited with signal 11"


def _extract_with(runner: Any) -> Any:
    extractor = OcrExtractor(
        version=ocr_version,
        page_renderer=FakePageRenderer,
        ocr_runner=lambda: runner,
        preprocessor=lambda img: img,
    )
    return extractor, extractor.extract(b"\x89PNG-scan", "image/png")


_CRASHING_FACTORIES: list[Callable[[], Any]] = [
    lambda: TesseractRunner(pytesseract_module=FakePytesseractModule(osd_raises=RuntimeError(_CRASH))),
    lambda: FakeOcrRunner(detect_raises=RuntimeError(_CRASH)),
]
_BLANK_FACTORIES: list[Callable[[], Any]] = [
    lambda: TesseractRunner(pytesseract_module=FakePytesseractModule(data={"text": ["", " "], "conf": [-1, -1]})),
    lambda: FakeOcrRunner(text="", mean_confidence=0.0, word_count=0),
]
_IDS = ["real-tesseract-runner", "fake"]


@pytest.mark.parametrize("factory", _CRASHING_FACTORIES, ids=_IDS)
def test_detect_orientation_raises_engine_crash_propagates_out_of_extract(factory: Callable[[], Any]) -> None:
    """A non-Tesseract engine crash during OSD propagates out of ``extract``
    with its message intact (the orchestrator dead-letters on it).

    Sabotage proof: in ``TesseractRunner.detect_orientation`` widen the
    ``except self._pyt.TesseractError`` to ``except Exception``. Re-run: the
    real case fails because the crash is swallowed into an upright-page
    result and ``extract`` returns a document. Restored.
    """
    runner = factory()

    with pytest.raises(RuntimeError, match="signal 11"):
        _extract_with(runner)


@pytest.mark.parametrize("factory", _BLANK_FACTORIES, ids=_IDS)
def test_recognise_text_returns_empty_document_fails_quality_gate(factory: Callable[[], Any]) -> None:
    """Nothing recognised: ``extract`` returns a one-page, empty,
    zero-confidence document and ``quality_ok`` escalates it.

    Sabotage proof: in ``TesseractRunner.recognise_text`` change
    ``mean_conf = float(np.mean(confs)) if confs else 0.0`` to
    ``... else 100.0``. Re-run: the real case fails on the
    ``confidence == 0.0`` assertion. Restored.
    """
    extractor, doc = _extract_with(factory())

    assert doc.markdown == ""
    assert len(doc.pages) == 1
    assert doc.confidence == 0.0
    assert extractor.quality_ok(doc) is False
