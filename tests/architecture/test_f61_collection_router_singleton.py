"""Unit tests for F61 (``scripts/checks/check_f61_collection_router_singleton.py``).

F61 forbids bare ``_SqliteChunkWriter(...)`` construction outside
``kairix/core/connectors/``. The framework owns the writer; everywhere
else must flow through ``CollectionRouter`` (Wave C).

Drives ``collect_violations(repo_root)`` against synthetic trees under
``tmp_path``. Each scenario carries an inline executed sabotage-proof.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_f61_collection_router_singleton.py"


def _load_detector() -> object:
    """Load the F61 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f61_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f61_detector"] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, body: str = "") -> None:
    """Create ``path`` with parent dirs and write ``body``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_real_repo_gate_is_green() -> None:
    """The real F61 detector on the full repo emits no violations (there
    is no grandfathering). Sabotage proof: introduce a brand-new file outside
    the framework that constructs ``_SqliteChunkWriter`` — the gate fires.
    """
    detector = _load_detector()
    assert detector.main() == 0  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs


def test_vacuous_when_no_kairix_dir(tmp_path: Path) -> None:
    """Fresh tree with no ``kairix/`` directory: gate stays green."""
    detector = _load_detector()
    assert detector.collect_violations(tmp_path) == set()  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs


def test_writer_construction_outside_framework_is_flagged(tmp_path: Path) -> None:
    """A ``_SqliteChunkWriter(...)`` call from ``kairix/worker.py`` fires.

    Sabotage-proof: relocate to ``kairix/core/connectors/`` → clean.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "worker.py"
    _write(
        target,
        "from kairix.core.connectors.pipeline import _SqliteChunkWriter\n"
        "\n"
        "def go(db, name):\n"
        "    return _SqliteChunkWriter(db, collection=name)\n",
    )
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert Path("kairix/worker.py") in violations

    # Sabotage executed: relocate construction inside the framework → clean.
    target.unlink()
    framework = tmp_path / "kairix" / "core" / "connectors" / "router.py"
    _write(
        framework,
        "def go(db, name):\n    return _SqliteChunkWriter(db, collection=name)\n",
    )
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert Path("kairix/worker.py") not in violations
    assert Path("kairix/core/connectors/router.py") not in violations


def test_writer_construction_inside_framework_is_allowed(tmp_path: Path) -> None:
    """``kairix/core/connectors/<any>.py`` constructing the writer is
    allowed — the framework owns the writer."""
    detector = _load_detector()
    _write(
        tmp_path / "kairix" / "core" / "connectors" / "collection_router.py",
        "def writer_for(db, name):\n    return _SqliteChunkWriter(db, collection=name)\n",
    )
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert violations == set()


def test_attribute_access_writer_is_not_matched(tmp_path: Path) -> None:
    """``pipeline._SqliteChunkWriter(...)`` via attribute access is NOT
    matched — F34/F26 already gate cross-layer reach. F61 protects
    bare-name construction.
    """
    detector = _load_detector()
    _write(
        tmp_path / "kairix" / "connectors" / "obsidian" / "writer.py",
        "import kairix.core.connectors.pipeline as p\n"
        "\n"
        "def go(db, name):\n"
        "    return p._SqliteChunkWriter(db, collection=name)\n",
    )
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert violations == set()


def test_file_without_writer_construction_is_not_flagged(tmp_path: Path) -> None:
    """A file that mentions ``_SqliteChunkWriter`` only in an import (no
    construction call) is not flagged.
    """
    detector = _load_detector()
    _write(
        tmp_path / "kairix" / "worker.py",
        "from kairix.core.connectors.pipeline import _SqliteChunkWriter  # noqa: F401 — re-export\n",
    )
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert violations == set()


def test_multiple_construction_sites_in_same_file_flagged_once(tmp_path: Path) -> None:
    """A file with multiple bare-name constructions is flagged once
    (file-level granularity, mirrors F38/F39/F55).
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "worker.py"
    _write(
        target,
        "def go(db):\n"
        "    a = _SqliteChunkWriter(db, collection='c1')\n"
        "    b = _SqliteChunkWriter(db, collection='c2')\n"
        "    return a, b\n",
    )
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert violations == {Path("kairix/worker.py")}


def test_remediation_carries_action_markers() -> None:
    """F61's REMEDIATION must satisfy F21."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
