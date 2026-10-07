"""Adversarial round-trip corpus for ``FileTokenStore`` → ``load_secrets_file``.

# F87-corpus: connect_file_store

``kairix connect`` persists captured tokens through
:class:`kairix.connect.store.file_store.FileTokenStore` (which delegates
every leaf to ``set_secret``); the next boot reads them back through
:func:`kairix.secrets.load_secrets_file`. The happy-path tests in
``test_connect_store_file.py`` only ever round-trip single-line ASCII
tokens — the exact gap that shipped the GitHub-PEM consent failure. This
module drives the four adversarial material classes F87 requires across
the REAL write → read boundary:

* multi-line — embedded ``\\n`` / ``\\r\\n`` / bare ``\\r`` (the PEM class);
* unicode — emoji AND CJK code points (UTF-8 byte-width edges), plus
  the Unicode line/paragraph separators ``str.splitlines`` breaks on;
* large — a value >= 64 KiB (buffer / line-length ceilings);
* escape-lookalike — backslash sequences that must survive verbatim,
  including a fully double-quoted value carrying a literal backslash-n
  (the shape the bundle decoder treats as its own encoding).

Every value is asserted byte-identical after the round trip, and every
bundle line must stay a single parseable ``KEY=VALUE`` pair.

Sabotage-proof (executed): reverted the quoted-lookalike guard in
``kairix.secrets.encoding.encode_bundle_value`` (single-line values pass
through verbatim again) — the ``quoted-literal-backslash-n`` / ``-r``
cases failed with the literal ``\\n`` / ``\\r`` decoded into a real line
break and the quotes stripped. Restored. Second sabotage: reverted
``load_secrets_file`` to ``str.splitlines()`` — the
``unicode-line-separators`` case failed (the token torn at U+2028).
Restored. Before this corpus landed, all three cases were real
round-trip losses in production; both fixes live in
``kairix/secrets/encoding.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.connect.protocols import CapturedTokens, ClientCredentials
from kairix.connect.store.file_store import FileTokenStore
from kairix.secrets import load_secrets_file
from kairix.secrets.naming import canonical_env_var

pytestmark = pytest.mark.unit

_LARGE = "K" * (64 * 1024)  # >= 64 KiB single value

_CORPUS: dict[str, str] = {
    # multi-line
    "pem-lf": "-----BEGIN FAKE KEY-----\nFAKE-LINE-agent-alpha\n-----END FAKE KEY-----\n",
    "crlf": "line-one\r\nline-two\r\n",
    "bare-cr": "carriage\rreturn",
    # unicode (emoji + CJK)
    "emoji-cjk": "token-🔑-世界-한국어-✨",
    "unicode-multi-line": "鍵🔐\n第二行",
    # Unicode line/paragraph separators that str.splitlines() breaks on
    "unicode-line-separators": "sep\u2028mid\u2029dle\x85next\x0cfeed\x1cend",
    # large
    "large": _LARGE,
    "large-multi-line": _LARGE + "\n" + _LARGE,
    # escape-lookalike — backslash sequences that must NOT be interpreted
    "windows-path": "C:\\new\\path\\to\\key",
    "literal-backslash-n": "keep\\nliteral",
    "quoted-literal-backslash-n": '"looks\\nencoded"',
    "quoted-literal-backslash-r": '"looks\\rencoded"',
    "backslash-then-newline": "trailing\\\nnext",
}


def _store_and_reload(tmp_path: Path, value: str) -> tuple[Path, str]:
    """Write ``value`` as the access-token leaf via FileTokenStore; read it back."""
    bundle = tmp_path / "kairix.env"
    store = FileTokenStore(path=bundle, env={}, home=tmp_path / "home")
    store.store(
        scope="connector",
        area="gmail",
        instance=None,
        tokens=CapturedTokens(refresh_token="refresh-001", access_token=value, token_uri="https://example.test/t"),
        client=ClientCredentials(client_id="cid-001", client_secret="csec-001"),
    )
    load_secrets_file.cache_clear()
    parsed = load_secrets_file(bundle)
    return bundle, parsed[canonical_env_var("connector", "gmail", None, "access-token")]


@pytest.mark.parametrize("value", list(_CORPUS.values()), ids=list(_CORPUS))
def test_adversarial_token_round_trips_byte_identical(tmp_path: Path, value: str) -> None:
    """FileTokenStore.store → load_secrets_file returns exactly what went in."""
    _, read_back = _store_and_reload(tmp_path, value)
    assert read_back == value


@pytest.mark.parametrize("value", list(_CORPUS.values()), ids=list(_CORPUS))
def test_adversarial_token_keeps_every_bundle_line_parseable(tmp_path: Path, value: str) -> None:
    """Each leaf lands on exactly ONE ``KEY=VALUE`` line — no corruption."""
    bundle, _ = _store_and_reload(tmp_path, value)
    lines = bundle.read_text(encoding="utf-8").split("\n")
    entries = [line for line in lines if line]
    keys = [line.partition("=")[0] for line in entries]
    assert all(key.replace("_", "").isalnum() for key in keys), keys
    assert keys.count(canonical_env_var("connector", "gmail", None, "access-token")) == 1
    # The sibling leaves survive the adversarial neighbour untouched.
    load_secrets_file.cache_clear()
    parsed = load_secrets_file(bundle)
    assert parsed[canonical_env_var("connector", "gmail", None, "client-id")] == "cid-001"
    assert parsed[canonical_env_var("connector", "gmail", None, "refresh-token")] == "refresh-001"


def test_github_pem_private_key_round_trips_through_leaf_remap(tmp_path: Path) -> None:
    """The GitHub App PEM (client_secret slot → app-private-key leaf) survives."""
    bundle = tmp_path / "kairix.env"
    pem = "-----BEGIN FAKE KEY-----\n世界🔑\nC:\\new\\path\n" + "P" * (64 * 1024) + "\n-----END FAKE KEY-----\n"
    FileTokenStore(path=bundle, env={}, home=tmp_path / "home").store(
        scope="connector",
        area="github",
        instance=None,
        tokens=CapturedTokens(
            refresh_token="",
            access_token="",
            token_uri="",
            metadata={"installation_id": "70000"},
        ),
        client=ClientCredentials(client_id="42", client_secret=pem),
    )
    load_secrets_file.cache_clear()
    parsed = load_secrets_file(bundle)
    assert parsed[canonical_env_var("connector", "github", None, "app-private-key")] == pem
    assert parsed[canonical_env_var("connector", "github", None, "installation-id")] == "70000"
