"""F2 detector tests — direct ``os.environ`` writes of ``KAIRIX_*`` keys.

The F2 detector (``scripts/checks/check_no_env_monkeypatch.py``) originally
caught only ``monkeypatch.setenv/setattr/delenv("KAIRIX_*")``. A paydown found
tests evading it by writing ``os.environ`` directly — the pytest-bdd step that
did ``os.environ["KAIRIX_DB_PATH"] = ...`` leaked the value into every later
test in the process, because a raw write has no auto-undo. This module pins
each direct-write shape, the key-resolution hops (variable, loop, alias), the
negatives (reads, non-KAIRIX keys), and the two structurally-recognised
process-boundary shapes (conftest session baseline, snapshot-restore
teardown).

Pattern: write a small source string under ``tmp_path``, run it through the
public ``file_violations`` surface, assert on the reported shapes.

Sabotage proofs (executed — mutate the detector, confirm red, restore, green;
see the per-test docstrings for the exact mutation):
  * every positive test fails when ``_statement_shapes`` returns ``[]``;
  * each structural-recognition test fails when ``_is_recognised_boundary``
    returns ``False``.
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
    REMEDIATION,
    file_has_env_monkeypatch,
    file_violations,
)

pytestmark = pytest.mark.unit


def _violations(tmp_path: Path, source: str, name: str = "test_sample.py") -> list[str]:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return file_violations(path)


# ---------------------------------------------------------------------------
# Direct os.environ write shapes (positives).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "shape"),
    [
        ('os.environ["KAIRIX_DB_PATH"] = "x"', "assign os.environ[KAIRIX_*]"),
        ('os.environ["KAIRIX_DB_PATH"] += "x"', "assign os.environ[KAIRIX_*]"),
        ('del os.environ["KAIRIX_DB_PATH"]', "del os.environ[KAIRIX_*]"),
        ('os.environ.pop("KAIRIX_DB_PATH", None)', "os.environ.pop(KAIRIX_*)"),
        ('os.environ.setdefault("KAIRIX_DB_PATH", "x")', "os.environ.setdefault(KAIRIX_*)"),
        ('os.environ.update({"KAIRIX_DB_PATH": "x"})', "os.environ.update(<may carry KAIRIX_*>)"),
        ('os.environ.update(KAIRIX_DB_PATH="x")', "os.environ.update(<may carry KAIRIX_*>)"),
        ("os.environ.update(overrides)", "os.environ.update(<may carry KAIRIX_*>)"),
        ('patch.dict(os.environ, {"KAIRIX_DB_PATH": "x"})', "patch.dict(os.environ, <KAIRIX_*>)"),
        ('mock.patch.dict(os.environ, {"KAIRIX_DB_PATH": "x"})', "patch.dict(os.environ, <KAIRIX_*>)"),
        ('monkeypatch.setitem(os.environ, "KAIRIX_DB_PATH", "x")', "monkeypatch.setitem(os.environ, KAIRIX_*)"),
        ('monkeypatch.delitem(os.environ, "KAIRIX_DB_PATH")', "monkeypatch.delitem(os.environ, KAIRIX_*)"),
        ('os.environ[f"KAIRIX_{suffix}"] = "x"', "assign os.environ[KAIRIX_*]"),
    ],
)
def test_direct_environ_write_of_kairix_key_is_flagged(tmp_path: Path, statement: str, shape: str) -> None:
    """Every direct-write shape on a KAIRIX_* key is a violation.

    Sabotage proof (executed): make ``_statement_shapes`` return ``[]`` →
    every parametrised case reports no violation and fails; restored.
    """
    header = "import os\nfrom unittest import mock\nfrom unittest.mock import patch\n\n\n"
    src = f"{header}def test_x(monkeypatch):\n    {statement}\n"
    assert _violations(tmp_path, src) == [f"7: {shape}"]


def test_patch_dict_as_decorator_and_context_manager_is_flagged(tmp_path: Path) -> None:
    """``patch.dict`` is caught whether used as a decorator or a ``with``."""
    src = """
