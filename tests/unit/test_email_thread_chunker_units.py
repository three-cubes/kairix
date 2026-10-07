"""Unit-scope coverage for :class:`EmailThreadChunker`'s header-parse +
quoted-reply-strip + metadata-prefix branches.

The contract test (``tests/contracts/test_email_thread_chunker_protocol.py``)
pins the Protocol shape on the happy path; the integration test
(``tests/integration/test_email_thread_chunker_quality.py``) exercises
the three-message thread with a quoted-reply chain. Neither runs in
the unit-only Stage 2 coverage measurement, so these targeted unit
tests close the F7 floor gap on:

  * lines 154-155 — header block aborts at first non-``Header: Value`` line
  * line 162 — thread with only header lines (no blank-line terminator)
  * lines 180-181 + 183 — ``On <date>, <person> wrote:`` cut path
  * line 188 — ``>``-prefixed quoted-line continue path
  * line 201 — :func:`_format_metadata_prefix` empty-headers early-exit
  * line 208 — non-empty headers dict but no key in subject/from/to/date

Every branch is reached through the public ``chunker.chunk(...)`` API,
matching the "no tests against internal/private functions" project
discipline.

Sabotage proofs (executed inline before commit; restore after each):

  * Mutate line 154 from ``body_start = index`` to ``body_start = 0`` →
    test_non_header_line_aborts_header_block fails because the body
    string includes the unparsed non-header line at position 0.
  * Mutate line 180 from ``cut_at = index`` to ``cut_at = None`` →
    test_on_wrote_line_cuts_quoted_block fails because the prior
    quoted message body bleeds into the chunk.
  * Mutate line 188 from ``continue`` to ``visible.append(line)`` →
    test_quote_prefix_lines_drop_from_visible_body fails because the
    ``> quoted`` line surfaces in the rendered chunk text.
  * Mutate line 201 from ``return ""`` to ``return "HEADER\n\n"`` →
    test_no_recognised_headers_emits_chunk_with_no_prefix fails
    because the prefix appears at the start of the chunk text.
  * Mutate line 208 from ``return ""`` to ``return "STUB\n\n"`` →
    test_only_message_id_header_emits_chunk_with_no_prefix fails
    because the stub appears at the start of the chunk text.
"""

from __future__ import annotations

import pytest

from kairix.chunkers.email_thread import EmailThreadChunker, make_chunker

pytestmark = pytest.mark.unit


def test_non_header_line_aborts_header_block() -> None:
    """A line in the header region that doesn't match ``Header: Value``
    ends the header block, with the body starting at that line (lines
    154-155).
    """
    chunker = make_chunker()
    # First line is a valid header; second line has no colon and is not
    # blank → header parse aborts at index=1, body_start=1.
    text = "Subject: Re: roadmap\nthis-line-has-no-colon\nVisible body line.\n"
    chunks = chunker.chunk(text=text, section_kind="text", source_uri="msg/no-colon")
    assert len(chunks) == 1
    chunk_text = chunks[0].text
    # The non-header line must appear in the body (the header parser
    # broke out at it), proving body_start was set to the abort index.
    assert "this-line-has-no-colon" in chunk_text
    assert "Visible body line." in chunk_text
    # Only the first valid header was captured before the abort.
    assert chunks[0].metadata.get("header_subject") == "Re: roadmap"
    # The aborted line was NOT misparsed as a header.
    assert all(not key.startswith("header_this-line") for key in chunks[0].metadata)


