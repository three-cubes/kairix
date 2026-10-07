"""Unit tests for F38 (``scripts/checks/check_f38_silver_singleton.py``).

F38 forbids chunking-function definitions (names matching ``chunk_*``,
``_chunk*``, ``tokenize_into_chunks``) anywhere under ``kairix/``
except:
  - ``kairix/core/connectors/silver.py`` (the canonical Silver home)
  - ``kairix/quality/probe/**`` (perf fixtures)
  - ``kairix/corpus/**`` (existing conversational corpus path)
  - ``kairix/core/temporal/**``, ``kairix/core/embed/**``,
    ``kairix/core/search/**``, ``kairix/use_cases/**`` (existing
    conversational chunkers, orthogonal to the Bronze→Silver flow)

Singular Silver surface; Wave 1's connectors and extractors must call
into Silver rather than grow private chunkers. Each test has an inline
sabotage-proof.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_f38_silver_singleton.py"


def _load_detector():
    """Load the F38 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f38_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f38_detector"] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_real_repo_gate_is_green() -> None:
    """The real F38 detector run against the full repo emits no
    violations (there is no grandfathering).
    Today (pre-Wave 1) every existing chunker sits in an allow-listed
    tree, so the result is vacuous-green.
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_chunker_in_silver_singleton_is_allowed(tmp_path: Path) -> None:
    """A ``chunk_*`` function in
    ``kairix/core/connectors/silver.py`` is the canonical home —
    never flagged.

    Sabotage-proof inline: relocate the same function under
    ``kairix/connectors/sharepoint/`` and the detector fires.
    """
    detector = _load_detector()
    canonical = tmp_path / "kairix" / "core" / "connectors" / "silver.py"
    _write(canonical, "def chunk_bronze_record(record):\n    return []\n")
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: relocate to a per-connector tree.
    canonical.unlink()
    sabotage = tmp_path / "kairix" / "connectors" / "sharepoint" / "chunker.py"
    _write(sabotage, "def chunk_bronze_record(record):\n    return []\n")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/connectors/sharepoint/chunker.py") in violations


def test_per_connector_chunker_is_flagged(tmp_path: Path) -> None:
    """A per-connector chunker is the exact pattern F38 exists to
    block."""
    detector = _load_detector()
    target = tmp_path / "kairix" / "connectors" / "obsidian" / "split.py"
    _write(target, "def chunk_note(text):\n    return text.split()\n")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/connectors/obsidian/split.py") in violations

    # Sabotage: move into silver.py — gate clears.
    target.unlink()
    _write(
        tmp_path / "kairix" / "core" / "connectors" / "silver.py",
        "def chunk_note(text):\n    return text.split()\n",
    )
    assert detector.collect_violations(tmp_path) == set()


def test_per_extractor_chunker_is_flagged(tmp_path: Path) -> None:
    """An extractor that grows its own chunker is rejected — extractors
    own format → bytes; Silver owns bytes → chunks."""
    detector = _load_detector()
    _write(
        tmp_path / "kairix" / "extractors" / "markitdown" / "chunk_page.py",
        "def chunk_page(md):\n    return md.splitlines()\n",
    )
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/extractors/markitdown/chunk_page.py") in violations


def test_chunker_outside_silver_in_core_connectors_is_flagged(tmp_path: Path) -> None:
    """Even inside ``kairix/core/connectors/``, the chunker must live
    in ``silver.py`` — orchestration files (``pipeline.py``,
    ``cursor_store.py``) cannot define chunking functions."""
    detector = _load_detector()
    _write(
        tmp_path / "kairix" / "core" / "connectors" / "pipeline.py",
        "def chunk_record(r):\n    return [r]\n",
    )
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/core/connectors/pipeline.py") in violations


def test_existing_conversational_chunker_paths_are_allowed(tmp_path: Path) -> None:
    """``kairix/core/temporal/``, ``kairix/core/embed/``,
    ``kairix/core/search/``, ``kairix/use_cases/``, ``kairix/corpus/``
    are explicitly orthogonal to Bronze→Silver — chunkers there stay
    allowed.
    """
    detector = _load_detector()
    _write(tmp_path / "kairix" / "core" / "temporal" / "chunker.py", "def chunk_board(p):\n    return []\n")
    _write(tmp_path / "kairix" / "core" / "embed" / "embed.py", "def chunk_text(t):\n    return [t]\n")
    _write(tmp_path / "kairix" / "core" / "search" / "rrf.py", "def chunk_date_boost():\n    return 0\n")
    _write(tmp_path / "kairix" / "use_cases" / "timeline.py", "def _chunk_to_hit(c):\n    return c\n")
    _write(tmp_path / "kairix" / "corpus" / "ingest.py", "def chunk_corpus(b):\n    return [b]\n")
    assert detector.collect_violations(tmp_path) == set()


def test_probe_chunker_is_allowed(tmp_path: Path) -> None:
    """Perf-test fixtures under ``kairix/quality/probe/`` may define
    chunking helpers — they're not a parallel Silver surface."""
    detector = _load_detector()
    _write(
        tmp_path / "kairix" / "quality" / "probe" / "chunk_perf_fixture.py",
        "def chunk_payload(p):\n    return [p]\n",
    )
    assert detector.collect_violations(tmp_path) == set()


def test_non_chunk_files_are_not_flagged(tmp_path: Path) -> None:
    """Files that don't define any chunking-named function are left
    alone, regardless of location."""
    detector = _load_detector()
    _write(tmp_path / "kairix" / "connectors" / "obsidian" / "auth.py", "def authenticate():\n    return None\n")
    _write(tmp_path / "kairix" / "extractors" / "markitdown" / "extract.py", "def extract(b):\n    return b\n")
    _write(tmp_path / "kairix" / "core" / "connectors" / "registry.py", "def register(name):\n    return None\n")
    assert detector.collect_violations(tmp_path) == set()


def test_missing_kairix_directory_passes(tmp_path: Path) -> None:
    """Fresh checkout: no ``kairix/`` directory — gate green."""
    detector = _load_detector()
    assert detector.collect_violations(tmp_path) == set()


def test_chunk_name_regex_matches_expected_shapes() -> None:
    """The naming regex picks up the documented chunking patterns."""
    detector = _load_detector()
    for good in (
        "chunk_text",
        "chunk_board",
        "chunk_file",
        "chunk_memory_log",
        "_chunk",
        "_chunk_date_boost_impl",
        "_chunk_to_hit",
        "tokenize_into_chunks",
    ):
        assert detector._is_chunk_function_name(good), good
    for bad in (
        "chunk",  # bare "chunk" — too noisy, not in pattern
        "chunker",
        "extract",
        "register",
        "_helper",
        "tokenize",
    ):
        assert not detector._is_chunk_function_name(bad), bad


def test_remediation_carries_action_markers() -> None:
    """F38's REMEDIATION must satisfy F21."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
