"""Four PR #814 review items closed with the scope index (F1 / F2).

1. Dunder access on a guarded module (``os`` / ``sys`` / ``importlib``) is
   default-deny and never traced: ``os.__dict__``, ``sys.__getattribute__``,
   ``getattr(importlib, "__dict__")`` ... all fail; so do ``vars(R)`` /
   ``R.__dict__`` on ``os.environ`` / ``sys.modules`` themselves.
2. ``os.putenv`` / ``os.unsetenv`` (and their ``posix`` / ``nt`` twins) are
   keyed process-env writes: a protected or unresolved key fails, and any use
   other than a direct call (an alias, ``getattr(os, "putenv")``) fails.
3. ``patch`` / ``mock`` resolve through Python's scoping rules: a local
   ``def patch(...)`` or a parameter named ``patch`` / ``mock`` is not
   ``unittest.mock`` (the false positive); every real spelling still is.
4. ``ConstantTable`` keys values by VARIABLE — (scope, name) — so ``key =
   "PATH"`` in one test and ``key = "KAIRIX_DB_PATH"`` in another never merge
   (the false positive); several bindings in ONE scope still union.

Sabotage proofs (executed — mutate, confirm red, restore, confirm green):
  * ``classify_module`` stops reporting dunder attributes → the module-dunder
    cases report clean;
  * ``MappingGuard.process_writer_findings`` returns ``[]`` → every putenv /
    unsetenv case reports clean;
  * ``is_patch_ref`` treats an unresolved-to-mock ``patch`` name as patch again
    (``return expr.id == "patch" or ...``) → the local-def / parameter cases
    are flagged;
  * ``is_dunder`` always ``False`` → 11 dunder cases report clean;
  * ``ModuleIndex.scope_bindings`` returns every same-named binding in the
    file (the old merge) → the cross-function / nested / parameter cases are
    flagged; ``_param_values`` no longer filtering by the owning function →
    the same-parameter-name cases are flagged; ``global`` stores not moved to
    module scope → the global / nonlocal rebind cases report clean.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

from check_no_env_monkeypatch import file_violations  # noqa: E402 — see _CHECKS_DIR sys.path insert above
from check_no_internal_patches import file_has_internal_patch  # noqa: E402 — see _CHECKS_DIR sys.path insert above

pytestmark = pytest.mark.unit


def _f2(path: Path) -> bool:
    return bool(file_violations(path))


DETECTORS: dict[str, Callable[[Path], bool]] = {"F1": file_has_internal_patch, "F2": _f2}


def _flagged(tmp_path: Path, rule: str, source: str) -> bool:
    path = tmp_path / "test_sample.py"
    compile(source, str(path), "exec")  # a malformed sample must fail loudly, never read as "clean"
    path.write_text(source, encoding="utf-8")
    return DETECTORS[rule](path)


# ---------------------------------------------------------------------------
# 1. Dunder access on a guarded module / mapping.
# ---------------------------------------------------------------------------

DUNDERS = {
    "F2 os.__dict__": ("F2", 'import os\n\nos.__dict__["environ"]["KAIRIX_DB_PATH"] = "x"\n'),
    "F2 os.__getattribute__": ("F2", 'import os\n\nenv = os.__getattribute__("env" + "iron")\n'),
    "F2 os.__setattr__": ("F2", 'import os\n\nos.__setattr__("environ", {})\n'),
    "F2 os.__delattr__": ("F2", 'import os\n\nos.__delattr__("environ")\n'),
    "F2 os.__getattr__ (any dunder)": ("F2", "import os\n\nloader = os.__loader__\n"),
    "F2 getattr(os, '__dict__')": ("F2", 'import os\n\nns = getattr(os, "__dict__")\n'),
    "F2 vars(os.environ)": ("F2", "import os\n\nns = vars(os.environ)\n"),
    "F2 os.environ.__dict__": ("F2", "import os\n\nns = os.environ.__dict__\n"),
    "F1 sys.__dict__": ("F1", 'import sys\n\nsys.__dict__["modules"]["kairix.paths"] = object()\n'),
    "F1 sys.__getattribute__": ("F1", 'import sys\n\nmods = sys.__getattribute__("modules")\n'),
    "F1 getattr(sys, '__dict__')": ("F1", 'import sys\n\nns = getattr(sys, "__dict__")\n'),
    "F1 importlib.__dict__": ("F1", 'import importlib\n\nreload_fn = importlib.__dict__["reload"]\n'),
    "F1 getattr(importlib, '__dict__')": ("F1", 'import importlib\n\nns = getattr(importlib, "__dict__")\n'),
    "F1 vars(sys.modules)": ("F1", "import sys\n\nns = vars(sys.modules)\n"),
    "F1 sys.modules.__dict__": ("F1", "import sys\n\nns = sys.modules.__dict__\n"),
}


@pytest.mark.parametrize(("rule", "source"), DUNDERS.values(), ids=list(DUNDERS))
def test_dunder_access_on_a_guarded_object_is_flagged(tmp_path: Path, rule: str, source: str) -> None:
    assert _flagged(tmp_path, rule, source)


NOT_DUNDERS = {
    "F2 os.path / os.sep": ("F2", 'import os\n\np = os.path.join("a", os.sep)\n'),
    "F2 dunder on os.path (not the guarded module)": ("F2", "import os\n\nname = os.path.__name__\n"),
    "F1 sys.version_info": ("F1", "import sys\n\nv = sys.version_info\n"),
    "F1 importlib.import_module static": ("F1", 'import importlib\n\nmod = importlib.import_module("json")\n'),
}


@pytest.mark.parametrize(("rule", "source"), NOT_DUNDERS.values(), ids=list(NOT_DUNDERS))
def test_plain_attribute_access_is_not_flagged(tmp_path: Path, rule: str, source: str) -> None:
    assert not _flagged(tmp_path, rule, source)


# ---------------------------------------------------------------------------
# 2. os.putenv / os.unsetenv are keyed process-env writes (F2).
# ---------------------------------------------------------------------------

PUTENV = {
    "putenv literal": 'import os\n\nos.putenv("KAIRIX_DB_PATH", "x")\n',
    "unsetenv literal": 'import os\n\nos.unsetenv("KAIRIX_DB_PATH")\n',
    "keyword key": 'import os\n\nos.putenv(key="KAIRIX_DB_PATH", value="x")\n',
    "split constant": 'import os\n\nos.putenv("KAI" + "RIX_DB_PATH", "x")\n',
    "unresolved key": "import os\n\ndef test_x(key):\n    os.putenv(key, 'x')\n",
    "spread": "import os\n\ndef test_x(args):\n    os.putenv(*args)\n",
    "from-import": 'from os import putenv\n\nputenv("KAIRIX_DB_PATH", "x")\n',
    "from-import alias": 'from os import unsetenv as drop\n\ndrop("KAIRIX_DB_PATH")\n',
    "aliased writer": 'import os\n\nwrite = os.putenv\nwrite("PATH", "x")\n',
    "getattr(os, 'putenv')": 'import os\n\ngetattr(os, "putenv")("KAIRIX_DB_PATH", "x")\n',
    "posix twin": 'import posix\n\nposix.putenv("KAIRIX_DB_PATH", "x")\n',
}


@pytest.mark.parametrize("source", PUTENV.values(), ids=list(PUTENV))
def test_putenv_and_unsetenv_with_a_protected_or_unknown_key_are_flagged(tmp_path: Path, source: str) -> None:
    assert _flagged(tmp_path, "F2", source)


NOT_PUTENV = {
    "putenv non-KAIRIX": 'import os\n\nos.putenv("PATH", "/usr/bin")\n',
    "unsetenv non-KAIRIX": 'import os\n\nos.unsetenv("XDG_CONFIG_HOME")\n',
    "local def named putenv": 'def putenv(key, value):\n    return key\n\nputenv("KAIRIX_DB_PATH", "x")\n',
}


@pytest.mark.parametrize("source", NOT_PUTENV.values(), ids=list(NOT_PUTENV))
def test_putenv_with_a_provably_safe_key_is_not_flagged(tmp_path: Path, source: str) -> None:
    assert not _flagged(tmp_path, "F2", source)


# ---------------------------------------------------------------------------
# 3. patch / mock resolve through scopes.
# ---------------------------------------------------------------------------

PATCH_SHADOWED = {
    "F1 local def patch": (
        "F1",
        'def patch(target):\n    return target\n\n\ndef test_x():\n    patch("kairix.paths.x")\n',
    ),
    "F1 parameter named patch": ("F1", 'def test_x(patch):\n    patch("kairix.paths.x")\n'),
    "F1 parameter named mock": ("F1", 'def test_x(mock):\n    mock.patch("kairix.paths.x")\n'),
    "F2 local def patch": ("F2", 'def patch(target):\n    return target\n\n\ndef test_x():\n    patch("os.environ")\n'),
    "F2 parameter named patch": ("F2", 'def test_x(patch):\n    patch("os.environ")\n'),
}


@pytest.mark.parametrize(("rule", "source"), PATCH_SHADOWED.values(), ids=list(PATCH_SHADOWED))
def test_a_local_patch_is_not_unittest_mock(tmp_path: Path, rule: str, source: str) -> None:
    assert not _flagged(tmp_path, rule, source)


REAL_PATCH = {
    "F1 from unittest.mock import patch": (
        "F1",
        'from unittest.mock import patch\n\n\ndef test_x():\n    patch("kairix.paths.x")\n',
    ),
    "F1 imported alias": (
        "F1",
        'from unittest.mock import patch as p\n\n\n@p("kairix.paths.x")\ndef test_x():\n    pass\n',
    ),
    "F1 from unittest import mock": (
        "F1",
        'from unittest import mock\n\n\ndef test_x():\n    mock.patch("kairix.paths.x")\n',
    ),
    "F1 import unittest.mock": (
        "F1",
        'import unittest.mock\n\n\ndef test_x():\n    unittest.mock.patch("kairix.paths.x")\n',
    ),
    "F1 import unittest.mock as um": (
        "F1",
        'import unittest.mock as um\n\n\ndef test_x():\n    um.patch("kairix.paths.x")\n',
    ),
    "F1 import mock": ("F1", 'import mock\n\n\ndef test_x():\n    mock.patch("kairix.paths.x")\n'),
    "F1 star import": ("F1", 'from unittest.mock import *\n\n\ndef test_x():\n    patch("kairix.paths.x")\n'),
    "F1 patch.object": (
        "F1",
        "import kairix.paths\nfrom unittest.mock import patch\n\n\n"
        'def test_x():\n    patch.object(kairix.paths, "x")\n',
    ),
    "F2 imported alias patch.dict": (
        "F2",
        "import os\nfrom unittest.mock import patch as p\n\n\n"
        'def test_x():\n    p.dict(os.environ, {"KAIRIX_DB_PATH": "x"})\n',
    ),
    "F2 patch('os.environ')": ("F2", 'from unittest import mock\n\n\ndef test_x():\n    mock.patch("os.environ")\n'),
}


@pytest.mark.parametrize(("rule", "source"), REAL_PATCH.values(), ids=list(REAL_PATCH))
def test_real_mock_controls_are_still_patch(tmp_path: Path, rule: str, source: str) -> None:
    assert _flagged(tmp_path, rule, source)


# ---------------------------------------------------------------------------
# 4. Constants are per VARIABLE (scope, name) — written once for F2, derived for F1.
# ---------------------------------------------------------------------------

_F2_TO_F1 = (
    ("import os", "import sys"),
    ("os.environ", "sys.modules"),
    ('"PATH"', '"json"'),
    ('"KAIRIX_DB_PATH"', '"kairix.paths"'),
    ('= "x"', "= object()"),
)


def _as_f1(source: str) -> str:
    for old, new in _F2_TO_F1:
        source = source.replace(old, new)
    return source


SCOPED_CLEAN = {
    "cross-function same name": (
        'import os\n\n\ndef test_a():\n    key = "PATH"\n    os.environ[key] = "x"\n\n\n'
        'def test_b():\n    key = "KAIRIX_DB_PATH"\n    return key\n'
    ),
    "nested function shadows the outer name": (
        'import os\n\n\ndef test_a():\n    key = "PATH"\n\n    def inner():\n        key = "KAIRIX_DB_PATH"\n'
        '        return key\n\n    os.environ[key] = "x"\n    return inner\n'
    ),
    "parameter shadows a module constant": (
        'import os\n\nKEY = "KAIRIX_DB_PATH"\n\n\ndef set_it(KEY):\n    os.environ[KEY] = "x"\n\n\n'
        'def test_a():\n    set_it("PATH")\n'
    ),
    "same parameter name in another function": (
        'import os\n\n\ndef helper_a(key):\n    os.environ[key] = "x"\n\n\ndef helper_b(key):\n    return key\n\n\n'
        'def test_a():\n    helper_a("PATH")\n    helper_b("KAIRIX_DB_PATH")\n'
    ),
}

SCOPED_FLAGGED = {
    "same scope still unions": (
        'import os\n\n\ndef test_a(flag):\n    key = "PATH"\n    if flag:\n        key = "KAIRIX_DB_PATH"\n'
        '    os.environ[key] = "x"\n'
    ),
    "nested function reads the outer variable": (
        "import os\n\n\n"
        'def test_a():\n    key = "KAIRIX_DB_PATH"\n\n    def inner():\n        os.environ[key] = "x"\n\n'
        "    inner()\n"
    ),
    "parameter resolves through its own call sites": (
        'import os\n\nKEY = "PATH"\n\n\ndef set_it(KEY):\n    os.environ[KEY] = "x"\n\n\n'
        'def test_a():\n    set_it("KAIRIX_DB_PATH")\n'
    ),
    "global store rebinds the module constant": (
        'import os\n\nKEY = "PATH"\n\n\ndef setup_module():\n    global KEY\n    KEY = "KAIRIX_DB_PATH"\n\n\n'
        'def test_a():\n    os.environ[KEY] = "x"\n'
    ),
    "nonlocal store rebinds the enclosing variable": (
        'import os\n\n\ndef test_a():\n    key = "PATH"\n\n    def swap():\n        nonlocal key\n'
        '        key = "KAIRIX_DB_PATH"\n\n    swap()\n    os.environ[key] = "x"\n'
    ),
}


def _both_rules(cases: dict[str, str]) -> list[pytest.param]:
    out = []
    for label, source in cases.items():
        out.append(pytest.param("F2", source, id=f"F2 {label}"))
        out.append(pytest.param("F1", _as_f1(source), id=f"F1 {label}"))
    return out


@pytest.mark.parametrize(("rule", "source"), _both_rules(SCOPED_CLEAN))
def test_same_named_bindings_in_other_scopes_do_not_merge(tmp_path: Path, rule: str, source: str) -> None:
    assert not _flagged(tmp_path, rule, source)


@pytest.mark.parametrize(("rule", "source"), _both_rules(SCOPED_FLAGGED))
def test_the_variable_a_reference_resolves_to_still_decides(tmp_path: Path, rule: str, source: str) -> None:
    assert _flagged(tmp_path, rule, source)
