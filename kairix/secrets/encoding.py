"""Newline-safe value encoding for the line-based operator secrets bundle.

The bundle file (``kairix.env``) is one ``KEY=VALUE`` pair per line, so a
multi-line value — a GitHub App PEM private key is the canonical case —
cannot be stored verbatim without corrupting the file for every other
parser. This module owns the symmetric encode/decode pair:

* :func:`encode_bundle_value` runs at the single bundle WRITE site
  (:func:`kairix.secrets.store.set_secret`). Single-line values pass
  through verbatim. Values containing a newline (or CR) are escaped
  (``\\`` → ``\\\\``, CR → ``\\r``, LF → ``\\n``) and wrapped in double
  quotes — the dotenv-style quoted form, so the stored line stays
  greppable (``grep 'BEGIN RSA'`` still hits; base64 was
  rejected for exactly that reason).
* :func:`decode_bundle_value` runs at the bundle PARSE sites
  (:func:`kairix.secrets._legacy.load_secrets` /
  :func:`kairix.secrets._legacy.load_secrets_file`). Only values that
  carry the writer's signature — fully double-quoted AND containing at
  least one ``\\n`` / ``\\r`` escape — are decoded. Everything else
  (hand-written lines, vault-agent sidecar output, values that merely
  contain quote characters or backslashes) passes through byte-for-byte
  untouched, so pre-existing operator bundles never change meaning.

Decoding at the parse layer (rather than in ``SecretsLoader.get`` or in
each consumer) means the decoded value lands in ``os.environ`` during
bundle hydration and every downstream resolver — ``SecretsLoader``, the
legacy ``get_secret`` chain, direct env reads — sees the true multi-line
value with zero consumer changes. Operator-set env vars and KV-mount
files never pass through this module, so they are never rewritten.

F15: neither function logs, prints, or embeds the value in an error.
"""

from __future__ import annotations

import re

# One escape token per encoded byte; decode walks left-to-right so an
# escaped backslash followed by a literal "n" (``\\\\n``) never collapses
# into a newline.
_DECODE_PATTERN = re.compile(r"\\(.)")
_UNESCAPE = {"n": "\n", "r": "\r", "\\": "\\"}

# The writer only quotes when a newline/CR is present, so every encoded
# value carries at least one of these tokens — the decode signature.
_ENCODED_SIGNATURE = ("\\n", "\\r")


def encode_bundle_value(value: str) -> str:
    """Return the single-line bundle representation of ``value``.

    Values without newlines pass through verbatim (the overwhelmingly
    common case — API keys, tokens, endpoints). Multi-line values are
    escaped and double-quoted so the bundle file stays one
    ``KEY=VALUE`` pair per line.

    A single-line value that already LOOKS encoded — fully double-quoted
    and carrying a literal ``\\n`` / ``\\r`` sequence — is escaped too:
    passed through verbatim, the decoder would mistake it for its own
    encoding and turn the literal backslash-n into a real newline (an
    F87 escape-lookalike round-trip loss).
    """
    if "\n" not in value and "\r" not in value and not _is_encoded(value):
        return value
    escaped = value.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
    return f'"{escaped}"'


def split_bundle_lines(text: str) -> list[str]:
    """Split bundle file text into ``KEY=VALUE`` lines on LF only.

    The bundle's record separator is ``\\n`` (CRLF tolerated — a trailing
    ``\\r`` is dropped). :meth:`str.splitlines` is deliberately NOT used:
    it also breaks on Unicode line/paragraph separators (U+2028, U+2029,
    U+0085, form feed, ...), which :func:`encode_bundle_value` leaves
    verbatim inside a single-line value — so a token carrying one would
    be torn across two "lines" on read (an F87 round-trip loss).
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _is_encoded(raw: str) -> bool:
    """True only for values carrying the writer's quoted-escape signature."""
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return False
    return any(token in raw for token in _ENCODED_SIGNATURE)


def decode_bundle_value(raw: str) -> str:
    """Invert :func:`encode_bundle_value`; pass every other value through.

    The guard is deliberately narrow: a value is decoded only when it is
    fully double-quoted AND contains a ``\\n`` / ``\\r`` escape — the
    exact shape the writer emits. A hand-provisioned value that merely
    starts and ends with quotes (or contains backslashes, e.g. a Windows
    path) is returned untouched.
    """
    if not _is_encoded(raw):
        return raw
    inner = raw[1:-1]
    return _DECODE_PATTERN.sub(lambda match: _UNESCAPE.get(match.group(1), "\\" + match.group(1)), inner)


__all__ = ["decode_bundle_value", "encode_bundle_value", "split_bundle_lines"]
