"""Adversarial round-trip corpus for ``write_config_updates`` → ``load_merged_mapping``.

# F87-corpus: config_write_load

The setup wizard persists operator config through
:func:`kairix.platform.setup.backends.write_config_updates` (merge-write
into the wizard's config target, overlay-aware); every runtime consumer
reads it back through the canonical layered reader
:func:`kairix.config_layers.load_merged_mapping`. Operator-supplied
config values are free text — folder paths, display names, descriptions,
pasted multi-line blocks — so the pair must round-trip the four
adversarial material classes F87 requires, not just ASCII slugs:

* multi-line — embedded ``\\n`` / ``\\r\\n`` / bare ``\\r``;
* unicode — emoji AND CJK code points, in values AND keys;
* large — a value >= 64 KiB (YAML line folding / buffer ceilings);
* escape-lookalike — Windows paths and literal backslash sequences
  (``C:\\new\\path``, a literal ``\\n``) that must survive verbatim,
  plus YAML-syntax lookalikes (``key: value``, ``# comment``, ``&anchor``).

Both the legacy single-file target (``config_path=``) and the overlay
target (``overlay_path=``) are driven; the reader resolves through its
F2-clean ``env=`` seam, so no ``KAIRIX_*`` process env is touched.

Sabotage-proof (executed): made the writer's ``_deep_coerce_mapping``
drop carriage returns from string values before the merge-write — the
``crlf`` and ``bare-cr`` cases failed on both targets (4 failures).
Restored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kairix.config_layers import load_merged_mapping
from kairix.platform.setup.backends import write_config_updates

pytestmark = pytest.mark.integration

_LARGE_WORDS = ("lorem ipsum 世界 " * ((64 * 1024) // 16 + 1))[: 64 * 1024 + 7]

_CORPUS: dict[str, str] = {
    # multi-line
    "lf": "line-one\nline-two\n",
    "crlf": "line-one\r\nline-two\r\n",
    "bare-cr": "carriage\rreturn",
    "leading-trailing-space-lines": "  indented\n\ttabbed\ntrailing  \n",
    # unicode (emoji + CJK)
    "emoji-cjk": "Team 🔑 世界 한국어 ✨",
    "unicode-multi-line": "鍵🔐\n第二行",
    # large (>= 64 KiB) — with spaces (foldable) and without (unfoldable)
    "large-words": _LARGE_WORDS,
    "large-solid": "X" * (64 * 1024),
    # escape-lookalike — must NOT be interpreted
    "windows-path": "C:\\new\\path\\to\\docs",
    "literal-backslash-n": "keep\\nliteral",
    "quoted-lookalike": '"looks\\nencoded"',
    "yaml-syntax-lookalike": "key: value # not-a-comment &anchor *alias",
    "leading-dash": "- not a list item",
}


def _round_trip(tmp_path: Path, updates: dict[str, Any], *, overlay: bool) -> dict[str, Any]:
    target = tmp_path / ("overlay.yaml" if overlay else "kairix.config.yaml")
    written = write_config_updates(
        updates,
        overlay_path=str(target) if overlay else None,
        config_path=None if overlay else str(target),
        env={},
        home=tmp_path / "home",
    )
    assert written == target
    env = {"KAIRIX_CONFIG_OVERLAY_PATH": str(target)} if overlay else {"KAIRIX_CONFIG_PATH": str(target)}
    return load_merged_mapping(env=env, image_base_default=tmp_path / "no-image-base.yaml")


@pytest.mark.parametrize("overlay", [False, True], ids=["single-file", "overlay"])
@pytest.mark.parametrize("value", list(_CORPUS.values()), ids=list(_CORPUS))
def test_adversarial_value_round_trips_exactly(tmp_path: Path, value: str, overlay: bool) -> None:
    """A nested free-text config value reads back byte-identical."""
    merged = _round_trip(tmp_path, {"agents": {"agent-alpha": {"description": value}}}, overlay=overlay)
    assert merged["agents"]["agent-alpha"]["description"] == value


def test_unicode_and_lookalike_keys_round_trip(tmp_path: Path) -> None:
    """Keys carry the same adversarial shapes as values — they survive too."""
    updates = {
        "collections": {
            "研究-🔬": {"path": "C:\\new\\研究"},
            "key: with colon": {"path": "a\nb"},
        },
    }
    merged = _round_trip(tmp_path, updates, overlay=False)
    assert merged["collections"] == updates["collections"]


def test_second_write_merges_without_corrupting_adversarial_sibling(tmp_path: Path) -> None:
    """A later merge-write re-reads + re-dumps the file; the first value survives."""
    target = tmp_path / "kairix.config.yaml"
    first = _CORPUS["unicode-multi-line"] + _CORPUS["windows-path"] + "Z" * (64 * 1024)
    write_config_updates({"paths": {"note": first}}, overlay_path=None, config_path=str(target), env={})
    write_config_updates({"paths": {"document_root": "/data/文档"}}, overlay_path=None, config_path=str(target), env={})
    merged = load_merged_mapping(env={"KAIRIX_CONFIG_PATH": str(target)})
    assert merged["paths"] == {"note": first, "document_root": "/data/文档"}
