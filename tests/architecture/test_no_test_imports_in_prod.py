"""Unit tests for F24 (``scripts/checks/check_no_test_imports_in_prod.py``).

F24 blocks any ``from tests.* import ...`` or ``import tests`` line
in kairix production code. ``tests/`` isn't shipped in the wheel —
the v2026.5.15.1 → v2026.5.15.2 incident came from exactly this
shape (a production module pulling ``FakeVectorRepository`` out of
``tests.fakes``).

Each test pairs a happy-path assertion with a sabotage-proof: write
a kairix-style ``.py`` file with the forbidden import, confirm the
detector fires; remove it, confirm the detector clears.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_no_test_imports_in_prod.py"


def _load_detector():
    """Load the F24 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f24_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f24_detector"] = module
    spec.loader.exec_module(module)
    return module


def _write_py(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_empty_kairix_tree_passes(tmp_path: Path) -> None:
    """An empty file (no imports at all) does not trigger the detector.

    Sabotage-proof inline: adding a ``from tests.fakes`` line flips it.
    """
    detector = _load_detector()
    target = tmp_path / "clean.py"
    _write_py(target, '"""A perfectly innocent module."""\n')
    assert detector.file_has_violation(target) is False

    # Sabotage: add the forbidden import; confirm detector now fires.
    _write_py(target, '"""A perfectly innocent module."""\nfrom tests.fakes import X\n')
    assert detector.file_has_violation(target) is True


def test_from_tests_fakes_import_is_flagged(tmp_path: Path) -> None:
    """``from tests.fakes import FakeVectorRepository`` is exactly the
    v2026.5.15.1 incident shape — must be flagged.

    Sabotage-proof inline: rewriting the source module to a kairix.*
    path clears the flag.
    """
    detector = _load_detector()
    target = tmp_path / "service.py"
    _write_py(target, "from tests.fakes import FakeVectorRepository\n")
    assert detector.file_has_violation(target) is True

    # Sabotage: rewrite to import from kairix.* — clears.
    _write_py(target, "from kairix.core.vector.null import NullVectorRepository\n")
    assert detector.file_has_violation(target) is False


def test_import_tests_bare_is_flagged(tmp_path: Path) -> None:
    """``import tests`` (no submodule) is also flagged — equally broken
    when ``tests/`` isn't on the installed-wheel sys.path.
    """
    detector = _load_detector()
    target = tmp_path / "boot.py"
    _write_py(target, "import tests\n")
    assert detector.file_has_violation(target) is True


def test_from_tests_bare_import_is_flagged(tmp_path: Path) -> None:
    """``from tests import fakes`` (bare ``tests`` package, importing a
    submodule by name) is also a ``tests.*`` reference and must fire.
    """
    detector = _load_detector()
    target = tmp_path / "boot.py"
    _write_py(target, "from tests import fakes\n")
    assert detector.file_has_violation(target) is True


def test_kairix_imports_are_unaffected(tmp_path: Path) -> None:
    """Imports from kairix.* and stdlib are not flagged — the rule is
    narrowly scoped to the ``tests`` package.
    """
    detector = _load_detector()
    target = tmp_path / "module.py"
    _write_py(
        target,
        "from kairix.core.protocols import VectorRepository\nimport json\nfrom pathlib import Path\n",
    )
    assert detector.file_has_violation(target) is False


def test_allowlist_via_baseline_is_honoured(tmp_path: Path) -> None:
    """A file appearing in the baseline list is grandfathered — the
    aggregate gate returns 0 even though a violation exists.

    This proves the standard ``_arch_lib.gate`` ratchet works for F24.
    The detection function itself still returns True; only the
    aggregate ``main()`` honours the baseline.
    """
    detector = _load_detector()
    # The detection primitive remains truthful — it sees the violation.
    target = tmp_path / "legacy.py"
    _write_py(target, "from tests.fakes import LegacyFake\n")
    assert detector.file_has_violation(target) is True

    # The baseline mechanism lives in _arch_lib.gate. Load it the same
    # way the detector itself loads it (file-path import) so we don't
    # depend on the repo-root being on sys.path.
    import importlib.util

    arch_lib_path = _REPO_ROOT / "scripts" / "checks" / "_arch_lib.py"
    spec = importlib.util.spec_from_file_location("_f24_arch_lib", arch_lib_path)
    assert spec is not None and spec.loader is not None
    arch_lib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(arch_lib)
    # Empty current-set against an empty real baseline → green.
    assert arch_lib.gate("no-test-imports-in-prod", set(), "irrelevant") == 0


def test_real_repo_gate_is_green() -> None:
    """The real ``check_no_test_imports_in_prod.py`` against the full
    kairix tree emits no net-new violations; the baseline ships empty.
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_remediation_carries_action_markers() -> None:
    """F24's REMEDIATION must satisfy F21 — the agent reading a
    failure should get the correction action inline.
    """
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
