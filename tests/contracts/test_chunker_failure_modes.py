"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`Chunker`.

Single Protocol method ``chunk(*, text, section_kind, source_uri)``.
Two operationally-relevant failure shapes:

  * ``returns_empty`` — empty input text MUST yield zero chunks.
    Production wraps :class:`Chunker` plugins behind the silver
    processor; the silver code paths assume "no chunks for empty
    text" is safe (it's the documented contract).
  * ``raises`` — when a chunker is handed a section it cannot process
    (a non-``str`` body — e.g. an image section with no OCR text), the
    exception must propagate to the orchestrator so the per-batch
    transaction rolls back, never be masked as an empty tuple.

Every body runs over BOTH the shipped
:class:`kairix.chunkers.docx_heading.DocxHeadingChunker` and the
canonical :class:`tests.fakes.FakeParagraphChunker` (F43 behavioural parity).

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from kairix.chunkers.docx_heading import DocxHeadingChunker
from kairix.core.protocols import Chunk, Chunker
from tests.fakes import FakeParagraphChunker

pytestmark = pytest.mark.contract

_IMPLEMENTATIONS: list[tuple[str, Callable[[], Chunker]]] = [
    ("real", DocxHeadingChunker),
    ("fake", FakeParagraphChunker),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_chunk_returns_empty_when_text_is_empty(name: str, factory: Callable[[], Chunker]) -> None:
    """Empty / whitespace-only text MUST yield zero chunks — the silver
    layer relies on this to skip writes for blank documents.

    Sabotage proof (executed): in :meth:`DocxHeadingChunker.chunk` change
    the empty-input ``return ()`` to ``return (None,)``. Re-run: the
    ``real`` case fails. Restored.
    """
    chunker = factory()
    for text in ("", "   \n  \t "):
        result: tuple[Chunk, ...] = chunker.chunk(text=text, section_kind="text", source_uri="file:///empty.docx")
        assert result == (), f"{name}: empty text must yield empty tuple; got {result!r}"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_chunk_raises_when_underlying_implementation_fails(name: str, factory: Callable[[], Chunker]) -> None:
    """A chunker handed a section body it cannot process must surface
    the exception — silent fallback to an empty tuple would mask the
    failure and let bad input flow downstream.

    Sabotage proof: in :meth:`DocxHeadingChunker.chunk` change
    ``stripped = text.strip()`` to ``stripped = str(text or "").strip()``.
    Re-run: the ``real`` case fails because no exception fires. Restored.
    """
    chunker = factory()
    malformed = cast(str, cast(Any, None))  # a section with no text body
    with pytest.raises(AttributeError):
        chunker.chunk(text=malformed, section_kind="image", source_uri="file:///x.docx")
