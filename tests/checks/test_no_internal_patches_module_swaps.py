"""F1 detector tests — ``sys.modules`` swaps and ``importlib.reload`` of kairix modules.

The F1 detector (``scripts/checks/check_no_internal_patches.py``) shapes 1-6
catch ``@patch`` / ``monkeypatch.setattr`` / attribute assignment on kairix
targets (pinned in ``tests/architecture/test_check_no_internal_patches.py``).
A paydown found tests evading those shapes one level up: replacing a whole
kairix module object in ``sys.modules`` (to simulate an import failure, or to
evict it so the next import re-runs module code) and ``importlib.reload`` of a
kairix module (to reset hidden singleton state). Both substitute an
implementation no production process runs. This module pins shape 7 (the
``sys.modules`` swap family) and shape 8 (``importlib.reload``), their
key/receiver resolution, and the third-party negatives that stay allowed.

Sabotage proofs (executed — mutate the detector, confirm red, restore, green):
  * every shape-7 positive fails when ``_is_module_swap`` skips its
    ``sys.modules`` branches (``return False`` after the reload check);
  * every shape-8 positive fails when the ``ctx.is_reload(...)`` condition
    is replaced with ``False``.
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
    REMEDIATION,
    file_has_internal_patch,
)

pytestmark = pytest.mark.unit


def _flagged(tmp_path: Path, source: str) -> bool:
    path = tmp_path / "test_sample.py"
    path.write_text(source, encoding="utf-8")
    return file_has_internal_patch(path)


# ---------------------------------------------------------------------------
# Shape 7 — sys.modules swap of a kairix module.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        'sys.modules["kairix.core.search.pipeline"] = broken',
        'sys.modules["kairix"] = broken',
        'del sys.modules["kairix.platform.llm.embed_provider"]',
        'sys.modules.pop("kairix", None)',
        'sys.modules.setdefault("kairix.core.search.config", broken)',
        'sys.modules.update({"kairix.core.search.intent": broken})',
        'monkeypatch.setitem(sys.modules, "kairix.core.search.pipeline", broken)',
        'monkeypatch.delitem(sys.modules, "kairix.core.search.pipeline")',
        'sys.modules[f"kairix.{name}"] = broken',
    ],
)
def test_sys_modules_swap_of_kairix_module_is_flagged(tmp_path: Path, statement: str) -> None:
    """Every write / eviction form on a ``kairix`` module key is a violation.

    Sabotage proof (executed): in ``_is_module_swap`` return ``False`` right
    after the reload check, i.e. stop consulting the shared ``WriteSurface``
    → every parametrised case reports clean and fails;
    restored.
    """
    src = f"import sys\n\n\ndef test_x(monkeypatch, broken, name):\n    {statement}\n"
    assert _flagged(tmp_path, src) is True


def test_sys_modules_key_held_in_a_variable_is_flagged(tmp_path: Path) -> None:
    """``name = "kairix.X"; sys.modules[name] = m`` — the literal one hop away.

    Sabotage proof (executed): make ``tainted_names`` return ``set()`` →
    the variable key no longer resolves to kairix and this fails; restored.
    """
    src = """
import sys


def test_x(broken):
    target = "kairix.core.search.pipeline"
    sys.modules[target] = broken
