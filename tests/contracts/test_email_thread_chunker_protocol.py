"""Contract tests for :class:`EmailThreadChunker` (ADR-028 Wave G.1).

Pins the :class:`~kairix.core.protocols.Chunker` Protocol invariants with
ONE body per invariant, run over BOTH the shipped plugin (built through
its ``make_chunker`` entry-point factory) and the canonical
:class:`tests.fakes.FakeParagraphChunker` (F43 behavioural parity):

  * The instance satisfies the runtime-checkable :class:`Chunker`
    Protocol and declares a non-empty ``version`` equal to its
    declaration site (F55).
  * Every emitted :class:`Chunk` carries ``chunker_version=`` matching
    the instance's version (F55) and the input ``source_uri`` (F39).
  * Empty / whitespace-only input emits no chunks.
  * ``chunk`` returns a ``tuple``, never a list.

Email-specific behaviour (headers surfaced into ``Chunk.metadata``, the
factory's concrete type, quoted-reply stripping) lives in
``tests/unit/test_email_thread_chunker_units.py``.

Sabotage proof (executed): in ``_build_email_chunk`` change
``chunker_version=chunker_version`` to ``chunker_version="drift"`` → the
``real`` case of ``test_emitted_chunks_carry_plugin_version`` fails.
Restored.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.chunkers.email_thread import make_chunker, version
from kairix.core.protocols import Chunk, Chunker
from tests.fakes import FakeParagraphChunker

pytestmark = pytest.mark.contract


_SINGLE_MESSAGE = """\
Subject: Catch-up
From: agent-alpha@example.test
Date: 2026-05-30
To: agent-beta@example.test

Hello agent-beta, are you free for a quick sync?
"""

# (name, factory, declared version, representative non-empty input)
_IMPLEMENTATIONS: list[tuple[str, Callable[[], Chunker], str, str]] = [
    ("real", make_chunker, version, _SINGLE_MESSAGE),
    ("fake", FakeParagraphChunker, FakeParagraphChunker.version, _SINGLE_MESSAGE),
]
_IDS = [impl[0] for impl in _IMPLEMENTATIONS]


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_plugin_satisfies_chunker_protocol(
    name: str, factory: Callable[[], Chunker], declared: str, sample: str
) -> None:
    chunker = factory()
    assert isinstance(chunker, Chunker), name
    assert chunker.version == declared, name
    assert chunker.version  # non-empty (F55)


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_emitted_chunks_carry_plugin_version(
    name: str, factory: Callable[[], Chunker], declared: str, sample: str
) -> None:
    """Every Chunk carries chunker_version=self.version (F55)."""
    chunker = factory()
    chunks = chunker.chunk(text=sample, section_kind="text", source_uri="msg/1")
    assert chunks, name
    for chunk in chunks:
        assert isinstance(chunk, Chunk)
        assert chunk.chunker_version == declared, name


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_empty_input_emits_no_chunks(name: str, factory: Callable[[], Chunker], declared: str, sample: str) -> None:
    chunker = factory()
    assert chunker.chunk(text="", section_kind="text", source_uri="x") == (), name
    assert chunker.chunk(text="   \n  ", section_kind="text", source_uri="x") == (), name


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_emitted_chunks_propagate_source_uri(
    name: str, factory: Callable[[], Chunker], declared: str, sample: str
) -> None:
    chunker = factory()
    chunks = chunker.chunk(text=sample, section_kind="text", source_uri="thread/42")
    assert chunks, name
    for chunk in chunks:
        assert chunk.source_uri == "thread/42", name


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_chunk_method_returns_tuple_not_list(
    name: str, factory: Callable[[], Chunker], declared: str, sample: str
) -> None:
    assert isinstance(factory().chunk(text=sample, section_kind="text", source_uri="x"), tuple), name
