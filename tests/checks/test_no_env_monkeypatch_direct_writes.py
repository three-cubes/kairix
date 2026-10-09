"""F2 static-half tests — the common ``KAIRIX_*`` env-write spellings.

``scripts/checks/check_no_env_monkeypatch.py`` is the fast pre-commit layer:
it matches the direct spellings with a literal ``KAIRIX_`` key. Every other
spelling (computed keys, aliases, ``patch.dict``, ``os.putenv``, ...) is the
runtime guard's job (``tests/fixtures/process_state_guard.py``, proven in
``tests/test_process_state_guard.py``), so it is deliberately not pinned here.

Sabotage proof (executed): make ``_shape`` return ``None`` → every positive
case fails; drop the ``exempt`` filter → the baseline-block case fails;
restored.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

from check_no_env_monkeypatch import (  # noqa: E402 — see _CHECKS_DIR sys.path insert above
    F2,
    REMEDIATION,
    file_has_env_monkeypatch,
    file_violations,
)

pytestmark = pytest.mark.unit

_HEADER = "import os\nfrom os import environ\n\n\n"


def _violations(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "test_sample.py"
    path.write_text(_HEADER + body + "\n", encoding="utf-8")
    return file_violations(path)


@pytest.mark.parametrize(
    ("statement", "shape"),
    [
        ('monkeypatch.setenv("KAIRIX_DB_PATH", "x")', "setenv(KAIRIX_*)"),
        ('mp.delenv("KAIRIX_DB_PATH", raising=False)', "delenv(KAIRIX_*)"),
        ('os.environ["KAIRIX_DB_PATH"] = "x"', "assign os.environ[KAIRIX_*]"),
        ('os.environ["KAIRIX_DB_PATH"] += "x"', "assign os.environ[KAIRIX_*]"),
        ('environ["KAIRIX_DB_PATH"] = "x"', "assign os.environ[KAIRIX_*]"),
        ('del os.environ["KAIRIX_DB_PATH"]', "del os.environ[KAIRIX_*]"),
        ('os.environ.pop("KAIRIX_DB_PATH", None)', "os.environ.pop(KAIRIX_*)"),
        ('os.environ.setdefault("KAIRIX_DB_PATH", "x")', "os.environ.setdefault(KAIRIX_*)"),
    ],
)
def test_direct_kairix_env_write_is_flagged(tmp_path: Path, statement: str, shape: str) -> None:
    assert _violations(tmp_path, statement) == [f"5: {shape}"]


@pytest.mark.parametrize(
    "statement",
    [
        'value = os.environ.get("KAIRIX_DB_PATH")',
        'value = os.environ["KAIRIX_DB_PATH"]',
        'monkeypatch.setenv("XDG_CONFIG_HOME", "x")',
        'os.environ["PATH"] = "x"',
        'env = dict(os.environ)\nenv["KAIRIX_DB_PATH"] = "x"',
        'subprocess.run(cmd, env={**os.environ, "KAIRIX_DB_PATH": "x"})',
    ],
)
def test_reads_copies_and_other_keys_are_not_flagged(tmp_path: Path, statement: str) -> None:
    assert _violations(tmp_path, statement) == []


def _file_violations(tmp_path: Path, source: str) -> list[str]:
    path = tmp_path / "test_sample.py"
    path.write_text(source, encoding="utf-8")
    return file_violations(path)


def test_bare_environ_counts_only_when_imported_from_os(tmp_path: Path) -> None:
    """``environ[...] =`` is the process env only via ``from os import
    environ [as e]``; without that import a bare ``environ`` is someone's dict.

    Sabotage proof (executed): match every bare name ``environ`` again → the
    no-import case is flagged and this fails; restored.
    """
    imported = 'from os import environ as env\n\n\ndef test_x():\n    env["KAIRIX_DB_PATH"] = "x"\n'
    assert _file_violations(tmp_path, imported) == ["5: assign os.environ[KAIRIX_*]"]
    not_imported = 'environ = {}\n\n\ndef test_x():\n    environ["KAIRIX_DB_PATH"] = "x"\n'
    assert _file_violations(tmp_path, not_imported) == []


@pytest.mark.parametrize(
    "function",
    [
        'def test_x():\n    environ = {}\n    environ["KAIRIX_DB_PATH"] = "x"\n',
        'def test_x(environ):\n    environ["KAIRIX_DB_PATH"] = "x"\n',
    ],
    ids=["local-assignment", "parameter"],
)
def test_locally_rebound_environ_is_not_flagged(tmp_path: Path, function: str) -> None:
    """Even with ``from os import environ`` in the file, a function that
    rebinds the name (an assignment or a parameter) writes its own mapping.

    Sabotage proof (executed): make ``_rebound_locally`` return ``False`` →
    both cases are flagged and this fails; restored.
    """
    assert _file_violations(tmp_path, "from os import environ\n\n\n" + function) == []


_BASELINE = """
with allow_baseline_writes():
    monkeypatch.setenv("KAIRIX_CONNECT_DISABLE_BROWSER", "1")
"""


def test_baseline_block_is_exempt_only_in_the_root_conftest(tmp_path: Path) -> None:
    """Writes inside ``with allow_baseline_writes():`` are the session
    baseline's, exempt in ``tests/conftest.py`` — and the block itself is a
    violation anywhere else (it is the runtime guard's one exemption)."""
    assert _violations(tmp_path, _BASELINE) == ["6: allow_baseline_writes() outside tests/conftest.py"]
    assert file_violations(_REPO_ROOT / "tests" / "conftest.py") == []


def test_bool_surface_agrees_with_violation_list(tmp_path: Path) -> None:
    path = tmp_path / "test_sample.py"
    path.write_text('import os\nos.environ["KAIRIX_DB_PATH"] = "x"\n', encoding="utf-8")
    assert file_has_env_monkeypatch(path) is True
    path.write_text('import os\nos.environ["XDG_CONFIG_HOME"] = "x"\n', encoding="utf-8")
    assert file_has_env_monkeypatch(path) is False


def test_remediation_is_f21_actionable() -> None:
    assert REMEDIATION.startswith("KAIRIX_* process-env write found in a test. Refactor to")
    for marker in ("fix:", "next:", "run:", "Pass example:", "Forbidden example:"):
        assert marker in REMEDIATION


def test_rule_gate_reports_line_keys_and_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``F2(repo_root=...).run()`` scans ``tests/`` under the root, prints each
    ``path:line: shape`` and returns 1; a clean tree returns 0."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_bad.py").write_text('import os\nos.environ["KAIRIX_DB_PATH"] = "x"\n', encoding="utf-8")
    assert F2(repo_root=tmp_path).run() == 1
    assert "tests/test_bad.py:2: assign os.environ[KAIRIX_*]" in capsys.readouterr().out
    (tests_dir / "test_bad.py").write_text('import os\nos.environ["PATH"] = "x"\n', encoding="utf-8")
    assert F2(repo_root=tmp_path).run() == 0