import os
from unittest.mock import patch


@patch.dict(os.environ, {"KAIRIX_MAX_CONCURRENCY": "3"})
def test_a():
    pass


def test_b():
    with patch.dict(os.environ, {"KAIRIX_MAX_CONCURRENCY": "3"}):
        pass
"""
    assert _violations(tmp_path, src) == [
        "6: patch.dict(os.environ, <KAIRIX_*>)",
        "12: patch.dict(os.environ, <KAIRIX_*>)",
    ]


# ---------------------------------------------------------------------------
# Key resolution — the literal moved one or more hops away.
# ---------------------------------------------------------------------------


def test_key_held_in_a_variable_is_flagged(tmp_path: Path) -> None:
    """``var = "KAIRIX_X"; os.environ.pop(var)`` — the evasion this rule closes.

    Sabotage proof (executed): make ``tainted_names`` return ``set()`` →
    the variable key resolves as non-KAIRIX and the assertion fails; restored.
    """
    src = """
import os


def test_x():
    var = "KAIRIX_BOOTSTRAP_VAR"
    os.environ.pop(var, None)
"""
    assert _violations(tmp_path, src) == ["7: os.environ.pop(KAIRIX_*)"]


def test_key_from_a_loop_over_a_module_constant_is_flagged(tmp_path: Path) -> None:
    """Taint flows constant -> loop target -> comprehension -> restore loop."""
    src = """
import os

_SECRETS = ("KAIRIX_PROVIDER_LLM_API_KEY", "KAIRIX_NEO4J_PASSWORD")


def test_x():
    saved = {k: os.environ.pop(k, None) for k in _SECRETS}
    for key, value in saved.items():
        os.environ[key] = value
"""
    assert _violations(tmp_path, src) == [
        "8: os.environ.pop(KAIRIX_*)",
        "10: assign os.environ[KAIRIX_*]",
    ]


def test_monkeypatch_delenv_with_a_variable_key_is_flagged(tmp_path: Path) -> None:
    """The original monkeypatch shapes now resolve variable keys too."""
    src = """
def _names():
    return ("KAIRIX_SECRETS_FILE", "KAIRIX_KV_NAME")


def test_x(monkeypatch):
    for var in _names():
        monkeypatch.delenv(var, raising=False)
"""
    assert _violations(tmp_path, src) == ["8: monkeypatch.delenv(KAIRIX_*)"]


@pytest.mark.parametrize(
    "header",
    ["import os as _os\n", "from os import environ\n"],
)
def test_aliased_environ_receivers_are_flagged(tmp_path: Path, header: str) -> None:
    """``import os as _os`` and ``from os import environ`` receivers are resolved."""
    receiver = "_os.environ" if "_os" in header else "environ"
    src = f'{header}\n\ndef test_x():\n    {receiver}["KAIRIX_DB_PATH"] = "x"\n'
    violations = _violations(tmp_path, src)
    assert len(violations) == 1
    assert violations[0].endswith("assign os.environ[KAIRIX_*]")


@pytest.mark.parametrize(
    ("statement", "shape"),
    [
        ('b["KAIRIX_DB_PATH"] = "x"', "assign os.environ[KAIRIX_*]"),
        ('del b["KAIRIX_DB_PATH"]', "del os.environ[KAIRIX_*]"),
        ('b.pop("KAIRIX_DB_PATH", None)', "os.environ.pop(KAIRIX_*)"),
        ('b.setdefault("KAIRIX_DB_PATH", "x")', "os.environ.setdefault(KAIRIX_*)"),
        ('b.update({"KAIRIX_DB_PATH": "x"})', "os.environ.update(<may carry KAIRIX_*>)"),
        ('monkeypatch.setitem(b, "KAIRIX_DB_PATH", "x")', "monkeypatch.setitem(os.environ, KAIRIX_*)"),
    ],
)
def test_local_alias_chain_of_environ_is_flagged(tmp_path: Path, statement: str, shape: str) -> None:
    """Codex PR #814 thread: ``a = os.environ; b = a`` then a write through
    ``b`` mutates the live process env exactly like a direct write.

    Sabotage proof (executed): skip the alias fixpoint in ``ProcessMapping.resolve`` →
    every parametrised case reports clean; restored.
    """
    src = f"import os\n\n\ndef test_x(monkeypatch):\n    a = os.environ\n    b = a\n    {statement}\n"
    assert _violations(tmp_path, src) == [f"7: {shape}"]


@pytest.mark.parametrize("copy_expr", ["dict(os.environ)", "os.environ.copy()", "{**os.environ}"])
def test_write_to_a_copy_of_environ_is_not_flagged(tmp_path: Path, copy_expr: str) -> None:
    """A copied dict is a different object — writing it never touches the env."""
    src = f'import os\n\n\ndef test_x():\n    env = {copy_expr}\n    alias = env\n    alias["KAIRIX_DB_PATH"] = "x"\n'
    assert _violations(tmp_path, src) == []


# ---------------------------------------------------------------------------
# Negatives.
# ---------------------------------------------------------------------------


def test_reads_and_non_kairix_writes_are_not_flagged(tmp_path: Path) -> None:
    """Reads of KAIRIX_* keys and writes of non-KAIRIX keys stay allowed."""
    src = """
