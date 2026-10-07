"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`SilverProcessor`.

One method (``process``). Per the Protocol docstring + F38, Silver is
the singular chunking + entity-signal surface; failures must surface
typed exceptions so the orchestrator can route to dead-letter rather
than silently emit zero chunks (which would tombstone the document
on the next sync).

Failure surface:

  * ``returns_empty`` — empty markdown body yields a SilverOutput with
    no chunks and no signals (the documented "nothing to process" shape).
  * ``raises`` — surfaces typed exception when chunking crashes; pins
    the contract that ``process`` does NOT silently swallow.

F43: every test runs ONE assertion body over the real
:class:`kairix.core.connectors.silver.DefaultSilverProcessor` (failure
injected through its ``chunker_registry`` seam with
:class:`tests.fakes.FakeChunkerRegistry`) AND
:class:`tests.fakes.FakeSilverProcessor`.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.core.connectors.silver import DefaultSilverProcessor
from kairix.core.protocols import (
    BronzeRef,
    DocMetadata,
    ExtractedDocument,
    SilverProcessor,
)
from tests.fakes import FakeChunkerRegistry, FakeSilverProcessor

pytestmark = pytest.mark.contract

SilverFactory = Callable[[BaseException | None], SilverProcessor]


def _bronze_ref() -> BronzeRef:
    return BronzeRef(
        source_name="src",
        item_id="item-001",
        raw_path="src/item-001.bin",
        mime="text/plain",
        fetched_at="2026-01-01T00:00:00Z",
    )


def _doc(markdown: str) -> ExtractedDocument:
    return ExtractedDocument(
        markdown=markdown,
        pages=(),
        images=(),
        metadata=DocMetadata(title=None, author=None, created_date=None, language=None, page_count=None),
        confidence=1.0,
    )


def _real(raises: BaseException | None) -> SilverProcessor:
    """Real Silver; a chunking crash is injected through the ``chunker_registry`` seam."""
    if raises is None:
        return DefaultSilverProcessor()
    return DefaultSilverProcessor(chunker_registry=FakeChunkerRegistry(raises=raises))


def _fake(raises: BaseException | None) -> SilverProcessor:
    return FakeSilverProcessor(raises=raises)


_IMPLS = [_real, _fake]


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_process_raises_propagates_typed_exception(factory: SilverFactory) -> None:
    """A Silver processing failure surfaces — orchestrator must NOT
    silently emit zero chunks because that would tombstone the document
    on the next sync (the slim-prune cycle reads chunk count to decide).

    Sabotage proof: in ``kairix.core.connectors.silver.DefaultSilverProcessor._dispatch_markdown_chunks``
    wrap the ``dispatch`` + ``chunk`` calls in ``try/except Exception: return ()``.
    Re-run: the real leg's pytest.raises sees nothing. Restored.
    """
    proc = factory(RuntimeError("F68-silver-raises"))
    with pytest.raises(RuntimeError, match="F68-silver-raises"):
        proc.process(
            raw=_bronze_ref(),
            extracted=_doc("# Heading\n\nA paragraph Silver has to chunk."),
            source_uri="https://example.com/item-001",
            source_modified_at="2026-01-01T00:00:00Z",
            sensitivity="public",
        )


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_process_returns_empty_when_markdown_body_empty(factory: SilverFactory) -> None:
    """An extracted document with empty markdown yields a SilverOutput
    with no chunks and no signals — the "nothing to process" shape.

    Sabotage proof: in ``DefaultSilverProcessor.process`` change
    ``chunk_texts = _chunk_markdown(extracted.markdown)`` to
    ``... or ["phantom"]``. Re-run: the real leg's ``chunks == ()``
    assertion fails. Restored.
    """
    proc = factory(None)
    out = proc.process(
        raw=_bronze_ref(),
        extracted=_doc(""),
        source_uri="https://example.com/item-001",
        source_modified_at="2026-01-01T00:00:00Z",
        sensitivity="public",
    )
    assert out.chunks == (), f"empty markdown must yield no chunks; got {len(out.chunks)}"
    assert out.entity_signals == (), f"empty markdown must yield no entity signals; got {len(out.entity_signals)}"
