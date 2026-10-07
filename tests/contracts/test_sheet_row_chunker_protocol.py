"""Contract tests for :class:`kairix.chunkers.sheet_row.SheetRowChunker` (F43).

Pins:
* SheetRowChunker satisfies the :class:`~kairix.core.protocols.Chunker`
  Protocol.
* SheetRowChunker declares ``version: str`` (F55).
* Every emitted :class:`Chunk` carries ``chunker_version=self.version`` (F55).
* Empty / whitespace-only input emits no chunks.
* ``small_sheet_threshold`` is configurable.

Every test runs ONE assertion body over the real
:class:`~kairix.chunkers.sheet_row.SheetRowChunker` AND the faithful
:class:`tests.fakes.FakeSheetRowChunker` (constructed with the plugin's
registered name + version).

Sabotage-prove targets:
- chunker_version flow: drop ``chunker_version=self.version`` in
  ``_build_row_chunk`` → confirm test_chunk_carries_version fails →
  restore.
- Small-sheet branch boundary: change ``len(data_rows) <= threshold``
  to ``len(data_rows) < threshold`` → small-sheet integration test
  fails → restore.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.chunkers.sheet_row import (
    PLUGIN_NAME,
    SheetRowChunker,
    make_chunker,
    version,
)
from kairix.core.protocols import Chunk, Chunker
from tests.fakes import FakeSheetRowChunker

pytestmark = pytest.mark.contract


def _fake(**kwargs: Any) -> FakeSheetRowChunker:
    return FakeSheetRowChunker(name=PLUGIN_NAME, version=version, **kwargs)


# (constructor, entry-point factory) per implementation.
_IMPLS: list[tuple[Callable[..., Any], Callable[[], Any]]] = [
    (SheetRowChunker, make_chunker),
    (_fake, _fake),
]
_IDS = ["real", "fake"]


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_satisfies_chunker_protocol(impl: tuple[Callable[..., Any], Callable[[], Any]]) -> None:
    """Sabotage proof: rename ``SheetRowChunker.chunk`` to ``run`` → the
    real leg's isinstance check flips to False → restored."""
    construct, _ = impl
    chunker = construct()
    assert isinstance(chunker, Chunker)
    assert chunker.version == version
    assert chunker.name == PLUGIN_NAME


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_make_chunker_returns_sheet_row_chunker(impl: tuple[Callable[..., Any], Callable[[], Any]]) -> None:
    """The entry-point factory yields a Chunker registered as the sheet-row plugin.

    Sabotage proof: make ``kairix.chunkers.sheet_row.make_chunker`` return
    ``SlideChunker()`` → the real leg's name assertion fails → restored.
    """
    _, factory = impl
    instance = factory()
    assert isinstance(instance, Chunker)
    assert instance.name == PLUGIN_NAME
    assert instance.version == version


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_module_version_is_non_empty_string(impl: tuple[Callable[..., Any], Callable[[], Any]]) -> None:
    """F55: the plugin version is a non-empty string, carried on the instance.

    Sabotage proof: set ``SheetRowChunker.__init__``'s ``self.version = ""``
    → the real leg fails → restored.
    """
    construct, _ = impl
    chunker = construct()
    assert isinstance(version, str)
    assert isinstance(chunker.version, str)
    assert chunker.version.strip() != ""


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_empty_input_yields_no_chunks(impl: tuple[Callable[..., Any], Callable[[], Any]]) -> None:
    """Sabotage proof: in ``SheetRowChunker.chunk`` make the
    ``if not stripped:`` guard return a whole-sheet chunk instead of
    ``()`` → the real leg fails → restored."""
    construct, _ = impl
    chunker = construct()
    assert chunker.chunk(text="", section_kind="tabular", source_uri="x.xlsx") == ()
    assert chunker.chunk(text="   \n  ", section_kind="tabular", source_uri="x.xlsx") == ()


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_small_sheet_threshold_is_configurable(impl: tuple[Callable[..., Any], Callable[[], Any]]) -> None:
    """Sabotage proof: hard-code ``self.small_sheet_threshold = 50`` in
    ``SheetRowChunker.__init__`` → the real leg fails → restored."""
    construct, _ = impl
    chunker = construct(small_sheet_threshold=5)
    assert chunker.small_sheet_threshold == 5


@pytest.mark.parametrize("impl", _IMPLS, ids=_IDS)
def test_chunk_carries_version(impl: tuple[Callable[..., Any], Callable[[], Any]]) -> None:
    """F55: every emitted Chunk carries ``chunker_version=self.version``.

    Sabotage proof: rebind ``chunker_version = "sabotaged"`` at the top
    of ``kairix.chunkers.sheet_row._build_row_chunk`` → the real leg's
    version assertion fails → restored.
    """
    construct, _ = impl
    chunker = construct(small_sheet_threshold=0)  # force row-per-chunk
    sheet_text = "## Sheet: TestSheet\n\n| col_a | col_b |\n| --- | --- |\n| v1 | v2 |\n| v3 | v4 |\n"
    chunks = chunker.chunk(text=sheet_text, section_kind="tabular", source_uri="x.xlsx")
    assert len(chunks) == 2
    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.chunker_version == version
        assert c.source_uri == "x.xlsx"