import os
from unittest.mock import patch


def test_x(monkeypatch):
    seen = os.environ["KAIRIX_KV_NAME"]
    other = os.environ.get("KAIRIX_DB_PATH")
    os.environ["XDG_CONFIG_HOME"] = "/tmp/x"
    os.environ.pop("CONNECTOR_GITHUB_APP_ID", None)
    os.environ.update({"PATH": "/bin"})
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/y")
    env = {"KAIRIX_DB_PATH": "/tmp/db"}
    with patch.dict(os.environ, {}, clear=False):
        pass
    return seen, other, env
"""
    assert _violations(tmp_path, src) == []


# ---------------------------------------------------------------------------
# Structural recognition 1 — conftest session baseline.
# ---------------------------------------------------------------------------

_SESSION_BASELINE = """
import pytest


@pytest.fixture(scope="session", autouse=True)
def _hermetic(tmp_path_factory):
    monkeypatch = pytest.MonkeyPatch()
    for name in ("KAIRIX_DATA_DIR", "KAIRIX_DB_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAIRIX_CONNECT_DISABLE_BROWSER", "1")
    yield
    monkeypatch.undo()
"""


def test_session_autouse_fixture_in_conftest_is_recognised(tmp_path: Path) -> None:
    """The once-per-run hermetic baseline in a conftest.py is not a violation.

    Sabotage proof (executed): make ``_is_recognised_boundary`` return
    ``False`` → both baseline writes are reported and this fails; restored.
    """
    assert _violations(tmp_path, _SESSION_BASELINE, name="conftest.py") == []


def test_session_baseline_shape_outside_conftest_is_flagged(tmp_path: Path) -> None:
    """The same fixture in an ordinary test module is NOT recognised."""
    assert _violations(tmp_path, _SESSION_BASELINE, name="test_sample.py") == [
        "9: monkeypatch.delenv(KAIRIX_*)",
        "10: monkeypatch.setenv(KAIRIX_*)",
    ]


@pytest.mark.parametrize(
    "decorator",
    [
        "@pytest.fixture(autouse=True)",
        '@pytest.fixture(scope="session")',
        '@pytest.fixture(scope="module", autouse=True)',
    ],
)
def test_conftest_fixture_without_session_autouse_is_flagged(tmp_path: Path, decorator: str) -> None:
    """Only ``scope="session", autouse=True`` together is the baseline shape."""
    src = _SESSION_BASELINE.replace('@pytest.fixture(scope="session", autouse=True)', decorator)
    assert len(_violations(tmp_path, src, name="conftest.py")) == 2


# ---------------------------------------------------------------------------
# Structural recognition 2 — snapshot-restore teardown.
# ---------------------------------------------------------------------------

_SNAPSHOT_RESTORE = """
import os

import pytest


