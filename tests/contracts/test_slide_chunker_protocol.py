"""Contract tests for :class:`kairix.chunkers.slide.SlideChunker` (F43).

Pins:
* SlideChunker satisfies the :class:`~kairix.core.protocols.Chunker`
  Protocol (structural check).
* SlideChunker declares ``version: str`` (F55 + module-level + instance).
* Every emitted :class:`Chunk` carries ``chunker_version=self.version`` (F55).
* Single-slide input collapses to exactly one Chunk.
* Empty / whitespace-only input emits no chunks.

Every test runs ONE assertion body over the real
:class:`~kairix.chunkers.slide.SlideChunker` AND the faithful
:class:`tests.fakes.FakeSlideChunker` (constructed with the plugin's
registered name + version).

Sabotage-prove targets:
- chunker_version flow: drop ``chunker_version=self.version`` in
  ``_build_slide_chunk`` → confirm test_chunk_carries_version fails →
  restore.
- Protocol shape: rename ``chunk(...)`` to ``run(...)`` →
  test_satisfies_chunker_protocol fails → restore.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.chunkers.slide import (
    PLUGIN_NAME,
    SlideChunker,
    make_chunker,
    version,
)
from kairix.core.protocols import Chunk, Chunker
from tests.fakes import FakeSlideChunker

pytestmark = pytest.mark.contract


def _fake() -> FakeSlideChunker:
    return FakeSlideChunker(name=PLUGIN_NAME, version=version)


# (constructor, entry-point factory) per implementation.
_IMPLS: list[tuple[Callable[[], Any], Callable[[], Any]]] = [
    (SlideChunker, make_chunker),
    (_fake, _fake),
]
_IDS = ["real", "fake"]


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_satisfies_chunker_protocol(impl: tuple[Callable[[], Any], Callable[[], Any]]) -> None:
    """Sabotage proof: rename ``SlideChunker.chunk`` to ``run`` → the real
    leg's isinstance check flips to False → restored."""
    construct, _ = impl
    chunker = construct()
    assert isinstance(chunker, Chunker)
    assert chunker.version == version
    assert chunker.name == PLUGIN_NAME


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_make_chunker_returns_slide_chunker(impl: tuple[Callable[[], Any], Callable[[], Any]]) -> None:
    """The entry-point factory yields a Chunker registered as the slide plugin.

    Sabotage proof: make ``kairix.chunkers.slide.make_chunker`` return
    ``SheetRowChunker()`` → the real leg's name assertion fails → restored.
    """
    _, factory = impl
    instance = factory()
    assert isinstance(instance, Chunker)
    assert instance.name == PLUGIN_NAME
    assert instance.version == version


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_module_version_is_non_empty_string(impl: tuple[Callable[[], Any], Callable[[], Any]]) -> None:
    """F55: the plugin version is a non-empty string, carried on the instance.

    Sabotage proof: set ``SlideChunker.__init__``'s ``self.version = ""``
    → the real leg fails → restored.
    """
    construct, _ = impl
    chunker = construct()
    assert isinstance(version, str)
    assert isinstance(chunker.version, str)
    assert chunker.version.strip() != ""


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_empty_input_yields_no_chunks(impl: tuple[Callable[[], Any], Callable[[], Any]]) -> None:
    """Sabotage proof: drop the ``if not stripped: return ()`` guard in
    ``SlideChunker.chunk`` → the real leg emits one blank chunk → restored."""
    construct, _ = impl
    chunker = construct()
    assert chunker.chunk(text="", section_kind="text", source_uri="x.pptx") == ()
    assert chunker.chunk(text="   \n  ", section_kind="text", source_uri="x.pptx") == ()


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_single_page_input_collapses_to_one_chunk(impl: tuple[Callable[[], Any], Callable[[], Any]]) -> None:
    """Silver's per-page driver passes one page-text at a time.

    Sabotage proof: in ``kairix.chunkers.slide._split_on_slide_headers``
    return ``[]`` when no header matches → the real leg yields 0 chunks →
    restored.
    """
    construct, _ = impl
    chunker = construct()
    text = "Just some slide body text without the header"
    chunks = chunker.chunk(text=text, section_kind="text", source_uri="deck.pptx")
    assert len(chunks) == 1
    assert chunks[0].text == text


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_chunk_carries_version(impl: tuple[Callable[[], Any], Callable[[], Any]]) -> None:
    """F55: every emitted Chunk carries ``chunker_version=self.version``.

    Sabotage proof: pass ``chunker_version="sabotaged"`` instead of
    ``chunker_version=chunker_version`` in
    ``kairix.chunkers.slide._build_slide_chunk`` → the real leg's version
    assertion fails → restored.
    """
    construct, _ = impl
    chunker = construct()
    chunks = chunker.chunk(text="slide body", section_kind="text", source_uri="x.pptx")
    assert len(chunks) >= 1
    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.chunker_version == version
        assert c.source_uri == "x.pptx"
