"""Unit tests for ``scripts/update_reflib_history.py``.

Covers the idempotent reflib-history archive script that the
``reflib-history-capture.yml`` workflow calls per release (#271).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit


_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "update_reflib_history.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("update_reflib_history", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["update_reflib_history"] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_script()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sample_result(weighted: float = 0.901, generated: str = "2026-05-02") -> dict:
    return {
        "meta": {
            "name": "reflib-gold-v3",
            "system": "hybrid",
            "generated": generated,
            "suite": "suites/reflib-gold-v3.yaml",
        },
        "summary": {
            "weighted_total": weighted,
            "ndcg_at_10": 0.990,
            "hit_rate_at_5": 0.990,
            "mrr_at_10": 0.917,
            "category_scores": {
                "recall": 0.929,
                "temporal": 0.702,
                "entity": 0.860,
                "conceptual": 0.979,
                "multi_hop": 1.000,
                "procedural": 1.038,
            },
        },
    }


_INDEX_SCAFFOLD = (
    "# Reflib benchmark history\n\n"
    "## Releases\n\n"
    "| tag | date | weighted_total | NDCG@10 | Hit@5 |"
    " conceptual | recall | temporal | entity | multi_hop | procedural |\n"
    "|-----|------|----------------|---------|-------|"
    "------------|--------|----------|--------|-----------|------------|\n"
)


@pytest.fixture
def history_dir(tmp_path: Path) -> Path:
    d = tmp_path / "history"
    d.mkdir()
    (d / "INDEX.md").write_text(_INDEX_SCAFFOLD, encoding="utf-8")
    return d


def _write_result(tmp_path: Path, payload: dict, name: str = "result.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_validate_tag_accepts_three_part_calver() -> None:
    _mod.validate_tag("v2026.5.10")  # does not raise


def test_validate_tag_accepts_four_part_calver() -> None:
    _mod.validate_tag("v2026.5.10.1")


def test_validate_tag_rejects_non_calver() -> None:
    with pytest.raises(ValueError, match="CalVer"):
        _mod.validate_tag("release-1")


def test_validate_tag_rejects_missing_v_prefix() -> None:
    with pytest.raises(ValueError):
        _mod.validate_tag("2026.5.10")


def test_archive_filename_combines_tag_and_date() -> None:
    assert _mod.archive_filename("v2026.5.10.1", "2026-05-02") == "v2026.5.10.1-2026-05-02.json"


def test_format_index_row_renders_all_columns() -> None:
    row = _mod.format_index_row("v2026.5.10.1", "2026-05-02", _sample_result()["summary"])
    assert "[v2026.5.10.1](v2026.5.10.1-2026-05-02.json)" in row
    assert "2026-05-02" in row
    assert "0.901" in row  # weighted_total
    assert "0.990" in row  # ndcg / hit5
    # Category cells in column order
    for score in ("0.979", "0.929", "0.702", "0.860", "1.000", "1.038"):
        assert score in row


def test_format_index_row_renders_dash_for_missing_metrics() -> None:
    summary = {"weighted_total": 0.5}  # no ndcg, no categories
    row = _mod.format_index_row("v2026.5.10", "2026-05-02", summary)
    # NDCG, Hit@5 and all six category cells should render as ``-``
    # (7 dashes total — one per missing metric).
    assert row.count("| - ") >= 6


def test_index_has_tag_finds_existing_link() -> None:
    text = "| [v2026.5.10](v2026.5.10-2026-05-02.json) | 2026-05-02 | 0.9 |\n"
    assert _mod.index_has_tag(text, "v2026.5.10")


def test_index_has_tag_returns_false_for_absent_tag() -> None:
    text = "| [v2026.5.10](x.json) |\n"
    assert not _mod.index_has_tag(text, "v2026.5.11")


def test_index_has_tag_ignores_substring_collisions() -> None:
    # ``v2026.5.1`` is a prefix of ``v2026.5.10`` but must not match.
    text = "| [v2026.5.10](x.json) |\n"
    assert not _mod.index_has_tag(text, "v2026.5.1")


def test_append_row_preserves_single_trailing_newline() -> None:
    result = _mod.append_row("a\nb\n", "c")
    assert result == "a\nb\nc\n"


# ---------------------------------------------------------------------------
# update_history — orchestrator tests
# ---------------------------------------------------------------------------


def test_update_history_writes_archive_and_appends_row(tmp_path: Path, history_dir: Path) -> None:
    result = _write_result(tmp_path, _sample_result())

    archive_path, appended = _mod.update_history(
        result_json_path=result,
        tag="v2026.5.10.1",
        history_dir=history_dir,
    )

    assert appended is True
    assert archive_path.exists()
    assert archive_path.name == "v2026.5.10.1-2026-05-02.json"
    # Archived JSON matches input (modulo formatting)
    assert json.loads(archive_path.read_text())["summary"]["weighted_total"] == 0.901
    # Index now contains the tag row
    index_text = (history_dir / "INDEX.md").read_text()
    assert "[v2026.5.10.1](v2026.5.10.1-2026-05-02.json)" in index_text


def test_update_history_is_idempotent_on_rerun(tmp_path: Path, history_dir: Path) -> None:
    result = _write_result(tmp_path, _sample_result())

    _mod.update_history(result, "v2026.5.10.1", history_dir)
    archive_path, appended = _mod.update_history(result, "v2026.5.10.1", history_dir)

    assert appended is False
    # Row appears exactly once
    index_text = (history_dir / "INDEX.md").read_text()
    assert index_text.count("[v2026.5.10.1]") == 1
    assert archive_path.exists()


def test_update_history_rejects_tag_reuse_with_different_payload(tmp_path: Path, history_dir: Path) -> None:
    first = _write_result(tmp_path, _sample_result(weighted=0.901), name="first.json")
    second = _write_result(tmp_path, _sample_result(weighted=0.555), name="second.json")

    _mod.update_history(first, "v2026.5.10.1", history_dir)

    with pytest.raises(ValueError, match="append-only"):
        _mod.update_history(second, "v2026.5.10.1", history_dir)


def test_update_history_uses_date_override_over_generated(tmp_path: Path, history_dir: Path) -> None:
    result = _write_result(tmp_path, _sample_result(generated="2026-05-02"))

    archive_path, _ = _mod.update_history(result, "v2026.5.10.1", history_dir, date_override="2026-12-31")

    assert archive_path.name == "v2026.5.10.1-2026-12-31.json"


def test_update_history_falls_back_to_today_when_no_generated(tmp_path: Path, history_dir: Path) -> None:
    payload = _sample_result()
    payload["meta"].pop("generated")
    result = _write_result(tmp_path, payload)

    archive_path, _ = _mod.update_history(result, "v2026.5.10.1", history_dir)

    # Filename should be vTAG-YYYY-MM-DD.json — exact YMD depends on test
    # date, so just check the shape.
    assert archive_path.name.startswith("v2026.5.10.1-")
    assert archive_path.name.endswith(".json")


def test_update_history_raises_on_missing_result_file(tmp_path: Path, history_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _mod.update_history(tmp_path / "missing.json", "v2026.5.10.1", history_dir)


def test_update_history_raises_on_malformed_json(tmp_path: Path, history_dir: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json {{{", encoding="utf-8")

    with pytest.raises(ValueError, match="malformed"):
        _mod.update_history(bad, "v2026.5.10.1", history_dir)


def test_update_history_raises_on_non_object_json(tmp_path: Path, history_dir: Path) -> None:
    bad = tmp_path / "array.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(ValueError, match="not an object"):
        _mod.update_history(bad, "v2026.5.10.1", history_dir)


def test_update_history_raises_when_index_missing(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    # No INDEX.md
    result = _write_result(tmp_path, _sample_result())

    with pytest.raises(FileNotFoundError, match=r"INDEX\.md"):
        _mod.update_history(result, "v2026.5.10.1", history_dir)


def test_update_history_rejects_invalid_tag(tmp_path: Path, history_dir: Path) -> None:
    result = _write_result(tmp_path, _sample_result())

    with pytest.raises(ValueError, match="CalVer"):
        _mod.update_history(result, "not-a-tag", history_dir)


# ---------------------------------------------------------------------------
# CLI entry-point tests
# ---------------------------------------------------------------------------


def test_main_returns_zero_on_first_run(tmp_path: Path, history_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = _write_result(tmp_path, _sample_result())

    rc = _mod.main([str(result), "--tag", "v2026.5.10.1", "--history-dir", str(history_dir)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "appended" in out
    assert "v2026.5.10.1" in out


def test_main_returns_zero_and_reports_no_op_on_rerun(
    tmp_path: Path, history_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _write_result(tmp_path, _sample_result())
    argv = [str(result), "--tag", "v2026.5.10.1", "--history-dir", str(history_dir)]

    _mod.main(argv)
    capsys.readouterr()  # discard first-run output
    rc = _mod.main(argv)

    assert rc == 0
    assert "no-op" in capsys.readouterr().out


def test_main_returns_one_when_input_missing(
    tmp_path: Path, history_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _mod.main(
        [
            str(tmp_path / "missing.json"),
            "--tag",
            "v2026.5.10.1",
            "--history-dir",
            str(history_dir),
        ]
    )

    assert rc == 1
    assert "error" in capsys.readouterr().err.lower()


def test_main_returns_one_when_tag_invalid(
    tmp_path: Path, history_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _write_result(tmp_path, _sample_result())

    rc = _mod.main([str(result), "--tag", "bad", "--history-dir", str(history_dir)])

    assert rc == 1
    assert "calver" in capsys.readouterr().err.lower()
