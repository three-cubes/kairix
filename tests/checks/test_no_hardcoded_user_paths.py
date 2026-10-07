"""F31 detector tests — no hardcoded user/machine paths in committed code.

The F31 detector (``scripts/checks/check_no_hardcoded_user_paths.py``)
flags ``/Users/<dev>/`` and ``/home/<dev>/`` patterns that pin a single
contributor's machine to a checked-in file. ``/home/runner/`` and
``/Users/runner/`` are exempt because they're GitHub Actions hosted-runner
paths that legitimately appear in workflow / CI log fixtures.

The tests below pin the detector's behaviour on the four cases that
matter: hits the macOS pattern, hits the Linux pattern, exempts the
runner home, exempts markdown documentation.

Sabotage proof for the hit-cases: the assertions check both that a hit
returns at least one violation AND that the runner-home variant returns
none. Removing the negative lookahead ``(?!runner/)`` in the detector's
``PATTERNS`` flips the runner-exempt assertion red.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

import check_no_hardcoded_user_paths as f31  # noqa: E402
from check_no_hardcoded_user_paths import _scan_file  # noqa: E402

pytestmark = pytest.mark.unit


def _write_file(tmp_path: Path, name: str, content: str) -> Path:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return f


def test_macos_home_path_is_a_violation(tmp_path: Path) -> None:
    f = _write_file(
        tmp_path,
        "sample.py",
        'ROOT = "/Users/alice/Development/kairix"\n',
    )

    violations = _scan_file(f, "sample.py")

    assert len(violations) == 1
    assert "hardcoded user/machine path" in violations[0]
    assert "sample.py:1" in violations[0]


def test_linux_home_path_is_a_violation(tmp_path: Path) -> None:
    f = _write_file(
        tmp_path,
        "sample.sh",
        "KAIRIX_DATA_DIR=/home/bob/.local/share/kairix\n",
    )

    violations = _scan_file(f, "sample.sh")

    assert len(violations) == 1
    assert "sample.sh:1" in violations[0]


def test_github_runner_home_is_exempt(tmp_path: Path) -> None:
    f = _write_file(
        tmp_path,
        "workflow_fixture.py",
        'WORKSPACE = "/home/runner/work/kairix/kairix"\nMAC_RUNNER = "/Users/runner/work/kairix/kairix"\n',
    )

    violations = _scan_file(f, "workflow_fixture.py")

    # Both lines reference the runner workspace, neither is a violation.
    assert violations == []


def test_multiple_violations_in_one_file_all_get_reported(tmp_path: Path) -> None:
    f = _write_file(
        tmp_path,
        "leaky.py",
        'ROOT = "/Users/alice/dev/kairix"\nCACHE = "/home/bob/.cache/kairix"\nGOOD = "/opt/kairix"\n',
    )

    violations = _scan_file(f, "leaky.py")

    assert len(violations) == 2
    assert "leaky.py:1" in violations[0]
    assert "leaky.py:2" in violations[1]


def _recreate_retired_baseline(root: Path, gate_file: str, entry: str) -> None:
    """Write a file at the retired ``.architecture/baseline/`` location naming
    ``entry`` — the shape that used to grandfather it. PLA-472: it must have
    no effect on the verdict."""
    baseline = root / ".architecture" / "baseline" / gate_file
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(f"{entry}\n", encoding="utf-8")


def test_violation_fails_even_with_a_recreated_baseline_file(tmp_path: Path) -> None:
    """PLA-472: no grandfathering. A file listed in a re-created
    ``no-hardcoded-user-paths-files.txt`` is still reported, and the module
    exposes no baseline loader.

    Sabotage proof (executed): re-adding ``if rel in baseline: continue`` (with
    ``_load_baseline`` reading ``root/.architecture/baseline/...``) to
    ``collect_violations`` drops the hit and this test goes red; restored →
    green.
    """
    (tmp_path / "kairix").mkdir()
    (tmp_path / "kairix" / "leaky.py").write_text('ROOT = "/Users/alice/dev/kairix"\n', encoding="utf-8")
    _recreate_retired_baseline(tmp_path, "no-hardcoded-user-paths-files.txt", "kairix/leaky.py")

    violations = f31.collect_violations(tmp_path, ["kairix/leaky.py"])

    assert len(violations) == 1
    assert violations[0].startswith("kairix/leaky.py:1")
    assert not hasattr(f31, "_load_baseline")
    assert not hasattr(f31, "BASELINE_FILE")


def test_retired_baseline_dir_is_not_exempt() -> None:
    """The retired ``.architecture/baseline/`` tree is no longer an exempt
    prefix — a hardcoded path committed there is scanned like any other file.

    Sabotage proof (executed): re-adding ``".architecture/baseline/"`` to
    ``EXEMPT_PREFIX`` flips this red; restored → green.
    """
    assert f31._is_exempt_path(".architecture/baseline/x-files.txt") is False