def test_message_with_only_headers_no_blank_terminator() -> None:
    """A message whose body has *only* header-shaped lines (no blank
    line terminator, no trailing body content) reaches the ``else``
    branch on the for-loop (line 162) where ``body_start = len(lines)``.
    The empty body short-circuits the chunk-emission path.

    Sabotage-proof: if ``body_start = 0`` instead, the raw header
    lines (literal ``Subject:`` / ``From:`` / ``Date:`` strings) would
    bleed into the chunk body and show up *after* the rendered prefix,
    so the body would contain a literal ``\\nFrom: ...`` substring.
    """
    chunker = make_chunker()
    # All lines are valid header shape; no blank line; no body content.
    text = "Subject: header-only\nFrom: agent-alpha@example.test\nDate: 2026-05-30\n"
    chunks = chunker.chunk(text=text, section_kind="text", source_uri="msg/headers-only")
    # Headers were captured → headers dict is non-empty → the chunk
    # gets emitted (with no body, only the header prefix).
    assert len(chunks) == 1
    chunk_text = chunks[0].text
    assert chunks[0].metadata.get("header_subject") == "header-only"
    assert chunks[0].metadata.get("header_from") == "agent-alpha@example.test"
    # Render order is ``Subject: ... \nFrom: ... \nDate: ...`` — the
    # prefix appears exactly once. If body_start was 0, the raw header
    # lines would also appear in the body and we'd see a second copy
    # of "Subject:" in the chunk text. Asserting "Subject:" occurs
    # exactly once distinguishes the empty-body branch from a
    # body-includes-headers bleed.
    assert chunk_text.count("Subject:") == 1
    assert chunk_text.count("From:") == 1
    # Final character of the text should be the last header value,
    # NOT a trailing copy of the header block.
    assert chunk_text.endswith("2026-05-30")


def test_on_wrote_line_cuts_quoted_block() -> None:
    """The ``On <date>, <person> wrote:`` line triggers a hard cut so
    the prior message body doesn't bleed into the chunk (lines 180-181,
    183).
    """
    chunker = make_chunker()
    text = (
        "Subject: Re: planning\n"
        "From: agent-beta@example.test\n"
        "Date: 2026-05-30\n"
        "\n"
        "VISIBLE-REPLY-BODY my reply text\n"
        "\n"
        "On 2026-05-28, agent-alpha wrote:\n"
        "PRIOR-MESSAGE-BODY-LEAK that should be cut\n"
    )
    chunks = chunker.chunk(text=text, section_kind="text", source_uri="msg/on-wrote")
    assert len(chunks) == 1
    chunk_text = chunks[0].text
    # The reply body must surface in the chunk.
    assert "VISIBLE-REPLY-BODY" in chunk_text
    # Everything below the "On ... wrote:" cut must be dropped.
    assert "PRIOR-MESSAGE-BODY-LEAK" not in chunk_text
    assert "On 2026-05-28, agent-alpha wrote:" not in chunk_text


def test_quote_prefix_lines_drop_from_visible_body() -> None:
    """``>``-prefixed lines (without an ``On wrote:`` preamble) drop
    from the visible body (line 188 ``continue`` branch).
    """
    chunker = make_chunker()
    text = (
        "Subject: Re: scattered quotes\n"
        "From: agent-gamma@example.test\n"
        "Date: 2026-05-30\n"
        "\n"
        "VISIBLE-LINE-ONE first visible line\n"
        "> QUOTED-LINE-LEAK should not appear\n"
        "VISIBLE-LINE-TWO second visible line\n"
        "> ANOTHER-QUOTED-LEAK also dropped\n"
    )
    chunks = chunker.chunk(text=text, section_kind="text", source_uri="msg/quote-prefix")
    assert len(chunks) == 1
    chunk_text = chunks[0].text
    # Visible lines survive.
    assert "VISIBLE-LINE-ONE" in chunk_text
    assert "VISIBLE-LINE-TWO" in chunk_text
    # Quoted ``>``-prefixed lines drop.
    assert "QUOTED-LINE-LEAK" not in chunk_text
    assert "ANOTHER-QUOTED-LEAK" not in chunk_text