@pytest.fixture
def _restored_environ():
    snapshot = {snapshot_expr}
    yield
    os.environ.clear()
    os.environ.update(snapshot)
"""


@pytest.mark.parametrize("snapshot_expr", ["dict(os.environ)", "os.environ.copy()", "{**os.environ}"])
def test_snapshot_restore_teardown_is_recognised(tmp_path: Path, snapshot_expr: str) -> None:
    """Restoring a pre-yield ``os.environ`` snapshot after the yield is allowed.

    Sabotage proof (executed): make ``_is_recognised_boundary`` return
    ``False`` → the ``update(snapshot)`` is reported and this fails;
    restored.
    """
    src = _SNAPSHOT_RESTORE.replace("{snapshot_expr}", snapshot_expr)
    assert _violations(tmp_path, src) == []


def test_write_before_the_yield_of_a_snapshotting_fixture_is_flagged(tmp_path: Path) -> None:
    """Seeding a KAIRIX_* value before the yield is still a violation."""
    src = """
import os

import pytest


@pytest.fixture
def _seeded():
    snapshot = dict(os.environ)
    os.environ["KAIRIX_DB_PATH"] = "/tmp/db"
    yield
    os.environ.clear()
    os.environ.update(snapshot)
"""
    assert _violations(tmp_path, src) == ["10: assign os.environ[KAIRIX_*]"]


def test_teardown_write_not_from_the_snapshot_is_flagged(tmp_path: Path) -> None:
    """After the yield, only a write that restores FROM the snapshot is allowed."""
    src = """
import os

import pytest


@pytest.fixture
def _leaky():
    snapshot = dict(os.environ)
    yield
    os.environ.update(snapshot)
    os.environ["KAIRIX_DB_PATH"] = "/tmp/leak"
"""
    assert _violations(tmp_path, src) == ["12: assign os.environ[KAIRIX_*]"]


def test_restore_without_a_snapshot_or_outside_a_fixture_is_flagged(tmp_path: Path) -> None:
    """No pre-yield snapshot, or not a fixture -> not the recognised shape."""
    src = """
import os

import pytest


@pytest.fixture
def _no_snapshot(overrides):
    yield
    os.environ.update(overrides)


def _plain_generator():
    snapshot = dict(os.environ)
    yield
    os.environ.update(snapshot)
"""
    assert _violations(tmp_path, src) == [
        "10: os.environ.update(<may carry KAIRIX_*>)",
        "16: os.environ.update(<may carry KAIRIX_*>)",
    ]


def test_post_yield_write_that_merely_mentions_the_snapshot_is_flagged(tmp_path: Path) -> None:
    """Codex PR #814 thread: only GENUINE restoration is recognised. A teardown
    write that reads some other value out of the snapshot seeds a KAIRIX_*
    key and must still be reported.

    Sabotage proof (executed): make ``_is_genuine_restore`` return ``True``
    (back to "references the snapshot") → no violation is reported; restored.
    """
    src = """
import os

import pytest


@pytest.fixture
def _leaky():
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    os.environ["KAIRIX_DB_PATH"] = snapshot.get("PATH", "/leak")
"""
    assert _violations(tmp_path, src) == ["13: assign os.environ[KAIRIX_*]"]


def test_per_key_restore_from_the_snapshot_is_recognised(tmp_path: Path) -> None:
    """``os.environ[k] = snapshot[k]`` (same key) and a ``pop`` of ``k`` only
    when ``k`` was absent from the snapshot are genuine per-key restores.

    Sabotage proof (executed): make ``_is_genuine_restore`` return ``False``
    → both restore writes are reported; restored.
    """
    src = """
import os

import pytest

_KEYS = ("KAIRIX_DB_PATH", "KAIRIX_DATA_DIR")


@pytest.fixture
def _restored():
    snapshot = dict(os.environ)
    yield
    for key in _KEYS:
        if key in snapshot:
            os.environ[key] = snapshot[key]
        else:
            os.environ.pop(key, None)
