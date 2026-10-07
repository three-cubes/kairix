"""F68 (ADR-024 Bundle A) — failure-mode contract for ``_MarkitdownConverter``.

``_MarkitdownConverter.convert(source)`` is the ``markitdown.MarkItDown``
wire shape the ``markitdown`` extractor delegates to. Its characteristic
failure is *silent*: on an image-only (scanned) PDF it returns an empty
result rather than raising. The extractor must turn that into an empty,
zero-confidence document that fails ``quality_ok`` (so the escalation
chain hands it to OCR) and must not leave its scratch file behind.

One body, two converter implementations (F43 limb 2), both driven
through the real :class:`MarkitdownExtractor`:

* the real ``MarkItDown`` converter (default factory) fed the recorded
  ``scanned_sample.pdf`` fixture;
* :class:`tests.fakes.FakeMarkitdownConverter` returning empty markdown,
  injected through ``converter_factory``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kairix.extractors.markitdown import MarkitdownExtractor
from kairix.extractors.markitdown import version as markitdown_version
from tests.fakes import FakeMarkitdownConverter

pytestmark = pytest.mark.contract

_SCANNED_PDF = Path(__file__).resolve().parent.parent / "fixtures" / "extractors" / "scanned_sample.pdf"


def _real(scratch: Path) -> MarkitdownExtractor:
    return MarkitdownExtractor(version=markitdown_version, scratch_dir=scratch)


def _fake(scratch: Path) -> MarkitdownExtractor:
    return MarkitdownExtractor(
        version=markitdown_version,
        converter_factory=lambda: FakeMarkitdownConverter(markdown="", title=None),
        scratch_dir=scratch,
    )


_FACTORIES: list[Callable[[Path], MarkitdownExtractor]] = [_real, _fake]


@pytest.mark.parametrize("factory", _FACTORIES, ids=["real-markitdown", "fake"])
def test_convert_returns_empty_for_scanned_pdf_document_escalates_without_orphan_scratch(
    factory: Callable[[Path], MarkitdownExtractor],
    tmp_path: Path,
) -> None:
    """An empty conversion yields an empty, zero-confidence, untitled
    document that fails ``quality_ok``; the scratch dir is left empty.

    Sabotage proof: in ``MarkitdownExtractor.extract`` delete the
    ``tmp_path.unlink()`` in the ``finally`` block. Re-run: both cases
    fail because the scratch PDF is orphaned in ``scratch_dir``. Restored.
    """
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    extractor = factory(scratch)

    doc = extractor.extract(_SCANNED_PDF.read_bytes(), "application/pdf")

    assert (doc.markdown, doc.confidence, doc.metadata.title) == ("", 0.0, None)
    assert extractor.quality_ok(doc) is False
    assert list(scratch.iterdir()) == []