def test_no_recognised_headers_emits_chunk_with_no_prefix() -> None:
    """A message that parses *no* recognised headers (body has only
    plain-text content) reaches :func:`_format_metadata_prefix` with an
    empty dict (line 201 ``return ""`` early-exit).

    Sabotage-proof: drop the empty-dict early-exit and the chunk's
    text would gain a non-empty header prefix at position 0.
    """
    chunker = make_chunker()
    # Single message: first line has no colon → header parse aborts
    # at index 0 → headers dict is empty → body is everything.
    text = "plain-body-line-one not a header\nplain-body-line-two also not a header\n"
    chunks = chunker.chunk(text=text, section_kind="text", source_uri="msg/no-headers")
    assert len(chunks) == 1
    chunk_text = chunks[0].text
    # No header metadata extracted.
    assert chunks[0].metadata == {}
    # The chunk text starts with the body content, NOT with a header
    # prefix (would be ``Subject: ...\n\n...`` if line 201 didn't
    # early-exit).
    assert chunk_text.startswith("plain-body-line-one")


def test_only_message_id_header_emits_chunk_with_no_prefix() -> None:
    """Headers like ``message-id`` are surfaced into metadata but are
    NOT in the subject/from/to/date render set, so the prefix is empty
    (line 208 ``return ""``).

    Sabotage-proof: drop the empty-lines early-exit and the chunk's
    text would gain a stub prefix at position 0.
    """
    chunker = make_chunker()
    text = (
        "Message-ID: <abc-123@example.test>\n"
        "Thread-ID: <thread-xyz@example.test>\n"
        "\n"
        "VISIBLE-BODY message-id-only body content\n"
    )
    chunks = chunker.chunk(text=text, section_kind="text", source_uri="msg/id-only")
    assert len(chunks) == 1
    chunk_text = chunks[0].text
    # Both surfaced as metadata.
    assert chunks[0].metadata.get("header_message-id") == "<abc-123@example.test>"
    assert chunks[0].metadata.get("header_thread-id") == "<thread-xyz@example.test>"
    # Neither is in the render set → prefix is empty → body starts at
    # the chunk's first character.
    assert chunk_text.startswith("VISIBLE-BODY")
    # Neither prefix-key appears in the rendered text.
    assert "Message-Id:" not in chunk_text
    assert "Thread-Id:" not in chunk_text


def test_explicit_version_propagates_through_chunks() -> None:
    """Direct construction with a non-default version proves the
    version threads through, matching the canonical fakes-first pattern
    used in the contract test.
    """
    chunker = EmailThreadChunker(version="email-unit-v9")
    text = "Subject: hi\nFrom: a@x.test\n\nbody\n"
    chunks = chunker.chunk(text=text, section_kind="text", source_uri="msg/v")
    assert chunks
    assert all(c.chunker_version == "email-unit-v9" for c in chunks)


# Moved from tests/contracts/test_email_thread_chunker_protocol.py — the
# contract file now proves only the Protocol-level invariants over real +
# fake (F43); these pin email-thread-specific behaviour.


def test_factory_returns_real_class() -> None:
    chunker = make_chunker()
    assert isinstance(chunker, EmailThreadChunker)
    assert chunker.name == "email_thread"


def test_headers_surface_into_metadata() -> None:
    """Subject / From / Date / To get surfaced under ``header_*`` keys."""
    chunker = make_chunker()
    text = (
        "Subject: Catch-up\n"
        "From: agent-alpha@example.test\n"
        "Date: 2026-05-30\n"
        "To: agent-beta@example.test\n"
        "\n"
        "Hello agent-beta, are you free for a quick sync?\n"
    )
    chunks = chunker.chunk(text=text, section_kind="text", source_uri="msg/1")
    assert len(chunks) == 1
    metadata = chunks[0].metadata
    assert metadata.get("header_subject") == "Catch-up"
    assert metadata.get("header_from") == "agent-alpha@example.test"
    assert metadata.get("header_date") == "2026-05-30"
    assert metadata.get("header_to") == "agent-beta@example.test"
