"""Scope-aware resolution of the guarded imports for F1 / F2.

A name only refers to the imported ``os`` / ``sys`` / ``importlib`` module (or
``from os import environ``) when, under Python's scoping rules, nothing between
the reference and the import rebinds it: a parameter, assignment, loop / with /
except target, walrus, import, ``def`` / ``class`` name in an enclosing
function, lambda or comprehension scope shadows it. A name bound anywhere in a
function body is local throughout that body. A module-level rebind of the
imported name (``os = FakeOs()``) shadows it for the WHOLE module — resolution
requires every binding in the resolving scope to be an import of the guarded
module.

Sabotage proofs (executed — mutate, confirm red, restore, confirm green):
  * make ``ModuleIndex.resolve`` ignore scopes (always look the name up in the
    module scope) → every SHADOWED case below is flagged;
  * make ``ModuleIndex.resolves_to_module`` return ``True`` unconditionally →
    the shadowed ``os`` / ``sys`` / ``importlib`` cases are flagged (the
    ``from os import environ`` case is pinned by ``resolves_to_from``).
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
    "F2 module-level rebind shadows for the whole module": (
        "F2",
        'import os\n\nos = object()\n\ndef test_x():\n    os.environ["KAIRIX_DB_PATH"] = "x"\n',
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
