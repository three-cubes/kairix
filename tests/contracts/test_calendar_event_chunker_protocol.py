"""Contract tests for the ``calendar_event`` Chunker plugin (F43, F55).

Pins the :class:`~kairix.core.protocols.Chunker` Protocol invariants with
ONE body per invariant, run over BOTH the shipped
:class:`CalendarEventChunker` and the canonical
:class:`tests.fakes.FakeParagraphChunker` (F43 behavioural parity):

* The instance satisfies the :class:`Chunker` Protocol.
* ``version`` is non-empty AND identical to its declaration site (for
  the plugin: the module-level ``version``, on class and instance).
* Every emitted :class:`Chunk` carries ``chunker_version=self.version``
  (F55 invariant) AND ``source_uri`` (F39 invariant).
* Empty / whitespace-only input produces no chunks.

Calendar-envelope semantics (one event → one chunk, recurrence as
metadata, malformed / blank envelopes dropped, alternative field names)
live in ``tests/unit/test_calendar_event_chunker_units.py``.

Sabotage-proofs (mutate prod → confirm fail → restore):
* Delete ``version: str = version`` from the class → asserts in
  ``test_chunker_declares_version`` fail for the ``real`` case.
* Drop ``chunker_version=self.version`` from ``_build_chunk`` →
  asserts in ``test_emitted_chunks_carry_chunker_version`` fail.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from kairix.chunkers.calendar_event import CalendarEventChunker
from kairix.chunkers.calendar_event import version as cal_version
from kairix.core.protocols import Chunk, Chunker
from tests.fakes import FakeParagraphChunker

pytestmark = [pytest.mark.contract]

_EVENT = {
    "subject": "weekly sync",
    "description": "agenda outline",
    "start": "2026-05-30T10:00:00+00:00",
    "end": "2026-05-30T10:30:00+00:00",
    "attendees": ["agent-alpha@example.com", "agent-beta@example.com"],
    "location": "room-alpha",
    "calendar_id": "cal-alpha",
}

# (name, factory, declared version, representative non-empty input)
_IMPLEMENTATIONS: list[tuple[str, Callable[[], Chunker], str, str]] = [
    ("real", CalendarEventChunker, cal_version, json.dumps(_EVENT)),
    ("fake", FakeParagraphChunker, FakeParagraphChunker.version, "weekly sync\n\nagenda outline"),
]
_IDS = [impl[0] for impl in _IMPLEMENTATIONS]


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_chunker_satisfies_protocol(name: str, factory: Callable[[], Chunker], declared: str, sample: str) -> None:
    """The instance is recognised as a runtime :class:`Chunker`."""
    assert isinstance(factory(), Chunker), name


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_chunker_declares_version(name: str, factory: Callable[[], Chunker], declared: str, sample: str) -> None:
    """F55: declared + class-level + instance version is non-empty and consistent."""
    assert isinstance(declared, str)
    assert declared.strip() != "", name
    assert getattr(factory, "version", None) == declared, name
    assert factory().version == declared, name


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_empty_input_yields_no_chunks(name: str, factory: Callable[[], Chunker], declared: str, sample: str) -> None:
    chunker = factory()
    assert chunker.chunk(text="", section_kind="text", source_uri="cal://event/1") == (), name
    assert chunker.chunk(text="   \n  ", section_kind="text", source_uri="cal://event/1") == (), name


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_emitted_chunks_carry_chunker_version(
    name: str, factory: Callable[[], Chunker], declared: str, sample: str
) -> None:
    """F55: every Chunk threads ``chunker_version=self.version``."""
    chunker = factory()
    chunks = chunker.chunk(text=sample, section_kind="text", source_uri="cal://event/1")
    assert chunks, name
    for chunk in chunks:
        assert isinstance(chunk, Chunk)
        assert chunk.chunker_version == chunker.version, name


@pytest.mark.parametrize("name,factory,declared,sample", _IMPLEMENTATIONS, ids=_IDS)
def test_emitted_chunks_carry_source_uri_per_f39(
    name: str, factory: Callable[[], Chunker], declared: str, sample: str
) -> None:
    chunker = factory()
    chunks = chunker.chunk(text=sample, section_kind="text", source_uri="cal://event/XYZ")
    assert chunks, name
    for chunk in chunks:
        assert chunk.source_uri == "cal://event/XYZ", name
