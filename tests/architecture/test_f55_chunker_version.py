"""Unit tests for F55 (``scripts/checks/check_f55_chunker_version.py``).

F55 enforces two things in tandem:
  1. Every Chunker plugin under ``kairix/chunkers/<name>/`` declares a
     module-level ``version: str``.
  2. Every ``Chunk(...)`` call site under ``kairix/**/*.py`` passes
     ``chunker_version=`` as a kwarg.

These tests drive ``collect_violations(repo_root)`` against synthetic
trees under ``tmp_path`` so the rule is provable without mutating the
real ``kairix/`` tree. Each scenario carries an inline sabotage-proof —
the mutation that flips the assertion.

Per ``feedback_sabotage_must_be_executed`` the sabotage proofs are
EXECUTED in this test file via ``tmp_path`` mutation rather than
hand-waved.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_f55_chunker_version.py"


def _load_detector() -> object:
    """Load the F55 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f55_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f55_detector"] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, body: str = "") -> None:
    """Create ``path`` with parent dirs and write ``body``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_real_repo_gate_is_green() -> None:
    """The real F55 detector run against the full repo emits no
    violations (there is no grandfathering).

    Sabotage proof for this assertion: the tmp-tree cases below prove the
    detector fires on a chunker ``Chunk(...)`` without ``chunker_version``;
    this case proves the real tree is clean with no baseline to lean on.
    """
    detector = _load_detector()
    assert detector.main() == 0  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs


def test_vacuous_when_no_kairix_dir(tmp_path: Path) -> None:
    """Fresh tree with no ``kairix/`` directory: gate stays green."""
    detector = _load_detector()
    assert detector.collect_violations(tmp_path) == set()  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs


def test_chunker_plugin_without_version_is_flagged(tmp_path: Path) -> None:
    """A chunker plugin missing the ``version: str`` declaration trips F55.

    Sabotage-proof: add ``version: str = "1"`` and re-run — set goes empty.
    """
    detector = _load_detector()
    plugin = tmp_path / "kairix" / "chunkers" / "markdown_structural" / "__init__.py"
    _write(plugin, "def make_chunker():\n    return None\n")
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert Path("kairix/chunkers/markdown_structural/__init__.py") in violations

    # Sabotage executed: add version, expect clean.
    _write(plugin, 'version: str = "1"\n\ndef make_chunker():\n    return None\n')
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert Path("kairix/chunkers/markdown_structural/__init__.py") not in violations


def test_chunker_plugin_with_version_is_clean(tmp_path: Path) -> None:
    """A chunker plugin declaring ``version: str = "..."`` passes."""
    detector = _load_detector()
    plugin = tmp_path / "kairix" / "chunkers" / "thread_aware" / "__init__.py"
    _write(plugin, 'version: str = "0.1.0"\n')
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert violations == set()


def test_chunker_plugin_with_empty_version_is_flagged(tmp_path: Path) -> None:
    """An empty ``version = ""`` doesn't count — F55 requires a non-empty
    string literal (parallel to F40).
    """
    detector = _load_detector()
    plugin = tmp_path / "kairix" / "chunkers" / "tabular" / "__init__.py"
    _write(plugin, 'version: str = ""\n')
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert Path("kairix/chunkers/tabular/__init__.py") in violations


def test_chunker_plugin_with_bare_assignment_version_is_clean(tmp_path: Path) -> None:
    """Bare ``version = "..."`` (no annotation) is accepted — mirrors F40."""
    detector = _load_detector()
    plugin = tmp_path / "kairix" / "chunkers" / "transcript" / "__init__.py"
    _write(plugin, 'version = "2"\n')
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert violations == set()


def test_chunk_call_without_chunker_version_is_flagged(tmp_path: Path) -> None:
    """A ``Chunk(...)`` call site omitting ``chunker_version=`` trips F55.

    Sabotage-proof: add ``chunker_version="1"`` and re-run — clean.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "core" / "connectors" / "silver.py"
    _write(
        target,
        "from kairix.core.protocols import Chunk\n"
        "\n"
        "def emit() -> None:\n"
        "    Chunk(text='t', content_hash='h', source_name='s',\n"
        "          source_uri='u', source_modified_at=None, sensitivity='public')\n",
    )
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert Path("kairix/core/connectors/silver.py") in violations

    # Sabotage executed: add chunker_version kwarg, expect clean.
    _write(
        target,
        "from kairix.core.protocols import Chunk\n"
        "\n"
        "def emit() -> None:\n"
        "    Chunk(text='t', content_hash='h', source_name='s',\n"
        "          source_uri='u', source_modified_at=None, sensitivity='public',\n"
        "          chunker_version='1')\n",
    )
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert Path("kairix/core/connectors/silver.py") not in violations


def test_chunk_call_with_kwargs_splat_is_accepted(tmp_path: Path) -> None:
    """``Chunk(**kwargs)`` splat is conservatively treated as supplying
    every kwarg — mirrors F39's splat behaviour.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "core" / "connectors" / "silver.py"
    _write(
        target,
        "from kairix.core.protocols import Chunk\n\ndef emit(kw):\n    Chunk(**kw)\n",
    )
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert violations == set()


def test_attribute_access_chunk_is_not_matched(tmp_path: Path) -> None:
    """``module.Chunk(...)`` via attribute access is NOT matched by F55.

    F26/F27 already catch cross-layer reach. F55 protects the
    in-module bare-name construction surface (parallel to F39).
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "connectors" / "obsidian" / "writer.py"
    _write(target, "import kairix.core.protocols as p\np.Chunk(text='t')\n")
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert violations == set()


def test_underscore_prefixed_chunker_dir_is_skipped(tmp_path: Path) -> None:
    """``kairix/chunkers/_base.py`` etc. are not plugins."""
    detector = _load_detector()
    _write(tmp_path / "kairix" / "chunkers" / "_base" / "__init__.py", "")
    violations = detector.collect_violations(tmp_path)  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert violations == set()


def test_remediation_carries_action_markers() -> None:
    """F55's REMEDIATION must satisfy F21 (``fix:`` / ``next:`` / ``run:``)."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
