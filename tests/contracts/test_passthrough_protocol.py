"""Contract test for the ``passthrough`` extractor plugin (F43).

Imports the canonical fake AND the real implementation, then runs the
same :class:`Extractor` Protocol assertions against both. The fake
proves the test seam is real; the real impl proves the production
class satisfies the same shape.

Sabotage-proofs:

  * Deleting ``version`` from :mod:`kairix.extractors.passthrough`
    breaks ``test_extractor_declares_version``.
  * Flipping ``can_extract`` to ``return False`` on the real impl
    breaks ``test_can_extract_text_mime_round_trip`` (real branch).
  * Flipping ``extract`` to drop the ``markdown`` decode breaks
    ``test_extract_round_trips_markdown_bytes`` on both branches via
    the shared assertion.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.extractors import ExtractedDocument, Extractor
from kairix.extractors.passthrough import (
    PassthroughExtractor,
)
from kairix.extractors.passthrough import (
    make_extractor as make_real_extractor,
)
from kairix.extractors.passthrough import (
    version as passthrough_version,
)
from tests.fakes import FakePassthroughExtractor

pytestmark = pytest.mark.contract


_Factory = Callable[[], Extractor]


@pytest.fixture(
    params=[
        pytest.param(lambda: FakePassthroughExtractor(), id="fake"),
        pytest.param(make_real_extractor, id="real"),
    ]
)
def _extractor(request: pytest.FixtureRequest) -> Extractor:
    factory: _Factory = request.param
    return factory()


@pytest.mark.contract
@pytest.mark.parametrize(
    "factory,expected_cls",
    [
        pytest.param(make_real_extractor, PassthroughExtractor, id="real"),
        pytest.param(FakePassthroughExtractor, FakePassthroughExtractor, id="fake"),
    ],
)
def test_passthrough_extractor_satisfies_protocol(factory: _Factory, expected_cls: type) -> None:
    """Each factory returns its concrete class, a runtime ``Extractor``
    registered under the ``passthrough`` plugin name.

    Sabotage proof: change ``PLUGIN_NAME`` in
    kairix/extractors/passthrough/extractor.py to ``"passthru"`` — the
    real leg's name assertion fails.
    """
    impl = factory()
    assert isinstance(impl, Extractor)
    assert isinstance(impl, expected_cls)
    assert impl.name == "passthrough"


@pytest.mark.contract
@pytest.mark.parametrize(
    "factory",
    [pytest.param(make_real_extractor, id="real"), pytest.param(FakePassthroughExtractor, id="fake")],
)
def test_extractor_declares_version(factory: _Factory) -> None:
    """F40 requirement — module-level ``version`` is non-empty and every
    impl carries it (the fake mirrors the plugin's declared version).

    Sabotage proof: make ``make_extractor`` in
    kairix/extractors/passthrough/__init__.py pass ``version="0.0.0"`` —
    the real leg's equality fails.
    """
    assert isinstance(passthrough_version, str)
    assert passthrough_version.strip() != ""
    assert factory().version == passthrough_version


@pytest.mark.contract
def test_can_extract_text_mime_round_trip(_extractor: Extractor) -> None:
    """Both fake and real claim ``text/markdown`` and ``text/plain``."""
    assert _extractor.can_extract("text/markdown", b"") is True
    assert _extractor.can_extract("text/plain", b"") is True


@pytest.mark.contract
def test_can_extract_rejects_pdf_mime(_extractor: Extractor) -> None:
    """Both fake and real refuse ``application/pdf``."""
    assert _extractor.can_extract("application/pdf", b"%PDF") is False


@pytest.mark.contract
def test_extract_round_trips_markdown_bytes(_extractor: Extractor) -> None:
    """``extract`` decodes UTF-8 bytes into the document's ``markdown`` field."""
    raw = b"# Title\n\nbody\n"
    doc = _extractor.extract(raw, "text/markdown")
    assert isinstance(doc, ExtractedDocument)
    assert doc.markdown == "# Title\n\nbody\n"
    assert doc.pages == ()
    assert doc.images == ()


@pytest.mark.contract
def test_quality_ok_for_non_empty_document(_extractor: Extractor) -> None:
    doc = _extractor.extract(b"hello world\n", "text/plain")
    assert _extractor.quality_ok(doc) is True


@pytest.mark.contract
def test_quality_ok_false_for_empty_document(_extractor: Extractor) -> None:
    doc = _extractor.extract(b"", "text/plain")
    assert _extractor.quality_ok(doc) is False
