"""Plugin-specific behaviour of the ``calendar_event`` Chunker plugin.

The Protocol-level invariants (Chunker shape, F55 version, F39
source_uri, empty input) are proved over real + fake in
``tests/contracts/test_calendar_event_chunker_protocol.py``. These tests
pin what is specific to calendar envelopes (ADR-028 §"Calendar event"):

* One event → exactly one chunk; recurrence is metadata, not text-
  expansion.
* Both-blank-subject-and-description envelopes, malformed JSON, empty
  lists and JSON scalars produce no chunks rather than near-noise.
* Alternative field names and defensive parsing of dates / attendees.

Sabotage-proof: change ``one event → one chunk`` to ``one chunk per
occurrence`` → ``test_recurring_event_emits_one_chunk_only`` fails.
"""

from __future__ import annotations

import json

import pytest

from kairix.chunkers.calendar_event import CalendarEventChunker
from kairix.core.protocols import Chunk

pytestmark = pytest.mark.unit


def _event(**overrides: object) -> dict[str, object]:
    """Build a baseline calendar-event dict for fixture seeding."""
    base: dict[str, object] = {
        "subject": "weekly sync",
        "description": "agenda outline",
        "start": "2026-05-30T10:00:00+00:00",
        "end": "2026-05-30T10:30:00+00:00",
        "attendees": ["agent-alpha@example.com", "agent-beta@example.com"],
        "location": "room-alpha",
        "calendar_id": "cal-alpha",
    }
    base.update(overrides)
    return base


def test_one_event_yields_exactly_one_chunk() -> None:
    chunker = CalendarEventChunker()
    envelope = json.dumps(_event())
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="cal://event/1")
    assert len(chunks) == 1
    assert isinstance(chunks[0], Chunk)


def test_recurring_event_emits_one_chunk_only() -> None:
    """ADR-028 invariant: recurrence is metadata, NOT text-expansion."""
    chunker = CalendarEventChunker()
    envelope = json.dumps(_event(recurrence="RRULE:FREQ=WEEKLY;COUNT=10"))
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="cal://event/recurrer")
    assert len(chunks) == 1, "Recurring events MUST emit one chunk; recurrence is metadata only."


def test_both_blank_subject_and_description_yields_no_chunks() -> None:
    """A pure-metadata event has no embedding signal — drop it."""
    chunker = CalendarEventChunker()
    envelope = json.dumps(_event(subject="", description="", attendees=[]))
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="cal://event/empty")
    assert chunks == ()


def test_malformed_json_yields_no_chunks() -> None:
    """Bad input is dropped (chunker is downstream of connector/extractor)."""
    chunker = CalendarEventChunker()
    assert chunker.chunk(text="not json", section_kind="text", source_uri="cal://event/x") == ()


def test_list_envelope_with_single_event_is_accepted() -> None:
    """A list-wrapped single event decodes the same as a bare dict."""
    chunker = CalendarEventChunker()
    envelope = json.dumps([_event()])
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="cal://event/list-wrap")
    assert len(chunks) == 1


def test_list_envelope_empty_yields_no_chunks() -> None:
    chunker = CalendarEventChunker()
    assert chunker.chunk(text="[]", section_kind="text", source_uri="cal://event/empty-list") == ()


def test_scalar_json_yields_no_chunks() -> None:
    """A JSON scalar (number, bool, string) isn't an event envelope."""
    chunker = CalendarEventChunker()
    assert chunker.chunk(text="42", section_kind="text", source_uri="cal://event/x") == ()


def test_alternative_field_names_are_honoured() -> None:
    """``title`` / ``body`` / ``rrule`` / ``calendarId`` fallbacks are honoured."""
    chunker = CalendarEventChunker()
    envelope = json.dumps(
        {
            "title": "alt-naming event",
            "body": "alt-body content",
            "start_iso": "2026-05-30T09:00:00Z",
            "end_iso": "2026-05-30T09:15:00Z",
            "rrule": "RRULE:FREQ=MONTHLY",
            "calendarId": "alt-cal",
        }
    )
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="cal://event/alt")
    assert len(chunks) == 1
    assert "alt-naming event" in chunks[0].text
    assert "alt-body content" in chunks[0].text
    assert chunks[0].metadata["recurrence_rule"] == "RRULE:FREQ=MONTHLY"
    assert chunks[0].metadata["calendar_id"] == "alt-cal"


def test_unparseable_iso_dates_drop_duration() -> None:
    """``start``/``end`` that aren't valid ISO-8601 → ``duration_minutes`` empty."""
    chunker = CalendarEventChunker()
    envelope = json.dumps(_event(start="not-a-date", end="also-not-a-date"))
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="cal://event/bad")
    assert chunks[0].metadata["duration_minutes"] == ""


def test_description_with_no_urls_emits_empty_linked_docs() -> None:
    chunker = CalendarEventChunker()
    envelope = json.dumps(_event(description="no links here at all"))
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="cal://event/no-links")
    assert chunks[0].metadata["linked_docs"] == ""


def test_attendees_non_sequence_is_silently_ignored() -> None:
    """An ``attendees`` value that isn't a sequence (e.g. a single string) → empty."""
    chunker = CalendarEventChunker()
    # The connector contract is a list of strings; defensively a single string
    # is treated as no attendees rather than splitting into characters.
    envelope = json.dumps(_event(attendees="not-a-list"))
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="cal://event/odd")
    assert chunks[0].metadata["attendees"] == ""
