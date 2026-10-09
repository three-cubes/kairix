"""Scope-aware resolution of the guarded imports for F1 / F2.

A name only refers to the imported ``os`` / ``sys`` / ``importlib`` module (or
``from os import environ``) when, under Python's scoping rules, nothing between
the reference and the import rebinds it: a parameter, assignment, loop / with /
except target, walrus, import, ``def`` / ``class`` name in an enclosing
function, lambda or comprehension scope shadows it. A name bound anywhere in a
function body is local throughout that body, so function-scope shadowing
has no before / after ordering and is not a violation.

Rebinding a guarded name where ordering WOULD matter is itself a violation (the
alias-ban principle, ``_mapping_writes.guarded_rebinds``): any module-level or
class-body binding of ``os`` / ``sys`` / ``importlib`` / ``environ`` /
``modules`` / ``reload`` / ``getattr`` / ``setattr`` / ``delattr`` /
``__import__`` other than its own import (incl. ``import json as os`` and a
``global os`` store in a function), and a function that both imports the name
and rebinds it. Any reference to builtin ``__import__`` is an F1 violation.

Sabotage proofs (executed — mutate, confirm red, restore, confirm green):
  * make ``ModuleIndex.resolve`` ignore scopes (always look the name up in the
    module scope) → every SHADOWED case below is flagged;
  * make ``ModuleIndex.resolves_to_module`` return ``True`` unconditionally →
    the shadowed ``os`` / ``sys`` / ``importlib`` cases are flagged (the
    ``from os import environ`` case is pinned by ``resolves_to_from``);
  * make ``guarded_rebinds`` return ``[]`` → every REBINDS case reports clean;
  * drop the ``module_like`` branch of ``guarded_rebinds`` (report only mixed
    import + rebind scopes) → the module / class / global cases with no
    same-scope import report clean;
  * make ``_dunder_import_violation`` return ``False`` → every DUNDER_IMPORT
    case reports clean.
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


SHADOWED = {
    "F2 parameter named os": (
        "F2",
        'import os\n\ndef helper(os):\n    os.environ["KAIRIX_DB_PATH"] = "x"\n',
    ),
    "F2 local environ after from-import": (
        "F2",
        'from os import environ\n\ndef test_x():\n    environ = {}\n    environ["KAIRIX_DB_PATH"] = "x"\n',
    ),
    "F2 local bound later in the body is local throughout": (
        "F2",
        'from os import environ\n\ndef test_x():\n    environ["KAIRIX_DB_PATH"] = "x"\n    environ = {}\n',
    ),
    "F2 comprehension variable named os": (
        "F2",
        'import os\n\ndef test_x(stubs):\n    return [os.environ.setdefault("KAIRIX_DB_PATH", "x") for os in stubs]\n',
    ),
    "F2 lambda parameter named os": (
        "F2",
        'import os\n\nset_it = lambda os: os.environ.update(KAIRIX_DB_PATH="x")\n',
    ),
    "F2 with-target named os": (
        "F2",
        'import os\n\ndef test_x(fake):\n    with fake() as os:\n        os.environ["KAIRIX_DB_PATH"] = "x"\n',
    ),
    "F1 parameter named sys": (
        "F1",
        'import sys\n\ndef helper(sys):\n    sys.modules["kairix.paths"] = object()\n',
    ),
    "F1 parameter named importlib": (
        "F1",
        "import importlib\nimport kairix.paths\n\ndef helper(importlib):\n    importlib.reload(kairix.paths)\n",
    ),
}


@pytest.mark.parametrize(("rule", "source"), SHADOWED.values(), ids=list(SHADOWED))
def test_a_shadowing_binding_is_not_the_guarded_import(tmp_path: Path, rule: str, source: str) -> None:
    assert not _flagged(tmp_path, rule, source)


UNSHADOWED = {
    "F2 plain reference in a function": (
        "F2",
        'import os\n\ndef test_x():\n    os.environ["KAIRIX_DB_PATH"] = "x"\n',
    ),
    "F2 from-import reference": (
        "F2",
        'from os import environ\n\ndef test_x():\n    environ["KAIRIX_DB_PATH"] = "x"\n',
    ),
    "F2 outer still resolves while a nested def shadows": (
        "F2",
        'import os\n\ndef test_x():\n    def inner(os):\n        return os\n    os.environ["KAIRIX_DB_PATH"] = "x"\n',
    ),
    "F2 nested def without a local binding resolves outward": (
        "F2",
        'import os\n\ndef test_x():\n    def inner():\n        os.environ["KAIRIX_DB_PATH"] = "x"\n    inner()\n',
    ),
    "F2 class-body binding does not shadow inside a method": (
        "F2",
        "import os\n\nclass TestX:\n    os = None\n\n"
        '    def test_y(self):\n        os.environ["KAIRIX_DB_PATH"] = "x"\n',
    ),
    "F2 import inside the function resolves in that scope": (
        "F2",
        'def test_x():\n    import os\n    os.environ["KAIRIX_DB_PATH"] = "x"\n',
    ),
    "F2 comprehension iterable uses the outer name": (
        "F2",
        'import os\n\ndef test_x():\n    return [os for os in [os.environ.setdefault("KAIRIX_DB_PATH", "x")]]\n',
    ),
    "F2 module-level augmented assign mutates in place, does not shadow": (
        "F2",
        'from os import environ\n\nenviron |= {"KAIRIX_DB_PATH": "x"}\n',
    ),
    "F1 plain sys reference": (
        "F1",
        'import sys\n\ndef test_x():\n    sys.modules["kairix.paths"] = object()\n',
    ),
    "F1 plain importlib reference": (
        "F1",
        "import importlib\nimport kairix.paths\n\ndef test_x():\n    importlib.reload(kairix.paths)\n",
    ),
}


@pytest.mark.parametrize(("rule", "source"), UNSHADOWED.values(), ids=list(UNSHADOWED))
def test_an_unshadowed_reference_still_resolves(tmp_path: Path, rule: str, source: str) -> None:
    assert _flagged(tmp_path, rule, source)


# ---------------------------------------------------------------------------
# Rebinding a guarded name where ordering matters is itself a violation.
# ---------------------------------------------------------------------------

REBINDS = {
    "F2 Codex: write BEFORE a module-level rebind": (
        "F2",
        'import os\n\nos.environ["KAIRIX_DB_PATH"] = "x"\nos = object()\n',
    ),
    "F2 Codex: write AFTER a re-import": (
        "F2",
        'import os\n\nos = object()\nimport os\n\nos.environ["KAIRIX_DB_PATH"] = "x"\n',
    ),
    "F2 module-level rebind, write in a function": (
        "F2",
        'import os\n\nos = object()\n\ndef test_x():\n    os.environ["KAIRIX_DB_PATH"] = "x"\n',
    ),
    "F2 import of another module as os": (
        "F2",
        "import json as os\n\ndef test_x():\n    return os.environ\n",
    ),
    "F2 module-level environ rebind after from-import": (
        "F2",
        'from os import environ\n\nenviron = {}\nenviron["PATH"] = "x"\n',
    ),
    "F2 class-body rebind": (
        "F2",
        "import os\n\nclass TestX:\n    os = object()\n    environ = os.environ\n",
    ),
    "F2 global store in a function": (
        "F2",
        "import os\n\ndef setup_module():\n    global os\n    os = object()\n\n"
        'def test_x():\n    os.environ["PATH"] = "x"\n',
    ),
    "F2 module-level def getattr": (
        "F2",
        'import os\n\ndef getattr(obj, name):\n    return None\n\nvalue = os.environ.get("PATH")\n',
    ),
    "F2 function imports os and rebinds it": (
        "F2",
        'def test_x():\n    import os\n    os.environ["KAIRIX_DB_PATH"] = "x"\n    os = object()\n',
    ),
    "F2 function from-imports environ and rebinds it": (
        "F2",
        'def test_x():\n    from os import environ\n    environ["KAIRIX_DB_PATH"] = "x"\n    environ = {}\n',
    ),
    "F1 Codex: sys.modules write BEFORE a module-level rebind": (
        "F1",
        'import sys\n\nsys.modules["kairix.paths"] = object()\nsys = object()\n',
    ),
    "F1 module-level modules rebind": (
        "F1",
        'from sys import modules\n\nmodules = {}\nmodules["kairix.paths"] = 1\n',
    ),
    "F1 import of another module as importlib": (
        "F1",
        "import json as importlib\nimport kairix.paths\n",
    ),
    "F1 module-level reload rebind": (
        "F1",
        "from importlib import reload\n\nreload = print\nimport kairix.paths\n",
    ),
}


@pytest.mark.parametrize(("rule", "source"), REBINDS.values(), ids=list(REBINDS))
def test_rebinding_a_guarded_name_is_a_violation(tmp_path: Path, rule: str, source: str) -> None:
    assert _flagged(tmp_path, rule, source)


NOT_REBINDS = {
    "F2 import os and import os.path": ("F2", 'import os\nimport os.path\n\nvalue = os.environ.get("PATH")\n'),
    "F2 import os as os": ("F2", 'import os as os\n\nvalue = os.environ.get("PATH")\n'),
    "F2 from builtins import getattr": (
        "F2",
        'import os\nfrom builtins import getattr\n\nvalue = getattr(os, "sep")\n',
    ),
    "F2 function-local environ with no import of it there": (
        "F2",
        'from os import environ\n\ndef test_x():\n    environ = {"KAIRIX_DB_PATH": "x"}\n    return environ\n',
    ),
    "F1 import sys twice": ("F1", 'import sys\nimport sys\n\nloaded = "kairix.paths" in sys.modules\n'),
}


@pytest.mark.parametrize(("rule", "source"), NOT_REBINDS.values(), ids=list(NOT_REBINDS))
def test_canonical_imports_and_function_local_shadows_are_not_rebinds(tmp_path: Path, rule: str, source: str) -> None:
    assert not _flagged(tmp_path, rule, source)


# ---------------------------------------------------------------------------
# Any reference to builtin ``__import__`` is an F1 violation.
# ---------------------------------------------------------------------------

DUNDER_IMPORT = {
    "literal call": 'mod = __import__("kairix.paths")\n',
    "split-constant call": 'mod = __import__("kai" + "rix.paths")\n',
    "non-kairix name": 'mod = __import__("pathlib")\n',
    "bound to a name": "loader = __import__\n",
    "builtins attribute": 'import builtins\n\nmod = builtins.__import__("json")\n',
    "importlib attribute": 'import importlib\n\nmod = importlib.__import__("json")\n',
    "builtins getattr literal": 'import builtins\n\nload = getattr(builtins, "__import__")\n',
    "builtins getattr split constant": 'import builtins\n\nload = getattr(builtins, "__im" + "port__")\n',
    "builtins getattr constant name": 'import builtins\n\nNAME = "__im" "port__"\nload = getattr(builtins, NAME)\n',
    "__builtins__ subscript": 'load = __builtins__["__import__"]\n',
    "vars(builtins) split-constant subscript": 'import builtins\n\nload = vars(builtins)["__" + "import__"]\n',
    "from builtins import": 'from builtins import __import__\n\nmod = __import__("json")\n',
    "monkeypatch.setattr on builtins": (
        "import builtins\n\ndef test_x(monkeypatch):\n"
        '    monkeypatch.setattr(builtins, "__import__", lambda *a, **k: None)\n'
    ),
}


@pytest.mark.parametrize("source", DUNDER_IMPORT.values(), ids=list(DUNDER_IMPORT))
def test_any_reference_to_builtin_dunder_import_is_flagged(tmp_path: Path, source: str) -> None:
    assert _flagged(tmp_path, "F1", source)


NOT_DUNDER_IMPORT = {
    "importlib.import_module static name": 'import importlib\n\nmod = importlib.import_module("json")\n',
    "unrelated getattr": 'import builtins\n\nlen_fn = getattr(builtins, "len")\n',
    "unrelated subscript": 'import builtins\n\nlen_fn = vars(builtins)["len"]\n',
    "missing-dependency simulation via sys.modules": (
        'import sys\n\ndef test_x(monkeypatch):\n    monkeypatch.setitem(sys.modules, "yaml", None)\n'
    ),
}


@pytest.mark.parametrize("source", NOT_DUNDER_IMPORT.values(), ids=list(NOT_DUNDER_IMPORT))
def test_static_imports_and_unrelated_lookups_are_not_flagged(tmp_path: Path, source: str) -> None:
    assert not _flagged(tmp_path, "F1", source)
