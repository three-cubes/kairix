"""
Tests for scripts/audit-date-formats.py — date format audit (TMP-5).

Uses importlib for the hyphenated script filename.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "audit-date-formats.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_date_formats", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]  # importlib Loader protocol omits exec_module in stubs
    return mod


_mod = _load_script()
classify_date_value = _mod.classify_date_value
audit_file = _mod.audit_file
run_audit = _mod.run_audit


# ---------------------------------------------------------------------------
# classify_date_value
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_iso_date_classified_correctly() -> None:
    assert classify_date_value("2026-04-10") == "iso"


@pytest.mark.unit
def test_quoted_iso_date_classified_correctly() -> None:
    assert classify_date_value('"2026-04-10"') == "iso"


@pytest.mark.unit
def test_datetime_string_classified_as_datetime() -> None:
    assert classify_date_value("2026-04-10T09:30") == "datetime"
    assert classify_date_value("2026-04-10 09:30") == "datetime"


@pytest.mark.unit
def test_non_iso_classified_correctly() -> None:
    assert classify_date_value("10 April 2026") == "non_iso"
    assert classify_date_value("April 10, 2026") == "non_iso"
    assert classify_date_value("10/04/2026") == "non_iso"


@pytest.mark.unit
def test_empty_value_classified_as_absent() -> None:
    assert classify_date_value("") == "absent"


# ---------------------------------------------------------------------------
# audit_file
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_iso_frontmatter_detected(tmp_path: Path) -> None:
    f = tmp_path / "test.md"
    f.write_text("---\ndate: 2026-04-10\ntitle: Test\n---\n# Body\n")
    cls, raw = audit_file(f)
    assert cls == "iso"
    assert raw == "2026-04-10"


@pytest.mark.integration
def test_non_iso_frontmatter_detected(tmp_path: Path) -> None:
    f = tmp_path / "test.md"
    f.write_text("---\ndate: 10 April 2026\n---\n# Body\n")
    cls, raw = audit_file(f)
    assert cls == "non_iso"
    assert raw == "10 April 2026"


@pytest.mark.integration
def test_absent_date_no_frontmatter(tmp_path: Path) -> None:
    f = tmp_path / "test.md"
    f.write_text("# No frontmatter\nJust body text.\n")
    cls, raw = audit_file(f)
    assert cls == "absent"
    assert raw == ""


@pytest.mark.integration
def test_absent_date_frontmatter_no_date_field(tmp_path: Path) -> None:
    f = tmp_path / "test.md"
    f.write_text("---\ntitle: No date\ntags: [test]\n---\n# Body\n")
    cls, _raw = audit_file(f)
    assert cls == "absent"


@pytest.mark.integration
def test_datetime_frontmatter_detected(tmp_path: Path) -> None:
    f = tmp_path / "test.md"
    f.write_text("---\ncreated: 2026-04-10T09:30:00\n---\n# Body\n")
    cls, _raw = audit_file(f)
    assert cls == "datetime"


# ---------------------------------------------------------------------------
# run_audit
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_run_audit_counts_correct(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "iso.md").write_text("---\ndate: 2026-04-10\n---\n# Test\n")
    (vault / "non_iso.md").write_text("---\ndate: 10 April 2026\n---\n# Test\n")
    (vault / "absent.md").write_text("# No frontmatter\n")

    result = run_audit(vault)

    assert result["classification_counts"]["iso"] == 1
    assert result["classification_counts"]["non_iso"] == 1
    assert result["classification_counts"]["absent"] == 1
    assert result["total_files"] == 3


@pytest.mark.integration
def test_run_audit_json_structure(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "test.md").write_text("---\ndate: 2026-01-01\n---\n# Test\n")

    result = run_audit(vault)

    assert "total_files" in result
    assert "classification_counts" in result
    assert "by_category" in result
    assert "non_iso_top_values" in result
    assert "extractable_pct" in result


@pytest.mark.integration
def test_extractable_pct_correct(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "iso.md").write_text("---\ndate: 2026-04-10\n---\n# Test\n")
    (vault / "dt.md").write_text("---\ndate: 2026-04-10T10:00\n---\n# Test\n")
    (vault / "bad.md").write_text("---\ndate: not a date\n---\n# Test\n")
    (vault / "no.md").write_text("# no date\n")

    result = run_audit(vault)
    assert result["extractable_pct"] == pytest.approx(50.0)
