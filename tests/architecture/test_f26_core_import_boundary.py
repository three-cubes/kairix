"""Unit tests for F26 (``scripts/checks/check_provider_layer_imports.py``).

F26 forbids ``kairix/core/**`` from importing
``kairix/providers/**`` or ``kairix/transport/**`` — domain code talks
to those layers through Protocols only.

Each test has an inline sabotage-proof: introduce a violation, confirm
the detector flags it; remove the violation, confirm the detector
clears.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_provider_layer_imports.py"


def _load_detector():
    """Load the F26 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f26_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f26_detector"] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_core_file_with_no_imports_passes(tmp_path: Path) -> None:
    """A core file that imports nothing forbidden is not flagged.

    Sabotage-proof inline: adding ``from kairix.transport import x``
    causes the detector to flag the file.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "core" / "search.py"
    _write(target, '"""Domain module — pure."""\n')
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage.
    _write(target, "from kairix.transport.pool import make_openai_client\n")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/core/search.py") in violations


def test_core_imports_protocol_passes(tmp_path: Path) -> None:
    """``from kairix.core.protocols import X`` is the seam — always allowed.

    Sabotage-proof inline: swap the import for ``kairix.providers``
    and the detector fires.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "core" / "pipeline.py"
    _write(target, "from kairix.core.protocols import EmbeddingService\n")
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage.
    _write(target, "from kairix.providers.openai import OpenAIProvider\n")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/core/pipeline.py") in violations


def test_core_imports_providers_is_flagged(tmp_path: Path) -> None:
    """``from kairix.providers.X import Y`` from inside core is rejected.

    Sabotage-proof inline: changing the import to a sibling
    ``kairix.core`` module clears the flag.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "core" / "factory.py"
    _write(target, "from kairix.providers.azure_foundry import make_provider\n")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/core/factory.py") in violations

    # Sabotage: replace with a legal core sibling import.
    _write(target, "from kairix.core.protocols import LLMBackend\n")
    assert detector.collect_violations(tmp_path) == set()


def test_core_imports_transport_via_plain_import_is_flagged(tmp_path: Path) -> None:
    """``import kairix.transport.X`` form is also detected (not just
    ``from ... import ...``).

    Sabotage-proof inline: replace with a non-kairix import; the flag
    clears.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "core" / "boot.py"
    _write(target, "import kairix.transport.pool\n")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/core/boot.py") in violations

    # Sabotage.
    _write(target, "import logging\n")
    assert detector.collect_violations(tmp_path) == set()


def test_missing_core_directory_passes(tmp_path: Path) -> None:
    """A fresh checkout where ``kairix/core/`` doesn't exist yet
    must not false-positive — F26 is a no-op until core code appears.
    """
    detector = _load_detector()
    assert detector.collect_violations(tmp_path) == set()


def test_empty_core_directory_passes(tmp_path: Path) -> None:
    """``kairix/core/`` exists but contains no .py files — gate green."""
    detector = _load_detector()
    (tmp_path / "kairix" / "core").mkdir(parents=True)
    assert detector.collect_violations(tmp_path) == set()


def test_kairix_providers_sibling_module_does_not_match_prefix(tmp_path: Path) -> None:
    """``kairix.providers_helpers`` (hypothetical sibling, NOT under
    kairix/providers/) must not trip the prefix match — the rule is
    anchored on the dotted boundary.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "core" / "x.py"
    _write(target, "from kairix.providers_helpers import noop\n")
    assert detector.collect_violations(tmp_path) == set()


def test_real_repo_gate_is_green() -> None:
    """The real F26 detector run against the full repo emits no
    net-new violations vs ``.architecture/baseline/F26-files.txt``.
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_remediation_carries_action_markers() -> None:
    """F26's own REMEDIATION must satisfy F21 — the agent reading a
    failure must get the correction action inline.
    """
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
