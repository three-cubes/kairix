"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`Extractor`.

Every public method on :class:`kairix.core.protocols.Extractor` has at
least one test here that exercises a named failure class
(``raises`` / ``times_out`` / ``returns_partial`` / ``returns_empty`` /
``unauthorized`` / ``unavailable``) AND asserts a CONCRETE observable
outcome.

Every body runs over BOTH a shipped extractor and the canonical
:class:`tests.fakes.FakeExtractor` (F43 behavioural parity). The shipped
side is the :mod:`kairix.extractors.passthrough` plugin (the plain-text
extractor the generic fake mirrors), except where the failure needs an
extractor with a failing dependency: ``extract`` raising uses the real
:class:`kairix.extractors.docx.DocxExtractor` with its
``document_opener`` seam rejecting a corrupt container. No shipped
extractor's ``metadata_for`` raises (all guard), so that probe runs the
real passthrough with its ``metadata_for`` overridden to fail — a
minimal probe of a buggy plugin, per the PLA-472 ruling.

Composition follows F47 — pipelines are built via
:func:`kairix.core.factory.build_connector_pipeline`.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

import sqlite3
import zipfile
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest

from kairix.core.db.schema import create_schema
from kairix.core.factory import build_connector_pipeline
from kairix.core.protocols import ChangeEvent, DocMetadata, ExtractedDocument, Extractor, SourceMetadata
from kairix.extractors.docx import DocxExtractor
from kairix.extractors.docx import version as docx_version
from kairix.extractors.passthrough import PassthroughExtractor
from kairix.extractors.passthrough import version as passthrough_version
from tests.fakes import FakeChunkWriter, FakeEntityGraphSink, FakeExtractor, FakeSourceConnector

pytestmark = pytest.mark.contract

ExtractorFactory = Callable[[], Extractor]


def _passthrough() -> Extractor:
    return PassthroughExtractor(version=passthrough_version)


def _fake_text_extractor() -> Extractor:
    return FakeExtractor(claimed_mime_prefixes=("text/",))


_TEXT_IMPLEMENTATIONS: list[tuple[str, ExtractorFactory]] = [
    ("real", _passthrough),
    ("fake", _fake_text_extractor),
]


def _corrupt_container_opener() -> Callable[[str], Any]:
    def _open(path: str) -> Any:
        raise zipfile.BadZipFile(f"File is not a zip file: {path}")

    return _open


class _MetadataBugProbe(PassthroughExtractor):
    """The shipped passthrough plugin with a buggy ``metadata_for``."""

    def metadata_for(self, raw: bytes, mime: str) -> SourceMetadata:
        raise RuntimeError("F68-extractor-metadata-raises")


_EXTRACT_RAISES: list[tuple[str, ExtractorFactory]] = [
    ("real", lambda: DocxExtractor(version=docx_version, document_opener=_corrupt_container_opener)),
    ("fake", lambda: FakeExtractor(raise_on_extract=zipfile.BadZipFile("File is not a zip file"))),
]

_METADATA_RAISES: list[tuple[str, ExtractorFactory]] = [
    ("real", lambda: _MetadataBugProbe(version=passthrough_version)),
    ("fake", lambda: FakeExtractor(raise_on_metadata_for=RuntimeError("F68-extractor-metadata-raises"))),
]


# ---------------------------------------------------------------------------
# Helpers — factory-composed pipeline with canonical fakes (F47-compliant).
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    create_schema(conn)
    yield conn
    conn.close()


def _run(db: sqlite3.Connection, writer: FakeChunkWriter, source_name: str, extractor: Extractor) -> Any:
    pipeline = build_connector_pipeline(
        db=db, collection="default", chunk_writer=writer, entity_graph_sink=FakeEntityGraphSink()
    )
    source = FakeSourceConnector(
        name=source_name,
        events=[ChangeEvent(op="created", item_id="item-001", modified_at="2026-01-01T00:00:00Z")],
        content={"item-001": b"some body content"},
    )
    return pipeline.run_batch(source, extractor)


def _dead_letter_rows(db: sqlite3.Connection, source_name: str) -> list[tuple[str, str]]:
    return list(
        db.execute(
            "SELECT item_id, last_error FROM connector_deadletter WHERE source_name = ? ORDER BY item_id",
            (source_name,),
        ).fetchall()
    )


def _doc(markdown: str) -> ExtractedDocument:
    return ExtractedDocument(
        markdown=markdown,
        pages=(),
        images=(),
        metadata=DocMetadata(title=None, author=None, created_date=None, language=None, page_count=None),
        confidence=1.0,
    )


# ---------------------------------------------------------------------------
# Extractor.can_extract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,factory", _TEXT_IMPLEMENTATIONS)
def test_can_extract_raises_propagates_when_called_in_isolation(name: str, factory: ExtractorFactory) -> None:
    """``can_extract`` is invoked by the extractor escalation chain, not by
    ``ConnectorPipeline._process_item`` directly, so the proof is at the
    Protocol-method boundary: a malformed (non-``str``) mime hint raises
    cleanly instead of being silently claimed.

    Sabotage proof: in ``PassthroughExtractor.can_extract`` return
    ``isinstance(mime, str) and mime.startswith(...)``. Re-run: the
    ``real`` case fails because no exception fires. Restored.
    """
    malformed_mime = cast(str, cast(Any, None))
    with pytest.raises(AttributeError):
        factory().can_extract(malformed_mime, b"\x00\x00")


