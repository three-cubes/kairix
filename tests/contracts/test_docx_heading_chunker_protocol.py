"""Contract tests for :class:`kairix.chunkers.docx_heading.DocxHeadingChunker` (F43).

Pins the :class:`~kairix.core.protocols.Chunker` Protocol invariants with
ONE body per invariant, run over BOTH the shipped plugin (built through
its ``make_chunker`` entry-point factory) and the canonical
:class:`tests.fakes.FakeParagraphChunker` (F43 behavioural parity):

* satisfies the :class:`Chunker` Protocol and declares a non-empty
  ``version: str`` matching its declaration site (F55);
* every emitted :class:`Chunk` carries ``chunker_version=self.version``
  (F55) and the input ``source_uri`` (F39);
* empty / whitespace-only input emits no chunks.

Plugin-specific behaviour (heading-path breadcrumbs, plugin name) lives
in ``tests/unit/test_docx_heading_chunker_units.py``.

Sabotage-prove targets:
- chunker_version flow: drop ``chunker_version=self.version`` in
  ``_build_section_chunk`` → confirm test_chunk_carries_version fails
  for the ``real`` case → restore.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.chunkers.docx_heading import make_chunker, version
from kairix.core.protocols import Chunk, Chunker
from tests.fakes import FakeParagraphChunker

pytestmark = pytest.mark.contract

# (name, factory, declared version, representative non-empty input)
_IMPLEMENTATIONS: list[tuple[str, Callable[[], Chunker], str, str]] = [
    ("real", make_chunker, version, "# Chapter\n\nFirst paragraph of the chapter."),
    ("fake", FakeParagraphChunker, FakeParagraphChunker.version, "First paragraph.\n\nSecond paragraph."),
]
_IDS = [impl[0] for impl in _IMPLEMENTATIONS]


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_satisfies_chunker_protocol(name: str, factory: Callable[[], Chunker], declared: str, sample: str) -> None:
    """The instance is a runtime :class:`Chunker` with a non-empty F55
    ``version`` equal to its declaration site."""
    chunker = factory()
    assert isinstance(chunker, Chunker), name
    assert isinstance(chunker.version, str) and chunker.version.strip() != "", name
    assert chunker.version == declared, name


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_empty_input_yields_no_chunks(name: str, factory: Callable[[], Chunker], declared: str, sample: str) -> None:
    chunker = factory()
    assert chunker.chunk(text="", section_kind="text", source_uri="x.docx") == (), name
    assert chunker.chunk(text="   \n  ", section_kind="text", source_uri="x.docx") == (), name


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_chunk_carries_version(name: str, factory: Callable[[], Chunker], declared: str, sample: str) -> None:
    """F55 + F39: every emitted Chunk carries ``chunker_version=self.version``
    and the input ``source_uri``; the return is a tuple."""
    chunker = factory()
    chunks = chunker.chunk(text=sample, section_kind="text", source_uri="x.docx")
    assert isinstance(chunks, tuple), name
    assert len(chunks) >= 1, name
    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.chunker_version == declared, name
        assert c.source_uri == "x.docx", name
