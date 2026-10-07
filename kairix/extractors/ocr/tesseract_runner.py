"""Thin wrapper around :mod:`pytesseract` for the OCR extractor (MM-2).

Keeps the upstream library import and the per-call CLI flags in one
place so the rest of the OCR extractor can stay pure ``numpy``. The
wrapper also provides the canonical test seam — the
:class:`OcrExtractor` accepts an injected ``ocr_runner`` so a test
can pass a scripted fake without monkeypatching :mod:`pytesseract`
(F1-clean).

The wrapper exposes two operations:

* :meth:`TesseractRunner.detect_orientation` — Tesseract ``--psm 0``,
  returns the orientation in degrees (0 / 90 / 180 / 270) and a
  confidence score.
* :meth:`TesseractRunner.recognise_text` — Tesseract default PSM,
  returns the recognised text plus an average per-word confidence
  in ``[0, 100]`` (matching Tesseract's native scale; the extractor
  re-normalises to ``[0, 1]`` for the :class:`ExtractedDocument`).

A dataclass result type per operation makes the seam F42-clean
(no ``dict[str, Any]`` at the boundary).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np


@dataclass(frozen=True)
class OrientationResult:
    """Output of :meth:`OcrRunner.detect_orientation`."""

    #: Detected rotation in degrees ∈ ``{0, 90, 180, 270}`` — the
    #: clockwise angle the image must be rotated by to read upright.
    rotation_degrees: int
    #: Tesseract's orientation-confidence value, raw.
    orientation_confidence: float


@dataclass(frozen=True)
class RecognitionResult:
    """Output of :meth:`OcrRunner.recognise_text`."""

    #: The concatenated recognised text.
    text: str
    #: Mean per-word confidence in ``[0, 100]`` — Tesseract's native
    #: scale. The extractor normalises to ``[0, 1]`` for the
    #: :class:`ExtractedDocument`.
    mean_confidence: float
    #: Number of words whose confidence was considered (i.e. were not
    #: ``-1`` placeholders in Tesseract's ``image_to_data`` output).
    word_count: int


class OcrRunner(Protocol):
    """Wire-shape Protocol the OCR extractor consumes.

    Both the production :class:`TesseractRunner` and the test fakes
    implement this Protocol. The Protocol is module-private (lives
    next to its only consumer) because no other code in kairix
    consumes the OCR layer's runner abstraction — the extractor's
    public surface is the :class:`Extractor` Protocol.
    """

    def detect_orientation(self, img: np.ndarray) -> OrientationResult:
        """Detect the clockwise rotation needed to read ``img`` upright."""

    def recognise_text(self, img: np.ndarray) -> RecognitionResult:
        """Recognise text + per-word confidence on ``img``."""


def _pytesseract() -> Any:
    """Lazy-import :mod:`pytesseract`.

    Raises a typed ``RuntimeError`` if the ``ocr`` extra is not
    installed; the message carries a ``fix:`` action so the agent
    reading the gate failure knows what to install.
    """
    try:
        import pytesseract
    except ImportError as exc:  # pragma: no cover — import path validated by test
        raise RuntimeError(
            "ocr: the 'pytesseract' package is not installed. "
            "fix: pip install 'Kairix-agentic-knowledge-mgt[ocr]' "
            "to opt into the OCR extractor. "
            "next: re-run the connector sync; the OCR plugin will then resolve."
        ) from exc
    return pytesseract


class TesseractRunner:
    """Production :class:`OcrRunner` — delegates to :mod:`pytesseract`.

    The runner is stateless; constructing it eagerly resolves the
    :mod:`pytesseract` import so a missing extra fails fast with the
    typed ``fix:`` message rather than deferring the failure to the
    first ``extract`` call. Tests construct a scripted fake instead
    of this class.
    """

    def __init__(self, pytesseract_module: Any = None) -> None:
        """Resolve :mod:`pytesseract` once at construction time.

        ``pytesseract_module`` is the DI seam: production leaves it
        ``None`` and the real :mod:`pytesseract` is imported; tests pass
        a module-shaped fake (``image_to_osd`` / ``image_to_data`` /
        ``Output`` / ``TesseractError``) to drive the failure paths
        without the Tesseract binary.
        """
        self._pyt = pytesseract_module if pytesseract_module is not None else _pytesseract()

    def detect_orientation(self, img: np.ndarray) -> OrientationResult:
        """Tesseract ``--psm 0`` — orientation + script detection."""
        try:
            osd = self._pyt.image_to_osd(img, output_type=self._pyt.Output.DICT)
        except self._pyt.TesseractError:  # pragma: no cover — exercised via fake in tests
            return OrientationResult(rotation_degrees=0, orientation_confidence=0.0)
        rotation = int(osd.get("rotate", 0))
        confidence = float(osd.get("orientation_conf", 0.0))
        return OrientationResult(rotation_degrees=rotation, orientation_confidence=confidence)

    def recognise_text(self, img: np.ndarray) -> RecognitionResult:
        """Tesseract default PSM — recognise text + per-word confidence."""
        data = self._pyt.image_to_data(img, output_type=self._pyt.Output.DICT)
        words: list[str] = list(data.get("text", []))
        confidences: list[Any] = list(data.get("conf", []))
        text_lines, confs = _split_text_and_confidences(words, confidences)
        text = "\n".join(text_lines).strip()
        mean_conf = float(np.mean(confs)) if confs else 0.0
        return RecognitionResult(text=text, mean_confidence=mean_conf, word_count=len(confs))


def _split_text_and_confidences(
    words: list[str],
    confidences: list[Any],
) -> tuple[list[str], list[float]]:
    """Pair words with their confidence values.

    Tesseract reports ``-1`` for layout-only rows (page / block /
    paragraph headers); we filter those out before averaging.
    Empty word strings (blank lines between paragraphs) are skipped
    for the average but preserved in the recognised text via
    paragraph-boundary newlines.
    """
    text_pieces: list[str] = []
    valid_confidences: list[float] = []
    for word, conf in zip(words, confidences, strict=False):
        if not word or not word.strip():
            continue
        try:
            conf_value = float(conf)
        except (TypeError, ValueError):
            continue
        if conf_value < 0:
            continue
        text_pieces.append(word)
        valid_confidences.append(conf_value)
    return text_pieces, valid_confidences


__all__ = [
    "OcrRunner",
    "OrientationResult",
    "RecognitionResult",
    "TesseractRunner",
]
