"""F94 detector tests — no runtime writes to system/OS paths.

The F94 detector (``scripts/checks/check_f94_no_system_path_writes.py``)
flags literal-target write calls — ``open("/etc/...", "w")``,
``Path("/etc/...").write_text(...)`` / ``.write_bytes(...)`` / ``.open("w")``
— to core OS locations (/etc, /opt, /usr, ...), so kairix stays
least-privilege on hardened / read-only-root VMs. Reads of system paths
(the read-only base config) and writes to the writable data dir
(/var/lib/kairix) / tmpfs (/run) are NOT violations.

Sabotage proof: the read-mode case + the /var (data-dir) case both assert
ZERO hits. Dropping the write-mode guard in ``_open_write_target`` flips
the read-mode assertion red; dropping ``/var`` out of the excluded set (or
adding it to ``_SYSTEM_PREFIXES``) flips the data-dir assertion red.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

import check_f94_no_system_path_writes as f94  # noqa: E402
from check_f94_no_system_path_writes import _is_exempt_path, _scan_file  # noqa: E402

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, name: str, content: str) -> Path:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return f


def test_open_write_to_etc_is_violation(tmp_path: Path) -> None:
    f = _write(tmp_path, "s.py", 'open("/etc/kairix/kairix.config.yaml", "w")\n')

    hits = _scan_file(f, "s.py")

    assert len(hits) == 1
    assert "write to system path" in hits[0]
    assert "s.py:1" in hits[0]


def test_open_read_of_etc_is_not_a_violation(tmp_path: Path) -> None:
    """Reading the read-only base config is fine — only writes are flagged."""
    f = _write(tmp_path, "s.py", 'open("/etc/kairix/kairix.config.yaml")\n')

    assert _scan_file(f, "s.py") == []


def test_path_write_text_to_opt_is_violation(tmp_path: Path) -> None:
    f = _write(tmp_path, "s.py", 'Path("/opt/kairix/state.json").write_text(payload)\n')

    hits = _scan_file(f, "s.py")

    assert len(hits) == 1
    assert "/opt/kairix/state.json" in hits[0]


def test_path_open_write_to_etc_is_violation(tmp_path: Path) -> None:
    f = _write(tmp_path, "s.py", 'Path("/etc/kairix/x").open("w")\n')

    assert len(_scan_file(f, "s.py")) == 1


def test_write_to_data_dir_under_var_is_not_a_violation(tmp_path: Path) -> None:
    """/var/lib/kairix is the writable, app-owned data dir — allowed."""
    f = _write(tmp_path, "s.py", 'Path("/var/lib/kairix/state.json").write_text(payload)\n')

    assert _scan_file(f, "s.py") == []


def test_write_to_run_secrets_tmpfs_is_not_a_violation(tmp_path: Path) -> None:
    """/run is tmpfs (the secrets mount) — writable, not a core OS location."""
    f = _write(tmp_path, "s.py", 'Path("/run/secrets/kairix.env").write_text(payload)\n')

    assert _scan_file(f, "s.py") == []


def test_variable_target_write_is_not_flagged(tmp_path: Path) -> None:
    """Literal-only scope: a write whose target is a variable (resolved via
    kairix.paths) is not statically flagged — the architecture routes those
    through the data dir; the regression this rule guards is the hardcode."""
    f = _write(
        tmp_path,
        "s.py",
        "target = paths.data_dir() / 'state.json'\ntarget.write_text(payload)\nopen(cfg_path, 'w')\n",
    )

    assert _scan_file(f, "s.py") == []


def test_install_tree_is_exempt_runtime_is_not(tmp_path: Path) -> None:
    """The system installer may write to /etc (privileged install); runtime
    production modules are in scope; out-of-tree files are skipped."""
    assert _is_exempt_path("kairix/install/systemd.py") is True
    assert _is_exempt_path("kairix/worker.py") is False
    assert _is_exempt_path("scripts/deploy.py") is True
    assert _is_exempt_path("kairix/core/factory.py") is False


def _recreate_retired_baseline(root: Path, gate_file: str, entry: str) -> None:
    """Write a file at the retired ``.architecture/baseline/`` location naming
    ``entry`` — the shape that used to grandfather it. PLA-472: it must have
    no effect on the verdict."""
    baseline = root / ".architecture" / "baseline" / gate_file
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(f"{entry}\n", encoding="utf-8")


def test_violation_fails_even_with_a_recreated_baseline_file(tmp_path: Path) -> None:
    """PLA-472: no grandfathering. A system-path write in a file listed in a
    re-created ``f94-files.txt`` is still reported.

    Sabotage proof (executed): re-adding the ``if rel in baseline`` partition
    to ``collect_violations`` drops the hit and this test goes red; restored →
    green.
    """
    (tmp_path / "kairix").mkdir()
    (tmp_path / "kairix" / "writer.py").write_text('open("/etc/kairix/x.yaml", "w")\n', encoding="utf-8")
    _recreate_retired_baseline(tmp_path, "f94-files.txt", "kairix/writer.py")

    violations = f94.collect_violations(tmp_path, ["kairix/writer.py"])

    assert violations == ["kairix/writer.py:1: write to system path '/etc/kairix/x.yaml'"]
    assert not hasattr(f94, "_load_baseline")
