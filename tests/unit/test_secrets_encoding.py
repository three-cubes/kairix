"""Unit coverage for the newline-safe bundle value encoding.

Pins the symmetric contract between
:func:`kairix.secrets.encoding.encode_bundle_value` (the single bundle
write site uses it) and :func:`decode_bundle_value` (the bundle parse
sites use it): multi-line values round-trip byte-identically, and every
value NOT written by the encoder — hand-provisioned lines, quoted
passwords, Windows-path-style backslashes — passes through untouched.

Sabotage-proof (executed): swapped decode's escape walk to sequential
``str.replace`` calls — ``test_escaped_backslash_before_n_round_trips``
failed (``\\\\n`` collapsed into a newline). Restored.

# F87-corpus: secrets_set_load
This module is part of the secrets persist/load pair's adversarial
round-trip corpus (F87). ``test_adversarial_material_round_trips``
sweeps the four material classes the GitHub-PEM happy path skipped —
multi-line, unicode (emoji + CJK), large (>= 64 KiB), and
backslash-escape-lookalike — across encode/decode, the codec underneath
``set_secret`` / ``load_secrets_file``.
"""

from __future__ import annotations

import pytest

from kairix.secrets.encoding import decode_bundle_value, encode_bundle_value, split_bundle_lines

pytestmark = pytest.mark.unit

# Fake fixture key material + the grep marker it carries — NOT real keys.
_PEM_MARKER = "BEGIN RSA PRIVATE KEY"  # pragma: allowlist secret — marker text only
_FAKE_PEM = f"-----{_PEM_MARKER}-----\nFAKE\n-----END RSA PRIVATE KEY-----\n"


@pytest.mark.parametrize(
    "value",
    [
        "plain-token-123",
        "value with spaces",
        'quoted-"inside"-value',
        "trailing-backslash\\",
        "C:\\new\\path\\to\\key",  # backslash-n that must NOT become a newline
        '"fully-quoted-but-not-encoded"',
        "",
    ],
)
def test_single_line_values_pass_through_both_ways(value: str) -> None:
    """No newline → encode is identity, and decode never rewrites it."""
    assert encode_bundle_value(value) == value
    assert decode_bundle_value(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "line-one\nline-two",
        _FAKE_PEM,
        "carriage\rreturn",
        "mixed\r\nnewlines\n",
        "backslash\\then\nnewline",
        "escape-lookalike\\n-literal\nplus-real",
        'quotes "inside" and\nnewline',
        "\n",
    ],
)
def test_multi_line_values_round_trip_byte_identical(value: str) -> None:
    encoded = encode_bundle_value(value)
    assert "\n" not in encoded and "\r" not in encoded, "encoded form must be single-line"
    assert decode_bundle_value(encoded) == value


def test_encoded_form_is_quoted_and_greppable() -> None:
    encoded = encode_bundle_value(_FAKE_PEM.rstrip("\n"))
    assert encoded.startswith('"') and encoded.endswith('"')
    # base64 was rejected for exactly this: the bundle stays greppable.
    assert _PEM_MARKER in encoded


def test_escaped_backslash_before_n_round_trips() -> None:
    """``\\`` followed by literal ``n`` must survive — the decode walk is
    left-to-right, not sequential replace."""
    value = "ends-with\\n-literal\nand-newline"
    assert decode_bundle_value(encode_bundle_value(value)) == value


def test_decode_leaves_quoted_value_without_escape_signature_alone() -> None:
    """A hand-provisioned quoted value (no ``\\n``/``\\r``) is not our encoding."""
    raw = '"operator-password-with-quotes"'
    assert decode_bundle_value(raw) == raw


def test_decode_leaves_unquoted_value_with_escape_lookalike_alone() -> None:
    """An unquoted value containing literal ``\\n`` text is not decoded."""
    raw = "C:\\new\\notes"
    assert decode_bundle_value(raw) == raw


def test_decode_preserves_unknown_escape_sequences() -> None:
    """Inside a decoded value, escapes the encoder never emits pass through."""
    assert decode_bundle_value('"keep\\q-and\\nnewline"') == "keep\\q-and\nnewline"


# --- F87 adversarial round-trip corpus -------------------------------------
# Four material classes the single-line happy path never exercises — the
# GitHub-PEM consent failure shipped because only single-line tokens were
# ever round-tripped. Each value carries an embedded newline so the codec
# actually engages (encode is identity for single-line input).
_MULTI_LINE = "line-one\nline-two\nline-three"  # multi-line
_UNICODE = "secret-🔑-世界-値-한글\nwith-newline"  # unicode: emoji + CJK + Hangul
_LARGE = ("X" * (64 * 1024)) + "\nlarge-tail"  # large: >= 64 KiB
_ESCAPE_LOOKALIKE = "C:\\new\\path\\to\\key\nand-a\\n-literal"  # escape-lookalike


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("multi-line", _MULTI_LINE),
        ("unicode", _UNICODE),
        ("large", _LARGE),
        ("escape-lookalike", _ESCAPE_LOOKALIKE),
    ],
    ids=["multi-line", "unicode", "large", "escape-lookalike"],
)
def test_adversarial_material_round_trips(label: str, value: str) -> None:
    """Every adversarial class survives encode → decode byte-identical."""
    encoded = encode_bundle_value(value)
    assert "\n" not in encoded and "\r" not in encoded, f"{label}: encoded form must stay single-line"
    assert decode_bundle_value(encoded) == value, f"{label}: round-trip lost bytes"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # No trailing newline: the last record is real data and must survive.
        ("ALPHA=1\nBETA=2", ["ALPHA=1", "BETA=2"]),
        ("ALPHA=1", ["ALPHA=1"]),
        # Trailing newline: only the empty tail produced by the final LF is dropped.
        ("ALPHA=1\nBETA=2\n", ["ALPHA=1", "BETA=2"]),
        # CRLF tolerated: the trailing CR of each record is stripped.
        ("ALPHA=1\r\nBETA=2\r\n", ["ALPHA=1", "BETA=2"]),
        ("", []),
    ],
)
def test_split_bundle_lines_drops_only_the_empty_tail(text: str, expected: list[str]) -> None:
    """Only the empty string left after a final LF is discarded — a bundle
    whose last ``KEY=VALUE`` record has no trailing newline keeps it.

    Sabotage-proof (executed): flipped the tail guard to ``lines[-1] != ""``
    and, separately, ``lines or lines[-1] == ""`` — both drop ``BETA=2`` from
    the unterminated bundle and this test fails. Restored.
    """
    assert split_bundle_lines(text) == expected