"""
    assert _flagged(tmp_path, src) is True


def test_aliased_sys_and_from_import_modules_are_flagged(tmp_path: Path) -> None:
    """``import sys as _sys`` and ``from sys import modules`` receivers resolve."""
    assert _flagged(tmp_path, 'import sys as _sys\n_sys.modules.pop("kairix.paths", None)\n') is True
    assert _flagged(tmp_path, 'from sys import modules\nmodules["kairix.paths"] = object()\n') is True


@pytest.mark.parametrize(
    "statement",
    [
        'sys.modules["openai"] = stub',
        'monkeypatch.setitem(sys.modules, "sentence_transformers", None)',
        'sys.modules.pop("spacy", None)',
        'sys.modules["tests._fake_kairix_handlers.nonzero"] = stub',
        'sys.modules["_f52_detector"] = stub',
        'loaded = sys.modules["kairix.core.search.pipeline"]',
        'present = "kairix.paths" in sys.modules',
    ],
)
def test_third_party_swaps_and_reads_are_not_flagged(tmp_path: Path, statement: str) -> None:
    """Faking an external SDK import, test-local modules, and READS of
    ``sys.modules`` stay allowed — F1 blocks only kairix substitution."""
    src = f"import sys\n\n\ndef test_x(monkeypatch, stub):\n    {statement}\n"
    assert _flagged(tmp_path, src) is False


# ---------------------------------------------------------------------------
# Shape 8 — importlib.reload of a kairix module.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimport kairix.core.search.rerank as rerank_module\nimportlib.reload(rerank_module)\n",
        "import importlib\nimport kairix.core.search.rerank\nimportlib.reload(kairix.core.search.rerank)\n",
        "import importlib\nfrom kairix.core.search import rerank\nimportlib.reload(rerank)\n",
        'import importlib\nimport sys\nimportlib.reload(sys.modules["kairix.agents.onboarding.cli"])\n',
        'import importlib\nimportlib.reload(importlib.import_module("kairix.paths"))\n',
        "from importlib import reload\nimport kairix.paths as paths_mod\nreload(paths_mod)\n",
    ],
)
def test_reload_of_kairix_module_is_flagged(tmp_path: Path, source: str) -> None:
    """Reloading a kairix module, however it is referenced, is a violation.

    Sabotage proof (executed): replace ``ctx.is_reload(node.func)`` in
    ``_is_module_swap`` with ``False`` → every parametrised case reports
    clean and fails; restored.
    """
    assert _flagged(tmp_path, source) is True


def test_reload_of_non_kairix_module_is_not_flagged(tmp_path: Path) -> None:
    """Reloading a stdlib / third-party module stays allowed."""
    src = "import importlib\nimport json\nimportlib.reload(json)\n"
    assert _flagged(tmp_path, src) is False


def test_reload_through_an_aliased_import_module_is_flagged(tmp_path: Path) -> None:
    """Codex PR #814 thread: ``from importlib import import_module as load``
    is tracked like the ``reload`` alias.

    Sabotage proof (executed): drop the ``from_imports(tree, "importlib",
    "import_module")`` term from ``import_module_names`` → reports clean;
    restored.
    """
    src = 'from importlib import import_module as load, reload\nreload(load("kairix.paths"))\n'
    assert _flagged(tmp_path, src) is True


# ---------------------------------------------------------------------------
# Opaque sys.modules.update payloads + helper-returned keys (PR #814 threads).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "sys.modules.update(mods)",
        "sys.modules.update(build_mods())",
        "sys.modules.update({name: stub for name in names})",
        "sys.modules.update(**mods)",
    ],
)
def test_sys_modules_update_with_opaque_mapping_is_flagged(tmp_path: Path, statement: str) -> None:
    """An opaque ``sys.modules.update`` payload can install a kairix module the
    AST cannot see — treated like F2's opaque ``os.environ.update``.

    Sabotage proof (executed): make ``WriteSurface.mapping_may_carry`` inspect only
    dict literals (return ``False`` for anything else) → the variable, call
    and comprehension cases report clean (``**mods`` is still caught as a
    spread); restored.
    """
    src = f"import sys\n\n\ndef test_x(mods, build_mods, names, stub):\n    {statement}\n"
    assert _flagged(tmp_path, src) is True


def test_sys_modules_update_with_third_party_literal_is_not_flagged(tmp_path: Path) -> None:
    src = 'import sys\n\n\ndef test_x(stub):\n    sys.modules.update({"openai": stub})\n'
    assert _flagged(tmp_path, src) is False


def test_sys_modules_key_returned_by_a_helper_call_is_flagged(tmp_path: Path) -> None:
    """``sys.modules.pop(module_key(), None)`` with a helper returning a
    ``"kairix..."`` literal.

    Sabotage proof (executed): drop the ``ast.Call`` branch of
    ``key_is_protected`` → reports clean; restored.
    """
    src = (
        'import sys\n\n\ndef module_key():\n    return "kairix.paths"\n\n\n'
        "def test_x():\n    sys.modules.pop(module_key(), None)\n"
    )
    assert _flagged(tmp_path, src) is True


# ---------------------------------------------------------------------------
# Failure message contract.
# ---------------------------------------------------------------------------


def test_remediation_names_the_new_shapes_and_is_f21_actionable() -> None:
    """The message names the defect and the refactor, shows the new
    forbidden shapes, and carries fix:/next:/run: + Pass/Forbidden (F21)."""
    assert REMEDIATION.startswith("kairix-internal substitution found in a test")
    assert "Refactor to constructor injection" in REMEDIATION
    for marker in ("fix:", "next:", "run:", "Pass example:", "Forbidden example:"):
        assert marker in REMEDIATION
    assert 'sys.modules["kairix.core.search.pipeline"]' in REMEDIATION
    assert "importlib.reload(kairix.core.search.rerank)" in REMEDIATION
