"""Unit tests for F36 (``scripts/checks/check_f36_connector_bdd_parity.py``).

F36 requires, for every plugin under ``kairix/connectors/<name>/`` and
``kairix/extractors/<name>/``:

  1. ``tests/bdd/features/connector_<name>.feature`` (for connectors) or
     ``tests/bdd/features/extractor_<name>.feature`` (for extractors)
     exists.
  2. ``tests/bdd/features/e2e_connector_sync.feature`` contains an
     Examples-table cell equal to ``<name>`` (or the file is tagged with
     ``@<name>_no_sync``).

Each test has an inline sabotage-proof.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_f36_connector_bdd_parity.py"


def _load_detector():
    """Load the F36 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f36_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f36_detector"] = module
    spec.loader.exec_module(module)
    return module


def _mk_connector(tmp_path: Path, name: str) -> None:
    (tmp_path / "kairix" / "connectors" / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "kairix" / "connectors" / name / "__init__.py").write_text("", encoding="utf-8")


def _mk_extractor(tmp_path: Path, name: str) -> None:
    (tmp_path / "kairix" / "extractors" / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "kairix" / "extractors" / name / "__init__.py").write_text("", encoding="utf-8")


def _mk_per_plugin_feature(tmp_path: Path, kind: str, name: str) -> None:
    """``kind`` is ``"connector"`` or ``"extractor"``."""
    features = tmp_path / "tests" / "bdd" / "features"
    features.mkdir(parents=True, exist_ok=True)
    (features / f"{kind}_{name}.feature").write_text(
        f"Feature: {name} {kind}\n"
        f"  Scenario: it works\n"
        f"    Given a {name} {kind}\n"
        f"    When the operator runs sync\n"
        f"    Then docs land in the index\n",
        encoding="utf-8",
    )


def _mk_e2e_sync(
    tmp_path: Path,
    rows: list[tuple[str, str]],
    extra_tags: str = "",
) -> None:
    """Create ``e2e_connector_sync.feature`` with the given (connector,
    extractor) rows.
    """
    features = tmp_path / "tests" / "bdd" / "features"
    features.mkdir(parents=True, exist_ok=True)
    body_rows = "\n".join(f"      | {c} | {e} |" for c, e in rows)
    tag_line = f"{extra_tags}\n" if extra_tags else ""
    (features / "e2e_connector_sync.feature").write_text(
        f"{tag_line}"
        f"Feature: E2E connector sync\n"
        f"  Scenario Outline: sync with <connector> + <extractor>\n"
        f"    Given the kairix process is configured with connector <connector>\n"
        f"    And the extractor <extractor> is registered\n"
        f"    When the operator runs the connector sync\n"
        f"    Then docs land in the index\n\n"
        f"    Examples:\n"
        f"      | connector | extractor |\n"
        f"{body_rows}\n",
        encoding="utf-8",
    )


def test_current_tree_is_clean() -> None:
    """The real F36 detector run against the full repo emits no
    violations (there is no grandfathering).
    Wave 0 state: no connectors/, no extractors/, no e2e_connector_sync
    feature -> empty result.
    """
    detector = _load_detector()
    assert detector.collect_violations() == set()
    assert detector.main() == 0


def test_connector_without_per_plugin_feature_is_flagged(tmp_path: Path) -> None:
    """Plugin exists, no ``connector_<name>.feature`` — fail.

    Sabotage-proof inline: add the feature; flag clears.
    """
    detector = _load_detector()
    _mk_connector(tmp_path, "obsidian")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/connectors/obsidian") in violations

    # Sabotage: add the per-plugin feature (and a matching e2e row
    # so we don't trip the second requirement).
    _mk_per_plugin_feature(tmp_path, "connector", "obsidian")
    _mk_e2e_sync(tmp_path, [("obsidian", "markitdown")])
    assert detector.collect_violations(tmp_path) == set()


def test_extractor_without_per_plugin_feature_is_flagged(tmp_path: Path) -> None:
    """Same shape as the connector test, but on the extractor tree.

    Sabotage-proof inline: add the feature; flag clears.
    """
    detector = _load_detector()
    _mk_extractor(tmp_path, "markitdown")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/extractors/markitdown") in violations

    # Sabotage: add the per-plugin feature + e2e row.
    _mk_per_plugin_feature(tmp_path, "extractor", "markitdown")
    _mk_e2e_sync(tmp_path, [("obsidian", "markitdown")])
    assert detector.collect_violations(tmp_path) == set()


def test_full_coverage_passes(tmp_path: Path) -> None:
    """A plugin with its per-plugin feature AND a row in the E2E
    Examples table is not flagged.

    Sabotage-proof inline: delete the per-plugin feature; the
    detector fires.
    """
    detector = _load_detector()
    _mk_connector(tmp_path, "obsidian")
    _mk_extractor(tmp_path, "markitdown")
    _mk_per_plugin_feature(tmp_path, "connector", "obsidian")
    _mk_per_plugin_feature(tmp_path, "extractor", "markitdown")
    _mk_e2e_sync(tmp_path, [("obsidian", "markitdown")])
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: drop the per-plugin connector feature.
    (tmp_path / "tests" / "bdd" / "features" / "connector_obsidian.feature").unlink()
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/connectors/obsidian") in violations


