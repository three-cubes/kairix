"""Contract tests for :class:`MarkdownStructuralChunker` (ADR-028 Wave G.1).

Pins:
  * Plugin instance satisfies the
    :class:`kairix.core.protocols.Chunker` runtime-checkable Protocol and
    declares a non-empty ``version`` (F55).
  * Every emitted :class:`Chunk` carries ``chunker_version=`` matching
    the chunker instance's version (F55).
  * Empty / whitespace-only input emits no chunks.
  * Single-line markdown with no headings still emits one chunk that
    carries the chunker's version.
  * The ``make_chunker`` factory returns an instance whose ``version``
    matches the module-level ``version`` constant.

F43 parity: every body runs over BOTH the real
:class:`kairix.chunkers.markdown_structural.MarkdownStructuralChunker`
(built through its ``make_chunker`` factory) AND the canonical generic
:class:`tests.fakes.FakeChunker`.

Sabotage proofs are recorded per test below (production code mutated,
real leg confirmed failing, restored).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.chunkers.markdown_structural import (
    MarkdownStructuralChunker,
    make_chunker,
    version,
)
from kairix.core.protocols import Chunk, Chunker
from tests.fakes import FakeChunker

pytestmark = pytest.mark.contract

_IMPLS: list[Callable[[], Chunker]] = [make_chunker, FakeChunker]
_IDS = ["real", "fake"]


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_plugin_satisfies_chunker_protocol(factory: Callable[[], Chunker]) -> None:
    """Sabotage proof: renamed ``MarkdownStructuralChunker.chunk`` to
    ``_chunk`` → the real leg's isinstance(Chunker) failed. Restored."""
    chunker = factory()
    assert isinstance(chunker, Chunker)
    assert isinstance(chunker.version, str)
    assert chunker.version  # non-empty (F55)


@pytest.mark.parametrize(
    ("factory", "expected_cls", "expected_version"),
    [
        (make_chunker, MarkdownStructuralChunker, version),
        (FakeChunker, FakeChunker, FakeChunker.version),
    ],
    ids=_IDS,
)
def test_factory_returns_real_class(factory: Callable[[], Chunker], expected_cls: type, expected_version: str) -> None:
    """The factory returns its concrete class bound to the canonical
    version declaration.

    Sabotage proof: changed ``make_chunker`` to
    ``MarkdownStructuralChunker(version="0.0.0")`` → the real leg's version
    assertion failed. Restored.
    """
    chunker = factory()
    assert isinstance(chunker, expected_cls)
    assert chunker.version == expected_version


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_emitted_chunks_carry_plugin_version(factory: Callable[[], Chunker]) -> None:
    """Every Chunk emitted carries ``chunker_version=self.version`` (F55).

    Sabotage proof: in ``_build_chunk`` replaced
    ``chunker_version=chunker_version`` with ``chunker_version=None`` → the
    real leg failed the version assertion. Restored.
    """
    chunker = factory()
    text = "# Heading\n\nParagraph body with content.\n"
    chunks = chunker.chunk(text=text, section_kind="text", source_uri="doc.md")
    assert chunks
    for chunk in chunks:
        assert isinstance(chunk, Chunk)
        assert chunk.chunker_version == chunker.version


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_empty_input_emits_no_chunks(factory: Callable[[], Chunker]) -> None:
    """Sabotage proof: in ``_split_into_sections`` removed the
    ``if not text.strip(): return ()`` guard and made ``flush_current``
    append empty bodies → the real leg emitted a chunk for whitespace
    input and failed. Restored."""
    chunker = factory()
    assert chunker.chunk(text="", section_kind="text", source_uri="x") == ()
    assert chunker.chunk(text="   \n   ", section_kind="text", source_uri="x") == ()


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_single_paragraph_emits_one_chunk(factory: Callable[[], Chunker]) -> None:
    """Sabotage proof: in ``MarkdownStructuralChunker.chunk`` returned
    ``tuple(chunks) * 2`` → the real leg's ``len == 1`` failed. Restored."""
    chunker = factory()
    chunks = chunker.chunk(text="just a body line", section_kind="text", source_uri="x")
    assert len(chunks) == 1
    assert chunks[0].chunker_version == chunker.version
    assert "just a body line" in chunks[0].text


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_emitted_chunks_propagate_source_uri(factory: Callable[[], Chunker]) -> None:
    """F39 / Protocol contract: source_uri flows through to every emitted Chunk.

    Sabotage proof: in ``_build_chunk`` hard-coded ``source_uri=""`` → the
    real leg failed. Restored.
    """
    chunker = factory()
    chunks = chunker.chunk(text="# Title\n\nBody", section_kind="text", source_uri="vault/note.md")
    assert chunks
    for chunk in chunks:
        assert chunk.source_uri == "vault/note.md"


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_chunk_method_returns_tuple_not_list(factory: Callable[[], Chunker]) -> None:
    """The Chunker Protocol promises ``tuple[Chunk, ...]`` — Silver
    relies on the tuple shape (frozen / hashable). A list return
    would type-check but break consumers iterating multiple times.

    Sabotage proof: in ``MarkdownStructuralChunker.chunk`` returned
    ``chunks`` (the list) instead of ``tuple(chunks)`` → the real leg's
    isinstance(tuple) failed. Restored.
    """
    chunker = factory()
    result = chunker.chunk(text="# T\n\nbody", section_kind="text", source_uri="x")
    assert isinstance(result, tuple)
