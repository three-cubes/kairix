"""Unit tests for F27 (``scripts/checks/check_no_cross_provider.py``).

F27 forbids ``kairix/providers/<plugin>/**`` from importing another
plugin under ``kairix/providers/``. Plugins must stay independently
shippable. Cross-plugin work goes through ``kairix/transport/``.

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
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_no_cross_provider.py"


def _load_detector():
    """Load the F27 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f27_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f27_detector"] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_plugin_with_no_cross_imports_passes(tmp_path: Path) -> None:
    """A plugin importing only its own siblings and the shared base is
    not flagged.

    Sabotage-proof inline: add a sibling-plugin import; the detector
    fires.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "providers" / "openai" / "embed.py"
    _write(
        target,
        "from kairix.providers._base import Provider\nfrom kairix.providers.openai.client import build_client\n",
    )
    _write(tmp_path / "kairix" / "providers" / "openai" / "__init__.py", "")
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage.
    _write(target, "from kairix.providers.bedrock.sigv4 import sign\n")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/openai/embed.py") in violations


def test_plugin_importing_sibling_plugin_via_import_form_is_flagged(tmp_path: Path) -> None:
    """``import kairix.providers.<other>`` form is also detected.

    Sabotage-proof inline: rewrite as a transport import; the flag
    clears.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "providers" / "openai" / "auth.py"
    _write(target, "import kairix.providers.azure_foundry.auth\n")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/openai/auth.py") in violations

    # Sabotage: import from transport instead.
    _write(target, "from kairix.transport.auth import get_credentials\n")
    assert detector.collect_violations(tmp_path) == set()


def test_plugin_can_import_shared_base(tmp_path: Path) -> None:
    """``kairix.providers._base`` is shared scaffolding, NOT a peer
    plugin — never flagged.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "providers" / "openai" / "register.py"
    _write(target, "from kairix.providers._base import Provider, ProviderRegistry\n")
    assert detector.collect_violations(tmp_path) == set()


def test_plugin_can_import_transport(tmp_path: Path) -> None:
    """``kairix.transport.*`` is the legitimate cross-plugin seam —
    plugins use it freely.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "providers" / "bedrock" / "embed.py"
    _write(target, "from kairix.transport.pool import get_openai_client\n")
    assert detector.collect_violations(tmp_path) == set()


def test_plugin_can_import_core_protocols(tmp_path: Path) -> None:
    """A plugin importing the core Protocol surface is fine — that's
    the contract it implements.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "providers" / "openai" / "embed.py"
    _write(target, "from kairix.core.protocols import LLMBackend\n")
    assert detector.collect_violations(tmp_path) == set()


def test_root_level_provider_files_are_not_plugins(tmp_path: Path) -> None:
    """Files directly under ``kairix/providers/`` (``__init__.py``,
    ``_base.py``) are scaffolding, not plugins — F27 doesn't apply.
    """
    detector = _load_detector()
    _write(tmp_path / "kairix" / "providers" / "__init__.py", "")
    _write(
        tmp_path / "kairix" / "providers" / "_base.py",
        "# Provider Protocol lives here.\n",
    )
    assert detector.collect_violations(tmp_path) == set()


def test_missing_providers_directory_passes(tmp_path: Path) -> None:
    """Fresh checkout where ``kairix/providers/`` doesn't exist yet —
    F27 is a no-op until plugins appear.
    """
    detector = _load_detector()
    assert detector.collect_violations(tmp_path) == set()


def test_empty_providers_directory_passes(tmp_path: Path) -> None:
    """``kairix/providers/`` exists but holds no plugin subdirectories
    — gate green.
    """
    detector = _load_detector()
    (tmp_path / "kairix" / "providers").mkdir(parents=True)
    assert detector.collect_violations(tmp_path) == set()


def test_real_repo_gate_is_green() -> None:
    """The real F27 detector run against the full repo emits no
    net-new violations vs ``.architecture/baseline/F27-files.txt``.
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_remediation_carries_action_markers() -> None:
    """F27's REMEDIATION must satisfy F21."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
