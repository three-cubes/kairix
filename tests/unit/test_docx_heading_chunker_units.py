"""Plugin-specific behaviour of :class:`DocxHeadingChunker`.

The Protocol-level invariants (Chunker shape, F55 version carry-through,
F39 source_uri, empty-input) are proved over real + fake in
``tests/contracts/test_docx_heading_chunker_protocol.py``. These tests
pin what is specific to the docx-heading plugin: its registry name, the
entry-point factory's concrete type, and the heading-path breadcrumb.
"""

from __future__ import annotations

import pytest

from kairix.chunkers.docx_heading import PLUGIN_NAME, DocxHeadingChunker, make_chunker

pytestmark = pytest.mark.unit


def test_make_chunker_returns_docx_heading_chunker() -> None:
    instance = make_chunker()
    assert isinstance(instance, DocxHeadingChunker)
    assert instance.name == PLUGIN_NAME


def test_heading_path_is_surfaced_as_section_metadata() -> None:
    """Sabotage proof: in ``_split_into_sections`` set ``current_path = ""``
    for every heading → the section_path assertion fails. Restored."""
    chunks = DocxHeadingChunker().chunk(
        text="# Chapter 5\n\n## 5.2 Risk\n\nRegister body.", section_kind="text", source_uri="x.docx"
    )
    assert chunks[-1].metadata["section_path"] == "Chapter 5 > 5.2 Risk"
