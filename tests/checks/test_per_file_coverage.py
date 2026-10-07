"""F7 / F9 per-file coverage floor — no grandfathering (PLA-472).

``scripts/checks/check_per_file_coverage.py`` fails on ANY ``kairix/*``
file below the 90% floor. The per-file baselines that used to grandfather
files below the floor were retired; these tests pin that a file named in a
re-created baseline file still fails the gate.
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
import check_per_file_coverage as f7  # noqa: E402

pytestmark = pytest.mark.unit


def _coverage_xml(tmp_path: Path, rates: dict[str, float]) -> Path:
    classes = "".join(f'<class filename="{name}" line-rate="{rate}"/>' for name, rate in rates.items())
    xml = (
        '<?xml version="1.0" ?><coverage><sources><source>kairix</source></sources>'
        f"<packages><package><classes>{classes}</classes></package></packages></coverage>"
    )
    path = tmp_path / "coverage.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def test_file_below_floor_fails_even_with_a_recreated_baseline_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file below the floor fails the gate even when a file at the retired
    baseline location names it.

    Sabotage proof (executed): restoring a skip of files listed in
    ``.architecture/baseline/<gate>-files.txt`` before calling ``gate()``
    makes ``main`` return 0 here and this test goes red; restored → green.
    """
    report = _coverage_xml(tmp_path, {"legacy.py": 0.42, "good.py": 0.99})
    baseline = tmp_path / ".architecture" / "baseline" / "per-file-coverage-floor-files.txt"
    baseline.parent.mkdir(parents=True)
    baseline.write_text("kairix/legacy.py\n", encoding="utf-8")

    rc = f7.main(["check_per_file_coverage.py", str(report)])

    out = capsys.readouterr().out
    assert rc == 1
    assert "kairix/legacy.py" in out
    assert "kairix/good.py" not in out
    assert not hasattr(f7, "_load_baseline")


def test_all_files_at_or_above_floor_pass(tmp_path: Path) -> None:
    """Every file ≥ 90% → clean (``a.py`` sits exactly on the floor).

    Sabotage proof (executed): changing ``rate * 100 < FLOOR`` to ``<=``
    flags ``a.py`` and flips this red; restored → green.
    """
    report = _coverage_xml(tmp_path, {"a.py": 0.90, "b.py": 1.0})
    assert f7.main(["check_per_file_coverage.py", str(report)]) == 0
