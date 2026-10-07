"""Unit tests for F41 (``scripts/checks/check_f41_plugin_typing.py``).

F41 requires, for every plugin under
``kairix/{connectors,extractors,providers}/<name>/``:

  1. A ``py.typed`` marker file in the plugin root, AND
  2. No bare ``# type: ignore`` directives (every type-ignore has
     trailing rationale text on the same line).

Each test has an inline sabotage-proof: mutate the synthetic plugin
into / out of the violating shape and confirm the detector's verdict
flips.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_f41_plugin_typing.py"


def _load_detector():
    """Load the F41 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f41_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f41_detector"] = module
    spec.loader.exec_module(module)
    return module


def _mk_plugin(
    tmp_path: Path,
    tree: str,
    name: str,
    *,
    with_py_typed: bool = True,
    code: str = "x = 1\n",
) -> Path:
    """Scaffold a synthetic plugin at
    ``<tmp_path>/kairix/<tree>/<name>/`` and return the plugin dir.
    """
    plugin = tmp_path / "kairix" / tree / name
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "provider.py").write_text(code, encoding="utf-8")
    if with_py_typed:
        (plugin / "py.typed").write_text("", encoding="utf-8")
    return plugin


def test_conforming_plugin_is_not_flagged(tmp_path: Path) -> None:
    """A plugin with ``py.typed`` and no bare ``type: ignore`` passes.

    Sabotage-proof inline: remove ``py.typed``; the detector fires.
    """
    detector = _load_detector()
    plugin = _mk_plugin(tmp_path, "providers", "openai")
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: remove the py.typed marker.
    (plugin / "py.typed").unlink()
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/openai") in violations


def test_missing_py_typed_is_flagged(tmp_path: Path) -> None:
    """A plugin without ``py.typed`` is flagged.

    Sabotage-proof inline: create the marker; flag clears.
    """
    detector = _load_detector()
    plugin = _mk_plugin(tmp_path, "providers", "bedrock", with_py_typed=False)
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/bedrock") in violations

    # Sabotage: add the marker.
    (plugin / "py.typed").write_text("", encoding="utf-8")
    assert detector.collect_violations(tmp_path) == set()


def test_bare_type_ignore_is_flagged(tmp_path: Path) -> None:
    """A plugin with a bare ``# type: ignore`` (no rationale) is flagged
    even when ``py.typed`` exists.

    Sabotage-proof inline: append a rationale to the directive; flag
    clears.
    """
    detector = _load_detector()
    plugin = _mk_plugin(
        tmp_path,
        "providers",
        "anthropic",
        code="client = make_client()  # type: ignore[arg-type]\n",
    )
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/anthropic") in violations

    # Sabotage: append a rationale.
    (plugin / "provider.py").write_text(
        "client = make_client()  # type: ignore[arg-type] - sdk v1 still uses Any\n",
        encoding="utf-8",
    )
    assert detector.collect_violations(tmp_path) == set()


def test_type_ignore_with_em_dash_rationale_is_accepted(tmp_path: Path) -> None:
    """An em-dash separated rationale satisfies the rule."""
    detector = _load_detector()
    _mk_plugin(
        tmp_path,
        "providers",
        "ollama",
        code="raw = fetch()  # type: ignore[no-any-return] — upstream returns Any\n",
    )
    assert detector.collect_violations(tmp_path) == set()


def test_type_ignore_in_nested_file_is_flagged(tmp_path: Path) -> None:
    """The detector walks the plugin recursively — a bare ``type: ignore``
    in a nested module still trips the rule.
    """
    detector = _load_detector()
    plugin = _mk_plugin(tmp_path, "providers", "azure_foundry")
    nested = plugin / "subpkg"
    nested.mkdir()
    (nested / "__init__.py").write_text("", encoding="utf-8")
    (nested / "client.py").write_text("x: int = some_call()  # type: ignore\n", encoding="utf-8")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/azure_foundry") in violations


def test_connectors_tree_is_walked(tmp_path: Path) -> None:
    """Connectors are scanned the same way as providers."""
    detector = _load_detector()
    _mk_plugin(tmp_path, "connectors", "obsidian", with_py_typed=False)
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/connectors/obsidian") in violations


def test_extractors_tree_is_walked(tmp_path: Path) -> None:
    """Extractors are scanned the same way as providers."""
    detector = _load_detector()
    _mk_plugin(tmp_path, "extractors", "markitdown", with_py_typed=False)
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/extractors/markitdown") in violations


def test_scaffolding_files_at_tree_root_are_not_plugins(tmp_path: Path) -> None:
    """A bare ``_base.py`` / ``__init__.py`` under
    ``kairix/providers/`` is not a plugin and doesn't need ``py.typed``.
    """
    detector = _load_detector()
    (tmp_path / "kairix" / "providers").mkdir(parents=True)
    (tmp_path / "kairix" / "providers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "kairix" / "providers" / "_base.py").write_text("# Protocol\n", encoding="utf-8")
    assert detector.collect_violations(tmp_path) == set()


def test_underscore_prefixed_directories_are_not_plugins(tmp_path: Path) -> None:
    """A directory like ``kairix/providers/_shared/`` is scaffolding,
    not a plugin.
    """
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
    """The real F41 detector run against the full repo emits no
    violations (there is no grandfathering).
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_remediation_carries_action_markers() -> None:
    """F41's REMEDIATION must satisfy F21."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
