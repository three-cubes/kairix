"""Thread-aware email chunker — one chunk per message, quoted reply stripped.

The :class:`EmailThreadChunker` expects the input ``text`` to be a
serialised email thread where messages are separated by a recognisable
delimiter. The default delimiter is the literal ``\\n---MESSAGE---\\n``
sentinel that the M365 / Gmail email-headers extractors emit when they
serialise a thread for the chunker. Other extractors emitting
``rfc822``-style ``From: ...`` separators are also handled.

The algorithm in plain English:

  1. Split the thread on the message-boundary delimiter.
  2. For each message, parse the header block (``Subject:``,
     ``From:``, ``To:``, ``Date:``, ``Message-ID:``) into a metadata
     dictionary, then strip the body's quoted-reply lines (everything
     after the first run of ``>``-prefixed lines or after a typical
     ``On <date>, <person> wrote:`` separator).
  3. Format the visible header block as a metadata prefix on the
     chunk text so the embedding sees the context.
  4. If a single message exceeds the per-message cap, sub-split on
     paragraph boundaries (no overlap — the message boundary is the
     unit; bleed makes no sense).

This matches the consensus pattern (RAG-Mail, Luna, Colligo M365)
cited in ADR-028 without dragging an MTA-parser dependency along.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from kairix.core.protocols import Chunk

#: Canonical plugin name surfaced by the entry-point registry.
PLUGIN_NAME = "email_thread"

#: Per-message cap in characters (1024 tokens * 4 chars/token).
#: ADR-028 §"Email". Sub-split only when a single message exceeds.
_MESSAGE_CAP_CHARS = 4096

#: Canonical message delimiter the email extractors emit. Other
#: shapes (``From: ...`` rfc822, ``On <date>, ... wrote:``) get
#: handled by the regex fallback in :func:`_split_thread`.
_MESSAGE_SENTINEL = "\n---MESSAGE---\n"

#: Header fields surfaced into metadata. Anything else is dropped.
#: Names are normalised to lowercase before storage.
_HEADERS_OF_INTEREST: frozenset[str] = frozenset({"subject", "from", "to", "date", "message-id", "thread-id"})

#: Quoted-reply markers — lines starting with ``>`` (one or more)
#: optionally preceded by whitespace. Once we see a run of these,
#: every subsequent line is treated as quoted.
_QUOTED_LINE_RE = re.compile(r"^\s*>+")

#: "On <date>, <person> wrote:" reply boundary that Gmail / Outlook
#: synthesise above quoted-reply blocks. Matching forms a hard cut —
#: everything below is treated as the previous message and dropped
#: from the current chunk (it'll re-surface as its own chunk when
#: the parent message is emitted). Single ``\s`` delimiters around
#: the ``.+`` keep the pattern linear-time (no two adjacent quantifiers
#: share a character class); on a single line it accepts exactly the
#: same strings as the original ``^On\s+.+\s+wrote:\s*$``.
_REPLY_PREAMBLE_RE = re.compile(
    r"^On\s.+\swrote:\s*$",
    re.IGNORECASE,
)

#: Rfc822-style ``From: ...`` line that prefixes each message in a
#: full-text export. Used as a fallback delimiter when the canonical
#: sentinel is absent.
_RFC822_FROM_RE = re.compile(r"^From:\s+", re.MULTILINE)

#: Metadata keys we always want to surface to retrieval. Keeping the
#: list centralised lets downstream code rely on a stable shape.
_METADATA_HEADERS_PREFIX = "header_"


@dataclass(frozen=True)
class _ParsedMessage:
    """One message lifted out of the serialised thread."""

    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


class EmailThreadChunker:
    """Thread-aware :class:`Chunker` for email threads."""

    def __init__(self, *, version: str) -> None:
        """Bind the F55 version and the plugin name."""
        self.name: str = PLUGIN_NAME
        self.version: str = version

    def chunk(self, *, text: str, section_kind: str, source_uri: str) -> tuple[Chunk, ...]:
        """Split ``text`` into one chunk per message, stripping quoted replies.

        ``section_kind`` is accepted for Protocol conformance and held
        live for F19; email chunking is thread-driven irrespective of
        the section discriminator.
        """
        if section_kind:
            del section_kind
        if not text.strip():
            return ()
        raw_messages = _split_thread(text)
        chunks: list[Chunk] = []
        for raw in raw_messages:
            parsed = _parse_message(raw)
            visible_body = _strip_quoted_reply(parsed.body)
            if not visible_body.strip() and not parsed.headers:
                continue
            chunks.extend(_emit_message_chunks(parsed, visible_body, source_uri, self.version))
        return tuple(chunks)


def _split_thread(text: str) -> tuple[str, ...]:
    """Split a serialised thread into one string per message.

    Prefers the canonical ``---MESSAGE---`` sentinel; falls back to
    splitting on ``From: `` lines for rfc822-style exports.
    """
    if _MESSAGE_SENTINEL.strip() in text:
        sentinel_parts = text.split(_MESSAGE_SENTINEL)
        return tuple(p for p in sentinel_parts if p.strip())
    # Fallback: rfc822 ``From: `` boundaries. Keep the ``From:`` prefix
    # on each split so the header parse below picks it up.
    matches = list(_RFC822_FROM_RE.finditer(text))
    if len(matches) > 1:
        rfc_parts: list[str] = []
        for index, match in enumerate(matches):
            start = match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            rfc_parts.append(text[start:end])
        return tuple(p for p in rfc_parts if p.strip())
    # Single message — return the whole text as one piece.
    return (text,)


def _parse_message(raw: str) -> _ParsedMessage:
    """Lift the header block out of one message into a dict + body string.

    Header block ends at the first blank line. Header names are
    lowercased; only the keys in :data:`_HEADERS_OF_INTEREST` are
    retained. The remainder is the body.
    """
    lines = raw.splitlines()
    headers: dict[str, str] = {}
    body_start = 0
    for index, line in enumerate(lines):
        if not line.strip():
            body_start = index + 1
            break
        match = re.match(r"^([A-Za-z0-9_-]+):(.*)$", line)
        if match is None:
            body_start = index
            break
        name = match.group(1).strip().lower()
        value = match.group(2).strip()
        if name in _HEADERS_OF_INTEREST and value:
            headers[name] = value
    else:
        # No blank line — everything was headers (probably empty body).
        body_start = len(lines)
    body = "\n".join(lines[body_start:]).strip()
    return _ParsedMessage(headers=headers, body=body)


def _strip_quoted_reply(body: str) -> str:
    """Drop quoted-reply chains from the message body.

    Strategy:
      1. If a ``On <date>, ... wrote:`` line appears, hard-cut at the
         first occurrence — everything below is the prior message and
         will surface on its own chunk.
      2. Otherwise, drop the trailing run of ``>``-prefixed lines.
    """
    lines = body.splitlines()
    cut_at: int | None = None
    for index, line in enumerate(lines):
        if _REPLY_PREAMBLE_RE.match(line.strip()):
            cut_at = index
            break
    if cut_at is not None:
        lines = lines[:cut_at]
    # Drop trailing quoted block.
    visible: list[str] = []
    for line in lines:
        if _QUOTED_LINE_RE.match(line):
            continue
        visible.append(line)
    return "\n".join(visible).strip()


def _format_metadata_prefix(headers: dict[str, str]) -> str:
    """Render the header block as a human-readable prefix for the chunk text.

    Empty header dict returns an empty string. The output is a single
    block ending with a blank line so the body underneath reads
    naturally.
    """
    if not headers:
        return ""
    lines: list[str] = []
    for key in ("subject", "from", "to", "date"):
        value = headers.get(key)
        if value:
            lines.append(f"{key.title()}: {value}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


def _emit_message_chunks(
    parsed: _ParsedMessage,
    visible_body: str,
    source_uri: str,
    chunker_version: str,
) -> tuple[Chunk, ...]:
    """Emit one or more Chunks for a single message, honouring the cap."""
    prefix = _format_metadata_prefix(parsed.headers)
    full = f"{prefix}{visible_body}".strip()
    if not full:
        return ()
    metadata = _build_metadata_dict(parsed.headers)
    if len(full) <= _MESSAGE_CAP_CHARS:
        return (_build_email_chunk(full, source_uri, chunker_version, metadata),)
    # Oversize message — sub-split on paragraph boundaries. No
    # overlap; message boundary is the unit.
    windows = _split_oversize_message(full)
    return tuple(_build_email_chunk(w, source_uri, chunker_version, metadata) for w in windows)


def _split_oversize_message(text: str) -> tuple[str, ...]:
    """Greedy-pack paragraphs of an oversize message into cap-sized windows."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return ()
    windows: list[str] = []
    current = ""
    for para in paragraphs:
        if not current:
            current = para
            continue
        joined = current + "\n\n" + para
        if len(joined) <= _MESSAGE_CAP_CHARS:
            current = joined
        else:
            windows.append(current)
            current = para
    if current:
        windows.append(current)
    return tuple(windows)


def _build_metadata_dict(headers: dict[str, str]) -> dict[str, str]:
    """Surface each header into ``Chunk.metadata`` under a stable key."""
    metadata: dict[str, str] = {}
    for key, value in headers.items():
        metadata[f"{_METADATA_HEADERS_PREFIX}{key}"] = value
    return metadata


def _build_email_chunk(
    text: str,
    source_uri: str,
    chunker_version: str,
    metadata: dict[str, str],
) -> Chunk:
    """Construct one :class:`Chunk` for a single email message."""
    return Chunk(
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        source_name="",
        source_uri=source_uri,
        source_modified_at="",
        source_page=None,
        sensitivity="internal",
        chunker_version=chunker_version,
        metadata=metadata,
    )