@pytest.mark.parametrize("name,factory", _TEXT_IMPLEMENTATIONS)
def test_can_extract_returns_empty_for_unsupported_mime(name: str, factory: ExtractorFactory) -> None:
    """``returns_empty`` — the "no extractor matches" path is
    ``can_extract`` returning False (the escalation chain's terminal
    condition) for a mime outside the plugin's claim; its own mimes are
    still claimed.

    Sabotage proof (executed): in ``PassthroughExtractor.can_extract``
    ``return True``. Re-run: the ``real`` case fails. Restored.
    """
    extractor = factory()
    assert extractor.can_extract("application/octet-stream", b"") is False, name
    assert extractor.can_extract("text/markdown", b"# hi") is True, name


# ---------------------------------------------------------------------------
# Extractor.extract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,factory", _EXTRACT_RAISES)
def test_extract_raises_dead_letters_item_and_chunk_writer_not_called(
    name: str, factory: ExtractorFactory, db: sqlite3.Connection
) -> None:
    """When ``extractor.extract`` raises (a corrupt container), ``_process_item``
    records the item in the dead-letter table AND the chunk writer is NEVER
    called for that item (the silver pass is skipped).

    Sabotage proof (executed): in ``DocxExtractor.extract`` catch the
    opener's exception and render an empty document. Re-run: the ``real``
    case fails because the item is processed instead of dead-lettered.
    Restored.
    """
    writer = FakeChunkWriter()
    result = _run(db, writer, "extract-raises", factory())

    rows = _dead_letter_rows(db, "extract-raises")
    assert result.processed == 0, name
    assert result.dead_lettered == 1, name
    assert [r[0] for r in rows] == ["item-001"], name
    assert "zip" in rows[0][1].lower(), f"{name}: dead-letter must carry the extractor's error; got {rows[0][1]!r}"
    # Critical: writer NEVER called when extract raised.
    assert writer.writes == [], f"{name}: chunk writer must not be called when extract raises; got {writer.writes!r}"


# ---------------------------------------------------------------------------
# Extractor.quality_ok
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,factory", _TEXT_IMPLEMENTATIONS)
def test_quality_ok_returns_empty_signals_escalation_required(name: str, factory: ExtractorFactory) -> None:
    """``quality_ok`` returning False is the escalation signal — the
    canonical ``returns_empty`` failure class. A whitespace-only body
    carries no useful content; a real body passes.

    Sabotage proof (executed): in ``PassthroughExtractor.quality_ok``
    ``return True``. Re-run: the ``real`` case fails. Restored.
    """
    extractor = factory()
    assert extractor.quality_ok(_doc("   \n\t ")) is False, name
    assert extractor.quality_ok(_doc("adequate body")) is True, name


@pytest.mark.parametrize("name,factory", _TEXT_IMPLEMENTATIONS)
def test_quality_ok_raises_propagates_when_called_in_isolation(name: str, factory: ExtractorFactory) -> None:
    """``raises`` failure class for ``quality_ok`` at the Protocol-method
    boundary: handed no document (an upstream extract that produced
    nothing), the gate raises rather than reporting a verdict.

    Sabotage proof: in ``PassthroughExtractor.quality_ok`` return
    ``bool(getattr(doc, "markdown", "").strip())``. Re-run: the ``real``
    case fails because no exception fires. Restored.
    """
    missing = cast(ExtractedDocument, cast(Any, None))
    with pytest.raises(AttributeError):
        factory().quality_ok(missing)


# ---------------------------------------------------------------------------
# Extractor.metadata_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,factory", _METADATA_RAISES)
def test_metadata_for_raises_silver_falls_back_chunk_indexed(
    name: str, factory: ExtractorFactory, db: sqlite3.Connection
) -> None:
    """ADR-021 — ``extractor.metadata_for`` raising is NEVER fatal.
    :func:`_safe_extractor_metadata` absorbs the exception and silver
    proceeds with the connector-side metadata only. The chunk indexes.

    Sabotage proof: in
    ``kairix/core/connectors/pipeline.py:_safe_extractor_metadata``,
    change ``except Exception: return SourceMetadata()`` to
    ``except Exception: raise``. Re-run: both cases fail because the
    pipeline now propagates the RuntimeError. Restored.
    """
    writer = FakeChunkWriter()
    result = _run(db, writer, "extractor-metadata-raises", factory())

    assert result.processed == 1, name
    assert result.dead_lettered == 0, name
    assert len(writer.writes) == 1, f"{name}: writer must receive exactly one chunk batch; got {writer.writes!r}"
    assert _dead_letter_rows(db, "extractor-metadata-raises") == [], name


@pytest.mark.parametrize("name,factory", _TEXT_IMPLEMENTATIONS)
def test_metadata_for_returns_empty_when_no_override_configured(name: str, factory: ExtractorFactory) -> None:
    """``returns_empty`` — plain text carries no body-derived metadata,
    so the extractor returns an empty :class:`SourceMetadata` (never None).

    Sabotage proof: in ``PassthroughExtractor.metadata_for`` return
    ``None`` when no frontmatter parses. Re-run: the ``real`` case fails
    on the ``isinstance`` assertion. Restored.
    """
    md = factory().metadata_for(b"any bytes", "text/plain")
    assert isinstance(md, SourceMetadata), name
    assert md.author is None, name
    assert md.modified_at is None, name
