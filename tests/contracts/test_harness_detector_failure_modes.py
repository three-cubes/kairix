"""F68 failure-mode contract for the ``HarnessDetector`` Protocol.

The Protocol promises detectors "never raise on missing dirs or
non-directory candidates — they return an empty tuple". ``kairix onboard
scan`` iterates every registered detector over operator-supplied paths,
so one detector raising on a typo'd path would abort the whole scan.

F43 parity: ONE body runs over every registered detector implementation
(the real Claude Code, Codex and generic detectors) — a detector that
drifts from the shared "empty, never raise" contract fails the same
assertion its siblings pass.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kairix.core.agents.detectors.base import HarnessDetector
from kairix.core.agents.detectors.claude_code import ClaudeCodeDetector
from kairix.core.agents.detectors.codex import CodexDetector
from kairix.core.agents.detectors.generic import GenericDetector

pytestmark = pytest.mark.contract

_DETECTORS: list[tuple[str, Callable[[], HarnessDetector]]] = [
    ("claude-code", ClaudeCodeDetector),
    ("codex", CodexDetector),
    ("generic", GenericDetector),
]


@pytest.mark.parametrize("name,factory", _DETECTORS)
def test_propose_surfaces_returns_empty_for_missing_or_non_directory_root(
    name: str, factory: Callable[[], HarnessDetector], tmp_path: Path
) -> None:
    """``returns_empty``: a candidate root that doesn't exist — or is a
    plain file, not a directory — yields ``()`` and never raises.

    Sabotage proof (executed): delete the ``if not candidate_root.is_dir():
    return ()`` guard in ``GenericDetector.propose_surfaces`` → the
    ``generic`` case fails (``FileNotFoundError`` / ``NotADirectoryError``
    from ``iterdir``). Restored.
    """
    detector = factory()
    not_a_dir = tmp_path / "MEMORY.md"
    not_a_dir.write_text("# a file named like a memory marker\n", encoding="utf-8")
    for candidate in (tmp_path / "no-such-agent-home", not_a_dir):
        surfaces = detector.propose_surfaces("agent-alpha", candidate)
        assert surfaces == (), f"{name} proposed {surfaces!r} for {candidate}"
