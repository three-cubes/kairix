"""Code-specific behaviour of :class:`CodeChunker` (ADR-028 Wave G.1).

The Protocol-level invariants (Chunker shape, F55 version carry-through,
F39 source_uri, empty input, tuple return) are proved over real + fake
in ``tests/contracts/test_code_chunker_protocol.py``. These tests pin
what is specific to the code plugin: the ``language`` argument selects
the separator stack (unknown languages fall back without raising), the
language is surfaced in chunk metadata, and the entry-point factory
returns the concrete class with the plugin name.
"""

from __future__ import annotations

import pytest

from kairix.chunkers.code import CodeChunker, make_chunker

pytestmark = pytest.mark.unit


def test_factory_returns_real_class() -> None:
    chunker = make_chunker(language="python")
    assert isinstance(chunker, CodeChunker)
    assert chunker.name == "code"


@pytest.mark.parametrize("language", ["python", "go", "typescript", "unknown_lang"])
def test_unknown_language_falls_back_without_raising(language: str) -> None:
    """Unknown languages get the generic separator stack — never raise."""
    chunker = make_chunker(language=language)
    assert chunker.language == language
    assert chunker.chunk(text="alpha beta\n\ngamma", section_kind="text", source_uri="x")


def test_language_surfaced_in_metadata() -> None:
    """Each Chunk's metadata carries the configured language so downstream
    retrieval can render syntax-aware previews.
    """
    chunker = make_chunker(language="python")
    chunks = chunker.chunk(text="def foo():\n    pass\n", section_kind="text", source_uri="x.py")
    assert chunks
    for chunk in chunks:
        assert chunk.metadata.get("language") == "python"


def test_custom_version_is_threaded_through() -> None:
    """A directly-constructed chunker threads its own ``version`` (F55)."""
    chunker = CodeChunker(language="python", version="code-v9")
    chunks = chunker.chunk(text="class Foo:\n    pass\n", section_kind="text", source_uri="src/foo.py")
    assert chunks
    assert {c.chunker_version for c in chunks} == {"code-v9"}
