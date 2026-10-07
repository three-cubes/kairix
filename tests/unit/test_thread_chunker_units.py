"""Unit tests for :class:`ThreadChunker`'s own Slack-envelope parsing rules.

Moved from ``tests/contracts/test_thread_chunker_protocol.py`` (F43
paydown): these pin ThreadChunker-specific behaviour (window guard, JSON
scalar / mixed-list envelopes, non-numeric ``ts`` fallback, empty-text
skip) rather than the :class:`Chunker` Protocol contract, which stays
parametrized over the real chunker AND the canonical fake in the
contract file.
"""

from __future__ import annotations

import json

import pytest

from kairix.chunkers.thread import ThreadChunker

pytestmark = pytest.mark.unit


def _msg(
    *,
    ts: str,
    user: str,
    text: str,
    thread_ts: str | None = None,
    channel: str = "ch-alpha",
) -> dict[str, object]:
    """Build a slack-shaped message dict for fixture seeding."""
    return {
        "ts": ts,
        "user": user,
        "text": text,
        "thread_ts": thread_ts,
        "channel": channel,
    }


def test_constructor_rejects_non_positive_window() -> None:
    """A non-positive window is rejected at construction.

    Sabotage proof: replace the ``time_window_minutes <= 0`` guard's
    ``raise ValueError`` with ``pass``; the test fails. Restored.
    """
    with pytest.raises(ValueError, match="time_window_minutes"):
        ThreadChunker(time_window_minutes=0)
    with pytest.raises(ValueError, match="time_window_minutes"):
        ThreadChunker(time_window_minutes=-1)


def test_json_payload_neither_dict_nor_list_yields_no_chunks() -> None:
    """A JSON scalar / number / bool decodes to neither dict nor list — drop it.

    Sabotage proof: in ``_parse_messages`` make the final fallthrough
    ``return [{"text": stripped}]``; the scalar yields a chunk and the
    test fails. Restored.
    """
    chunker = ThreadChunker()
    # A JSON number is valid JSON but isn't a message envelope.
    assert chunker.chunk(text="42", section_kind="text", source_uri="slack://x") == ()
    # A JSON string ditto.
    assert chunker.chunk(text='"hi"', section_kind="text", source_uri="slack://x") == ()


def test_list_payload_filters_non_dict_entries() -> None:
    """A list with mixed dict / non-dict entries keeps only the dicts.

    Sabotage proof: in ``_parse_messages`` return ``payload`` unfiltered
    for lists; the non-dict entries crash grouping and the test fails.
    Restored.
    """
    chunker = ThreadChunker()
    envelope = json.dumps(
        [
            _msg(ts="1.0", user="agent-alpha", text="kept"),
            "not a dict",
            42,
            _msg(ts="2.0", user="agent-beta", text="also-kept"),
        ]
    )
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="slack://x")
    # Two messages within 5 minutes of each other → one window-chunk.
    assert len(chunks) == 1
    assert "kept" in chunks[0].text
    assert "also-kept" in chunks[0].text


def test_ts_with_non_numeric_value_degrades_to_zero() -> None:
    """A non-numeric ``ts`` value falls back to 0.0 without crashing.

    Sabotage proof: in ``_ts_float`` drop the ``(TypeError, ValueError)``
    fallback so ``float(ts)`` raises; the test fails. Restored.
    """
    chunker = ThreadChunker()
    envelope = json.dumps(_msg(ts="not-a-number", user="agent-alpha", text="hi"))
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="slack://x")
    assert len(chunks) == 1
    # time_range = "0.0..0.0" for the bad-ts single-message case.
    assert chunks[0].metadata["time_range"] == "0.0..0.0"


def test_empty_message_text_skips_silently() -> None:
    """A message with empty text contributes no text but still counts as a member.

    Sabotage proof: in ``_join_text`` join every part (drop the ``if p``
    filter); the chunk text gains a leading newline and the test fails.
    Restored.
    """
    chunker = ThreadChunker()
    envelope = json.dumps(
        [
            _msg(ts="1.0", user="agent-alpha", text=""),
            _msg(ts="2.0", user="agent-beta", text="real content"),
        ]
    )
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="slack://x")
    assert len(chunks) == 1
    # Only "real content" — the empty-text first message is skipped in join.
    assert chunks[0].text == "real content"
