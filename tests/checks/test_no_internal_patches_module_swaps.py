"""F1 static-half tests — ``patch.object``, ``sys.modules`` swaps, reloads.

Shapes 1-6 of ``scripts/checks/check_no_internal_patches.py`` (``@patch`` /
``monkeypatch.setattr`` / attribute assignment on kairix targets) are pinned
in ``tests/architecture/test_check_no_internal_patches.py``. This module pins
the direct spellings added for the static half: ``patch.object`` on a kairix
reference, a literal ``sys.modules["kairix..."]`` swap, ``importlib.reload``
of an imported kairix module, ``monkeypatch.delattr``, and builtin
``setattr`` / ``delattr`` on an imported kairix module or its attribute. Every other
spelling is the runtime guard's job (``tests/fixtures/process_state_guard.py``,
proven in ``tests/test_process_state_guard.py``).

Sabotage proof (executed): make ``_node_shape`` return ``None`` → every
positive case fails; restored.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

from check_no_internal_patches import (  # noqa: E402 — see _CHECKS_DIR sys.path insert above
    F1,
    REMEDIATION,
    file_violations,
)

pytestmark = pytest.mark.unit

_HEADER = (
    "import importlib\nimport sys\nfrom importlib import reload\nfrom unittest import mock\n"
    "from unittest.mock import patch\n\nimport kairix.core.search.rerank as rerank_mod\nimport yaml\n\n\n"
)


def _violations(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "test_sample.py"
    path.write_text(_HEADER + body + "\n", encoding="utf-8")
    return [v.split(": ", 1)[1] for v in file_violations(path)]


@pytest.mark.parametrize(
    ("statement", "shape"),
    [
        ('patch.object(rerank_mod, "RERANK_MODEL", "x")', "patch.object(<kairix ref>)"),
        ('mock.patch.object(rerank_mod, "RERANK_MODEL", "x")', "patch.object(<kairix ref>)"),
        ('patch("kairix.core.search.rerank.rerank").start()', "patch(kairix.*)"),
        ('monkeypatch.delattr(rerank_mod, "RERANK_MODEL")', "monkeypatch.setattr/delattr(<kairix target>)"),
        ('sys.modules["kairix.core.search.rerank"] = None', "sys.modules[kairix.*] swap"),
        ('sys.modules["kairix"] = None', "sys.modules[kairix.*] swap"),
        ('del sys.modules["kairix.core.search.rerank"]', "sys.modules[kairix.*] swap"),
        ("importlib.reload(rerank_mod)", "importlib.reload(<kairix module>)"),
        ("reload(rerank_mod)", "importlib.reload(<kairix module>)"),
        ('setattr(rerank_mod, "RERANK_MODEL", "x")', "setattr/delattr(<kairix target>)"),
        ('delattr(rerank_mod.rerank, "__doc__")', "setattr/delattr(<kairix target>)"),
    ],
)
def test_direct_kairix_substitution_is_flagged(tmp_path: Path, statement: str, shape: str) -> None:
    assert _violations(tmp_path, statement) == [shape]


@pytest.mark.parametrize(
    "statement",
    [
        'monkeypatch.setitem(sys.modules, "sentence_transformers", None)',
        'sys.modules["openai"] = None',
        'patch.object(yaml, "safe_load")',
        "importlib.reload(yaml)",
        'setattr(yaml, "safe_load", None)',
        'module = sys.modules.get("kairix.core.search.rerank")',
    ],
)
def test_third_party_targets_and_reads_are_not_flagged(tmp_path: Path, statement: str) -> None:
    assert _violations(tmp_path, statement) == []


def test_remediation_is_f21_actionable() -> None:
    assert REMEDIATION.startswith("Refactor to")
    for marker in ("fix:", "next:", "run:", "Pass example:", "Forbidden example:", "sys.modules", "reload"):
        assert marker in REMEDIATION


def test_rule_gate_reports_line_keys_and_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``F1(repo_root=...).run()`` scans ``tests/`` under the root, prints each
    ``path:line: shape`` and returns 1; a clean tree returns 0."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_bad.py").write_text('import sys\nsys.modules["kairix.x"] = None\n', encoding="utf-8")
    assert F1(repo_root=tmp_path).run() == 1
    assert "tests/test_bad.py:2: sys.modules[kairix.*] swap" in capsys.readouterr().out
    (tests_dir / "test_bad.py").write_text('import sys\nsys.modules["openai"] = None\n', encoding="utf-8")
    assert F1(repo_root=tmp_path).run() == 0
