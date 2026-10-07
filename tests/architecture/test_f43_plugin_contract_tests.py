"""Unit tests for F43 (``scripts/checks/check_f43_plugin_contract_tests.py``).

F43 requires, for every plugin under
``kairix/{connectors,extractors,providers}/<name>/``:

  * A ``tests/contracts/test_<name>_protocol.py`` file that
    imports BOTH the canonical fake from ``tests.fakes`` AND the
    real implementation from ``kairix.<tree>.<name>``.

Each test scaffolds a synthetic plugin + (optional) contract test
under tmpdir and verifies the detector's verdict, with an inline
sabotage-proof flipping the violating shape.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_f43_plugin_contract_tests.py"


def _load_detector():
    """Load the F43 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f43_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f43_detector"] = module
    spec.loader.exec_module(module)
    return module


def _mk_plugin(tmp_path: Path, tree: str, name: str) -> Path:
    """Scaffold a synthetic plugin at
    ``<tmp_path>/kairix/<tree>/<name>/`` and return the plugin dir.
    """
    plugin = tmp_path / "kairix" / tree / name
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    return plugin


def _mk_contract_test(
    tmp_path: Path,
    tree: str,
    name: str,
    *,
    body: str,
) -> Path:
    """Write a synthetic contract test at
    ``<tmp_path>/tests/contracts/test_<name>_protocol.py`` with the
    given ``body``.
    """
    contracts = tmp_path / "tests" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    target = contracts / f"test_{name}_protocol.py"
    target.write_text(body, encoding="utf-8")
    return target


def test_plugin_with_conforming_contract_test_is_not_flagged(tmp_path: Path) -> None:
    """A plugin with a contract test importing both fake and real is
    not flagged.

    Sabotage-proof inline: remove the canonical-fake import; the
    detector fires.
    """
    detector = _load_detector()
    _mk_plugin(tmp_path, "providers", "openai")
    _mk_contract_test(
        tmp_path,
        "providers",
        "openai",
        body=(
            "from tests.fakes import FakeOpenAIProvider\n"
            "from kairix.providers.openai import OpenAIProvider\n"
            "# F43-single-impl: limb-1 fixture\n"
            "def test_shape():\n    assert FakeOpenAIProvider and OpenAIProvider\n"
        ),
    )
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: drop the tests.fakes import.
    _mk_contract_test(
        tmp_path,
        "providers",
        "openai",
        body=("from kairix.providers.openai import OpenAIProvider\ndef test_shape():\n    assert OpenAIProvider\n"),
    )
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/openai") in violations


def test_missing_contract_test_is_flagged(tmp_path: Path) -> None:
    """A plugin without ``test_<name>_protocol.py`` is flagged.

    Sabotage-proof inline: create the file with both imports; flag
    clears.
    """
    detector = _load_detector()
    _mk_plugin(tmp_path, "providers", "bedrock")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/bedrock") in violations

    # Sabotage: add the contract test.
    _mk_contract_test(
        tmp_path,
        "providers",
        "bedrock",
        body=(
            "from tests.fakes import FakeBedrockProvider\n"
            "from kairix.providers.bedrock import BedrockProvider\n"
            "# F43-single-impl: limb-1 fixture\n"
            "def test_shape():\n    assert FakeBedrockProvider and BedrockProvider\n"
        ),
    )
    assert detector.collect_violations(tmp_path) == set()


def test_contract_test_missing_real_import_is_flagged(tmp_path: Path) -> None:
    """File exists but doesn't import the real plugin module."""
    detector = _load_detector()
    _mk_plugin(tmp_path, "providers", "anthropic")
    _mk_contract_test(
        tmp_path,
        "providers",
        "anthropic",
        body=("from tests.fakes import FakeAnthropicProvider\ndef test_shape():\n    assert FakeAnthropicProvider\n"),
    )
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/anthropic") in violations


def test_contract_test_missing_fake_import_is_flagged(tmp_path: Path) -> None:
    """File exists but doesn't import the canonical fake."""
    detector = _load_detector()
    _mk_plugin(tmp_path, "providers", "ollama")
    _mk_contract_test(
        tmp_path,
        "providers",
        "ollama",
        body=("from kairix.providers.ollama import OllamaProvider\ndef test_shape():\n    assert OllamaProvider\n"),
    )
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/ollama") in violations


def test_import_from_nested_module_path_is_accepted(tmp_path: Path) -> None:
    """``from kairix.providers.openai.provider import OpenAIProvider``
    counts as importing the real plugin module.
    """
    detector = _load_detector()
    _mk_plugin(tmp_path, "providers", "openai")
    _mk_contract_test(
        tmp_path,
        "providers",
        "openai",
        body=(
            "from tests.fakes import FakeOpenAIProvider\n"
            "from kairix.providers.openai.provider import OpenAIProvider\n"
            "# F43-single-impl: limb-1 fixture\n"
            "def test_shape():\n    assert FakeOpenAIProvider and OpenAIProvider\n"
        ),
    )
    assert detector.collect_violations(tmp_path) == set()


def test_import_module_form_is_accepted(tmp_path: Path) -> None:
    """``import kairix.providers.openai`` + ``import tests.fakes``
    counts.
    """
    detector = _load_detector()
    _mk_plugin(tmp_path, "providers", "openai")
    _mk_contract_test(
        tmp_path,
        "providers",
        "openai",
        body=(
            "import tests.fakes\n"
            "import kairix.providers.openai\n"
            "# F43-single-impl: limb-1 fixture\n"
            "def test_shape():\n"
            "    assert tests.fakes is not None\n"
            "    assert kairix.providers.openai is not None\n"
        ),
    )
    assert detector.collect_violations(tmp_path) == set()


def test_connectors_tree_is_walked(tmp_path: Path) -> None:
    """Connectors are scanned the same way as providers."""
    detector = _load_detector()
    _mk_plugin(tmp_path, "connectors", "obsidian")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/connectors/obsidian") in violations


def test_extractors_tree_is_walked(tmp_path: Path) -> None:
    """Extractors are scanned the same way as providers."""
    detector = _load_detector()
    _mk_plugin(tmp_path, "extractors", "markitdown")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/extractors/markitdown") in violations


def test_scaffolding_files_at_tree_root_are_not_plugins(tmp_path: Path) -> None:
    """``__init__.py`` / ``_base.py`` at the tree root are not
    plugins.
    """
    detector = _load_detector()
    (tmp_path / "kairix" / "providers").mkdir(parents=True)
    (tmp_path / "kairix" / "providers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "kairix" / "providers" / "_base.py").write_text("# Protocol\n", encoding="utf-8")
    assert detector.collect_violations(tmp_path) == set()


def test_underscore_prefixed_directories_are_not_plugins(tmp_path: Path) -> None:
    """``kairix/providers/_shared/`` is scaffolding, not a plugin."""
    detector = _load_detector()
    shared = tmp_path / "kairix" / "providers" / "_shared"
    shared.mkdir(parents=True)
    (shared / "__init__.py").write_text("", encoding="utf-8")
    assert detector.collect_violations(tmp_path) == set()


def test_missing_plugin_trees_passes(tmp_path: Path) -> None:
    """Fresh checkout: no plugin trees — detector is a no-op."""
    detector = _load_detector()
    assert detector.collect_violations(tmp_path) == set()


def test_real_repo_gate_is_green() -> None:
    """The real F43 detector against the full repo emits no
    violations (there is no grandfathering).
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_remediation_carries_action_markers() -> None:
    """F43's REMEDIATION must satisfy F21."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
