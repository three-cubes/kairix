"""Unit tests for F28 (``scripts/checks/check_provider_bdd_completeness.py``).

F28 requires, for every plugin under ``kairix/providers/<name>/``:

  1. ``tests/bdd/features/provider_<name>.feature`` exists.
  2. Every ``tests/bdd/features/e2e_provider_*.feature`` has an
     Examples-table row whose first cell is ``<name>`` (or the file is
     tagged with the ``@<name>_no_<journey>`` opt-out).

Each test has an inline sabotage-proof.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_provider_bdd_completeness.py"


def _load_detector():
    """Load the F28 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f28_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f28_detector"] = module
    spec.loader.exec_module(module)
    return module


def _mk_plugin(tmp_path: Path, name: str) -> None:
    (tmp_path / "kairix" / "providers" / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "kairix" / "providers" / name / "__init__.py").write_text("", encoding="utf-8")


def _mk_per_plugin_feature(tmp_path: Path, name: str) -> None:
    features = tmp_path / "tests" / "bdd" / "features"
    features.mkdir(parents=True, exist_ok=True)
    (features / f"provider_{name}.feature").write_text(
        f"Feature: {name} provider\n"
        f"  Scenario: it works\n"
        f"    Given a {name} plugin\n"
        f"    When the caller invokes embed_batch\n"
        f"    Then a vector is returned\n",
        encoding="utf-8",
    )


def _mk_e2e_feature(tmp_path: Path, journey: str, rows: list[str], extra_tags: str = "") -> None:
    features = tmp_path / "tests" / "bdd" / "features"
    features.mkdir(parents=True, exist_ok=True)
    body_rows = "\n".join(f"      | {row} | model |" for row in rows)
    tag_line = f"{extra_tags}\n" if extra_tags else ""
    (features / f"e2e_provider_{journey}.feature").write_text(
        f"{tag_line}"
        f"Feature: E2E provider {journey}\n"
        f"  Scenario Outline: {journey} with provider <provider>\n"
        f"    Given the kairix process is configured with provider <provider>\n"
        f"    When the caller does {journey}\n"
        f"    Then it succeeds\n\n"
        f"    Examples:\n"
        f"      | provider | model |\n"
        f"{body_rows}\n",
        encoding="utf-8",
    )


def test_plugin_with_full_coverage_passes(tmp_path: Path) -> None:
    """A plugin with its per-plugin feature AND a row in every E2E
    journey is not flagged.

    Sabotage-proof inline: delete the per-plugin feature; the
    detector fires.
    """
    detector = _load_detector()
    _mk_plugin(tmp_path, "openai")
    _mk_per_plugin_feature(tmp_path, "openai")
    _mk_e2e_feature(tmp_path, "embed", ["openai"])
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: drop the per-plugin feature.
    (tmp_path / "tests" / "bdd" / "features" / "provider_openai.feature").unlink()
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/openai") in violations


def test_missing_per_plugin_feature_is_flagged(tmp_path: Path) -> None:
    """Plugin exists, no ``provider_<name>.feature`` — fail.

    Sabotage-proof inline: create the feature; flag clears.
    """
    detector = _load_detector()
    _mk_plugin(tmp_path, "bedrock")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/bedrock") in violations

    # Sabotage: add the feature.
    _mk_per_plugin_feature(tmp_path, "bedrock")
    assert detector.collect_violations(tmp_path) == set()


def test_missing_e2e_examples_row_is_flagged(tmp_path: Path) -> None:
    """Per-plugin feature exists, but the E2E journey has no
    Examples row for the plugin — fail.

    Sabotage-proof inline: add the row; flag clears.
    """
    detector = _load_detector()
    _mk_plugin(tmp_path, "anthropic")
    _mk_per_plugin_feature(tmp_path, "anthropic")
    _mk_e2e_feature(tmp_path, "chat", ["openai"])  # anthropic missing
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/anthropic") in violations

    # Sabotage: add the row.
    _mk_e2e_feature(tmp_path, "chat", ["openai", "anthropic"])
    assert detector.collect_violations(tmp_path) == set()


def test_opt_out_tag_satisfies_e2e_requirement(tmp_path: Path) -> None:
    """A plugin can opt out of a single E2E journey by tagging the
    feature with ``@<name>_no_<journey>``.

    Sabotage-proof inline: rename the tag; the detector fires again
    because the opt-out no longer matches.
    """
    detector = _load_detector()
    _mk_plugin(tmp_path, "embedonly")
    _mk_per_plugin_feature(tmp_path, "embedonly")
    # Embed journey: include the row.
    _mk_e2e_feature(tmp_path, "embed", ["embedonly"])
    # Chat journey: opt out via tag.
    _mk_e2e_feature(tmp_path, "chat", ["openai"], extra_tags="@embedonly_no_chat")
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: misspell the opt-out tag.
    _mk_e2e_feature(tmp_path, "chat", ["openai"], extra_tags="@embedonly_no_chatt")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/embedonly") in violations


def test_no_e2e_features_yet_only_requires_per_plugin(tmp_path: Path) -> None:
    """When ``tests/bdd/features/`` has no e2e_provider_*.feature
    files yet (Wave 1 scaffold), only the per-plugin requirement
    fires.
    """
    detector = _load_detector()
    _mk_plugin(tmp_path, "ollama")
    _mk_per_plugin_feature(tmp_path, "ollama")
    # No e2e files at all.
    assert detector.collect_violations(tmp_path) == set()


def test_scaffolding_files_at_providers_root_are_not_plugins(tmp_path: Path) -> None:
    """A bare ``_base.py`` / ``__init__.py`` under
    ``kairix/providers/`` is not a plugin and doesn't need coverage.
    """
    detector = _load_detector()
    (tmp_path / "kairix" / "providers").mkdir(parents=True)
    (tmp_path / "kairix" / "providers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "kairix" / "providers" / "_base.py").write_text("# Protocol\n", encoding="utf-8")
    assert detector.collect_violations(tmp_path) == set()


def test_missing_providers_directory_passes(tmp_path: Path) -> None:
    """Fresh checkout: no ``kairix/providers/`` directory — no-op."""
    detector = _load_detector()
    assert detector.collect_violations(tmp_path) == set()


def test_empty_providers_directory_passes(tmp_path: Path) -> None:
    """``kairix/providers/`` exists but holds no plugin directories."""
    detector = _load_detector()
    (tmp_path / "kairix" / "providers").mkdir(parents=True)
    assert detector.collect_violations(tmp_path) == set()


def test_underscore_prefixed_directories_are_not_plugins(tmp_path: Path) -> None:
    """A directory like ``kairix/providers/_internal/`` is scaffolding,
    not a plugin.
    """
    detector = _load_detector()
    (tmp_path / "kairix" / "providers" / "_internal").mkdir(parents=True)
    (tmp_path / "kairix" / "providers" / "_internal" / "__init__.py").write_text("", encoding="utf-8")
    assert detector.collect_violations(tmp_path) == set()


def test_real_repo_gate_is_green() -> None:
    """The real F28 detector run against the full repo emits no
    net-new violations vs ``.architecture/baseline/F28-files.txt``.
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_remediation_carries_action_markers() -> None:
    """F28's REMEDIATION must satisfy F21."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