def test_missing_e2e_row_is_flagged_when_e2e_exists(tmp_path: Path) -> None:
    """Per-plugin feature exists, e2e_connector_sync.feature exists,
    but the plugin has no row in its Examples table — fail.

    Sabotage-proof inline: add the row; flag clears.
    """
    detector = _load_detector()
    _mk_connector(tmp_path, "gdrive")
    _mk_per_plugin_feature(tmp_path, "connector", "gdrive")
    _mk_e2e_sync(tmp_path, [("obsidian", "markitdown")])  # gdrive missing
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/connectors/gdrive") in violations

    # Sabotage: add the row.
    _mk_e2e_sync(tmp_path, [("obsidian", "markitdown"), ("gdrive", "markitdown")])
    assert detector.collect_violations(tmp_path) == set()


def test_opt_out_tag_satisfies_e2e_requirement(tmp_path: Path) -> None:
    """A plugin can opt out of the E2E sync journey by tagging the
    feature with ``@<name>_no_sync``.

    Sabotage-proof inline: rename the tag; the detector fires again
    because the opt-out no longer matches.
    """
    detector = _load_detector()
    _mk_extractor(tmp_path, "foo")
    _mk_per_plugin_feature(tmp_path, "extractor", "foo")
    # Empty Examples table for foo, but opt-out tag is set.
    _mk_e2e_sync(tmp_path, [("obsidian", "markitdown")], extra_tags="@foo_no_sync")
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: misspell the opt-out tag.
    _mk_e2e_sync(tmp_path, [("obsidian", "markitdown")], extra_tags="@foo_no_synch")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/extractors/foo") in violations


def test_no_e2e_feature_yet_only_requires_per_plugin(tmp_path: Path) -> None:
    """When ``tests/bdd/features/e2e_connector_sync.feature`` does not
    yet exist (Wave 0 scaffold or partial scaffold), only the
    per-plugin requirement fires.
    """
    detector = _load_detector()
    _mk_connector(tmp_path, "obsidian")
    _mk_per_plugin_feature(tmp_path, "connector", "obsidian")
    # No e2e feature file at all.
    assert detector.collect_violations(tmp_path) == set()


def test_scaffolding_files_at_roots_are_not_plugins(tmp_path: Path) -> None:
    """A bare ``_base.py`` / ``__init__.py`` under
    ``kairix/connectors/`` or ``kairix/extractors/`` is not a plugin
    and doesn't need coverage.
    """
    detector = _load_detector()
    (tmp_path / "kairix" / "connectors").mkdir(parents=True)
    (tmp_path / "kairix" / "connectors" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "kairix" / "connectors" / "_base.py").write_text("# Protocol\n", encoding="utf-8")
    (tmp_path / "kairix" / "extractors").mkdir(parents=True)
    (tmp_path / "kairix" / "extractors" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "kairix" / "extractors" / "_base.py").write_text("# Protocol\n", encoding="utf-8")
    assert detector.collect_violations(tmp_path) == set()


def test_missing_directories_pass(tmp_path: Path) -> None:
    """Fresh checkout: neither directory present — no-op."""
    detector = _load_detector()
    assert detector.collect_violations(tmp_path) == set()


def test_underscore_prefixed_directories_are_not_plugins(tmp_path: Path) -> None:
    """A directory like ``kairix/connectors/_internal/`` is
    scaffolding, not a plugin.
    """
    detector = _load_detector()
    (tmp_path / "kairix" / "connectors" / "_internal").mkdir(parents=True)
    (tmp_path / "kairix" / "connectors" / "_internal" / "__init__.py").write_text("", encoding="utf-8")
    assert detector.collect_violations(tmp_path) == set()


def test_extractor_in_e2e_table_is_recognised(tmp_path: Path) -> None:
    """The E2E sync feature is parameterised over (connector, extractor)
    so the plugin identifier may sit in column 2 (extractor). The
    detector must accept any cell match.

    Sabotage-proof inline: drop the extractor cell; flag fires.
    """
    detector = _load_detector()
    _mk_extractor(tmp_path, "passthrough")
    _mk_per_plugin_feature(tmp_path, "extractor", "passthrough")
    _mk_e2e_sync(tmp_path, [("obsidian", "passthrough")])
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: drop the passthrough cell.
    _mk_e2e_sync(tmp_path, [("obsidian", "markitdown")])
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/extractors/passthrough") in violations


def test_remediation_carries_action_markers() -> None:
    """F36's REMEDIATION must satisfy F21."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