"""
    assert _violations(tmp_path, src) == []


def test_unguarded_or_mismatched_per_key_restore_is_flagged(tmp_path: Path) -> None:
    """A pop not guarded by key-absence, or a restore from a DIFFERENT key,
    is not genuine restoration."""
    src = """
import os

import pytest

_KEYS = ("KAIRIX_DB_PATH", "KAIRIX_DATA_DIR")


@pytest.fixture
def _restored():
    snapshot = dict(os.environ)
    yield
    for key in _KEYS:
        os.environ.pop(key, None)
        os.environ[key] = snapshot["PATH"]
"""
    assert _violations(tmp_path, src) == [
        "14: os.environ.pop(KAIRIX_*)",
        "15: assign os.environ[KAIRIX_*]",
    ]


def test_key_returned_by_a_helper_call_is_flagged(tmp_path: Path) -> None:
    """Codex PR #814 thread: ``os.environ.pop(env_key(), None)`` — the key
    comes from a call to a helper that returns a KAIRIX_* literal.

    Sabotage proof (executed): drop the ``ast.Call`` branch of
    ``key_is_protected`` → no violation is reported; restored.
    """
    src = """
import os


def env_key():
    return "KAIRIX_DB_PATH"


def test_x():
    os.environ.pop(env_key(), None)
"""
    assert _violations(tmp_path, src) == ["10: os.environ.pop(KAIRIX_*)"]


@pytest.mark.parametrize(
    "call",
    [
        'patch.dict(in_dict=os.environ, values={"KAIRIX_DB_PATH": "x"})',
        'patch.dict(values={"KAIRIX_DB_PATH": "x"}, in_dict=os.environ)',
        'patch.dict(os.environ, values={"KAIRIX_DB_PATH": "x"})',
    ],
)
def test_patch_dict_keyword_form_is_flagged(tmp_path: Path, call: str) -> None:
    """Codex PR #814 thread: ``in_dict=`` / ``values=`` keyword spellings.

    Sabotage proof (executed): resolve only the positional ``in_dict`` /
    ``values`` (the pre-fix code) → every case reports clean; restored.
    """
    src = f"import os\nfrom unittest.mock import patch\n\n\ndef test_x():\n    with {call}:\n        pass\n"
    assert _violations(tmp_path, src) == ["6: patch.dict(os.environ, <KAIRIX_*>)"]


def test_patch_dict_keyword_form_on_other_dicts_is_not_flagged(tmp_path: Path) -> None:
    src = (
        "from unittest.mock import patch\n\n\ndef test_x(cfg):\n"
        '    with patch.dict(in_dict=cfg, values={"KAIRIX_DB_PATH": "x"}):\n        pass\n'
    )
    assert _violations(tmp_path, src) == []


# ---------------------------------------------------------------------------
# The reviewed real-tree sites + the failure message contract.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", ["tests/conftest.py", "tests/setup/test_wizard.py"])
def test_reviewed_boundary_sites_in_the_tree_are_clean(relative: str) -> None:
    """The two reviewed process-boundary fixtures pass through the structural
    recognitions — no allow-list entry names them."""
    assert file_violations(_REPO_ROOT / relative) == []


def test_bool_surface_agrees_with_violation_list(tmp_path: Path) -> None:
    path = tmp_path / "test_sample.py"
    path.write_text('import os\nos.environ["KAIRIX_DB_PATH"] = "x"\n', encoding="utf-8")
    assert file_has_env_monkeypatch(path) is True
    path.write_text('import os\nos.environ["XDG_CONFIG_HOME"] = "x"\n', encoding="utf-8")
    assert file_has_env_monkeypatch(path) is False


def test_remediation_is_f21_actionable() -> None:
    """The failure message names the defect, the refactor, and carries the
    fix:/next:/run: markers plus Pass + Forbidden examples (F21)."""
    assert REMEDIATION.startswith("KAIRIX_* process-env write found in a test. Refactor to")
    for marker in ("fix:", "next:", "run:", "Pass example:", "Forbidden example:"):
        assert marker in REMEDIATION
    assert "os.environ['KAIRIX_DB_PATH']" in REMEDIATION
