"""Tests for the F73 pattern loader, scope filter, and exempt logic.

Patterns are loaded at runtime from the ``PRIVATE_INFRA_PATTERNS`` env
var — populated in CI from the org secret, and locally from the org
variable via ``scripts/fetch-fitness-config.sh`` — with a
gitignored ``.private-infra-patterns`` file as the last-resort fallback.
These tests use synthetic patterns so the test file carries no
operator-specific literals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

# Import depends on the sys.path mutation above — the detector lives
# outside the kairix package (repo-fitness script, not app code).
import check_no_private_infra_refs as infra  # noqa: E402
from check_no_private_infra_refs import (  # noqa: E402
    PATTERNS_ENV_VAR,
    _compile_patterns,
    _is_in_scope,
    _load_patterns,
    _scan_file,
)

pytestmark = pytest.mark.unit

# The detector's env var, spelled as a literal so F2 (default-deny) can PROVE
# the ``monkeypatch.setenv`` key below is not a ``KAIRIX_*`` variable — an
# imported constant is not statically resolvable across modules.
# ``test_patterns_env_var_literal_matches_detector`` pins the two together.
_PATTERNS_ENV_VAR = "PRIVATE_INFRA_PATTERNS"


def test_patterns_env_var_literal_matches_detector() -> None:
    assert _PATTERNS_ENV_VAR == PATTERNS_ENV_VAR


_SYNTHETIC_PATTERN_SOURCE = """
# Comment lines are skipped
azure-vm-shape: \\bsynthetic-test-vm-\\d{3}\\b
azure-kv-shape: \\bsynthetic-test-kv-\\d{3}\\b

# Blank lines + label-less patterns also work:
\\bsynthetic-test-storage\\b
"""


def test_compile_patterns_parses_label_prefix() -> None:
    patterns = _compile_patterns(_SYNTHETIC_PATTERN_SOURCE)
    labels = [label for label, _ in patterns]
    assert "azure-vm-shape" in labels
    assert "azure-kv-shape" in labels


def test_compile_patterns_skips_blanks_and_comments() -> None:
    raw = "# only comments\n\n# and blanks\n"
    assert _compile_patterns(raw) == []


def test_compile_patterns_warns_on_invalid_regex(capsys: pytest.CaptureFixture[str]) -> None:
    patterns = _compile_patterns("broken: [unclosed\nworking: \\bok\\b\n")
    assert len(patterns) == 1
    assert patterns[0][0] == "working"
    captured = capsys.readouterr()
    assert "invalid regex" in captured.err.lower()


def test_compile_patterns_unlabelled_lines_get_synthetic_label() -> None:
    patterns = _compile_patterns("\\bfoo\\b\n\\bbar\\b\n")
    labels = [label for label, _ in patterns]
    assert all(label.startswith("pattern-") for label in labels)
    assert len(patterns) == 2


def test_load_patterns_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_PATTERNS_ENV_VAR, "from-env: \\bsynthetic-env-marker\\b")
    patterns = _load_patterns()
    labels = [label for label, _ in patterns]
    assert "from-env" in labels


def test_load_patterns_env_overrides_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_PATTERNS_ENV_VAR, "from-env: \\bonly-from-env\\b")
    patterns = _load_patterns()
    labels = [label for label, _ in patterns]
    assert labels == ["from-env"]


def test_scan_file_returns_one_hit_per_match(tmp_path: Path) -> None:
    patterns = _compile_patterns("test-marker: \\bsynthetic-test-vm-001\\b")
    sample = tmp_path / "kairix" / "sample.py"
    sample.parent.mkdir(parents=True)
    sample.write_text(
        "NAME = 'synthetic-test-vm-001'\nOTHER = 'synthetic-test-vm-001-prod'\n",
        encoding="utf-8",
    )
    hits = _scan_file(sample, "kairix/sample.py", patterns)
    assert len(hits) == 2
    assert all("test-marker" in h for h in hits)


def test_scan_file_skips_generic_placeholders(tmp_path: Path) -> None:
    patterns = _compile_patterns("vm-shape: \\bsynthetic-test-vm-\\d+\\b")
    sample = tmp_path / "kairix" / "sample.py"
    sample.parent.mkdir(parents=True)
    sample.write_text(
        "NAME = '<your-vm-name>'\nHOST = 'example.com'\n",
        encoding="utf-8",
    )
    assert _scan_file(sample, "kairix/sample.py", patterns) == []


def test_in_scope_includes_production_code() -> None:
    assert _is_in_scope("kairix/worker.py")
    assert _is_in_scope("scripts/checks/check_anything.py")
    assert _is_in_scope("tests/unit/test_thing.py")
    assert _is_in_scope("docs/architecture/some-adr.md")
    assert _is_in_scope("CLAUDE.md")


def test_in_scope_excludes_vendored_and_unrelated_trees() -> None:
    assert not _is_in_scope("reference-library/some/citation.md")
    assert not _is_in_scope("benchmark-results/history/run-001.json")
    assert not _is_in_scope("kairix.egg-info/PKG-INFO")


def _recreate_retired_baseline(root: Path, gate_file: str, entry: str) -> None:
    """Write a file at the retired ``.architecture/baseline/`` location naming
    ``entry`` — the shape that used to grandfather it. PLA-472: it must have
    no effect on the verdict."""
    baseline = root / ".architecture" / "baseline" / gate_file
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(f"{entry}\n", encoding="utf-8")


def test_violation_fails_even_with_a_recreated_baseline_file(tmp_path: Path) -> None:
    """PLA-472: no grandfathering. A match in a file listed in a re-created
    ``no-private-infra-refs-files.txt`` is still reported.

    Sabotage proof (executed): re-adding the ``if rel in baseline: continue``
    skip to ``collect_violations`` drops the hit and this test goes red;
    restored → green.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("Use synthetic-test-vm-123 here.\n", encoding="utf-8")
    _recreate_retired_baseline(tmp_path, "no-private-infra-refs-files.txt", "docs/guide.md")

    violations = infra.collect_violations(tmp_path, ["docs/guide.md"], _compile_patterns(_SYNTHETIC_PATTERN_SOURCE))

    assert len(violations) == 1
    assert violations[0].startswith("docs/guide.md:1")
    assert not hasattr(infra, "_load_baseline")
