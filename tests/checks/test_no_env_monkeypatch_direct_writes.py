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
    _GUARD_RESTORE_MARKER,
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


_CONFTEST_BASELINE = """
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _baseline():
    with allow_baseline_writes():
        os.environ["KAIRIX_CONNECT_DISABLE_BROWSER"] = "1"
    yield
"""


@pytest.mark.parametrize(
    "statement",
    [
        'os.environ["KAIRIX_" + "X"] = "1"',
        'os.environ.setdefault(PREFIX + "X", "1")',
        'os.environ["PATH"] = "/usr/bin"',
        "os.environ.update(VALUES)",
        'os.putenv(NAME, "1")',
    ],
)
def test_module_level_env_write_in_a_conftest_is_flagged_for_any_key(tmp_path: Path, statement: str) -> None:
    """Conftest import-time code runs before the runtime guard is configured,
    so a conftest may not write the env at module level at all — computed
    keys included.

    Sabotage proof (executed): skip the ``conftest.py`` branch → nothing is
    flagged and every case fails; restored.
    """
    conftest = tmp_path / "conftest.py"
    conftest.write_text(f"import os\n\n{statement}\n", encoding="utf-8")
    assert file_violations(conftest) == ["3: module-level os.environ write in conftest.py (any key)"]


def test_fixture_body_baseline_write_in_the_root_conftest_is_clean() -> None:
    """The session baseline's writes inside its fixture (under
    ``allow_baseline_writes()``) are not module-level: the root conftest passes."""
    assert file_violations(_REPO_ROOT / "tests" / "conftest.py") == []


def test_fixture_body_write_in_a_conftest_is_not_a_module_level_write(tmp_path: Path) -> None:
    conftest = tmp_path / "conftest.py"
    conftest.write_text(_CONFTEST_BASELINE, encoding="utf-8")
    violations = file_violations(conftest)
    assert not any("module-level" in v for v in violations), violations


def test_aliased_os_module_level_write_in_a_conftest_is_flagged(tmp_path: Path) -> None:
    """``import os as _os`` (the root conftest's own spelling) is still ``os``.

    Sabotage proof (executed): recognise only the literal name ``os`` →
    nothing is flagged and this fails; restored.
    """
    conftest = tmp_path / "conftest.py"
    conftest.write_text('import os as _os\n\n_os.environ["X"] = "1"\n', encoding="utf-8")
    assert file_violations(conftest) == ["3: module-level os.environ write in conftest.py (any key)"]


def test_aliased_os_kairix_write_in_a_test_is_flagged_and_reads_are_clean(tmp_path: Path) -> None:
    source = (
        "import os as _os\n\n\n"
        "def test_x():\n"
        '    value = _os.environ.get("KAIRIX_DB_PATH")\n'
        '    other = _os.environ["KAIRIX_DB_PATH"]\n'
        '    _os.environ["KAIRIX_DB_PATH"] = value or other\n'
    )
    assert _file_violations(tmp_path, source) == ["7: assign os.environ[KAIRIX_*]"]


def test_wholesale_environ_replacement_is_flagged_even_when_restored(tmp_path: Path) -> None:
    """A replace-and-restore inside one phase leaves ``os.environ`` the same
    object at the phase end, so the runtime identity check cannot see it; the
    static half flags every assignment to ``os.environ`` (any alias).

    Sabotage proof (executed): drop the wholesale-replacement branch in
    ``_shape`` → no violations and this fails; restored.
    """
    source = (
        "import os\nimport os as _os\n\n\n"
        "def test_x():\n"
        "    original = os.environ\n"
        "    os.environ = {}\n"
        "    _os.environ = original\n"
        "    assert dict(os.environ)\n"
    )
    assert _file_violations(tmp_path, source) == [
        "7: os.environ replaced wholesale",
        "8: os.environ replaced wholesale",
    ]


def test_annotated_environ_replacement_is_flagged_and_bare_annotation_is_clean(tmp_path: Path) -> None:
    """``os.environ: dict[str, str] = {}`` is an ``AnnAssign`` — still a
    wholesale replacement. A bare annotation (no value) assigns nothing.

    Sabotage proof (executed): drop ``ast.AnnAssign`` from ``_shape`` → the
    annotated replacement is not flagged and this fails; restored.
    """
    source = "import os\n\n\ndef test_x():\n    os.environ: dict[str, str] = {}\n    os.environ: dict[str, str]\n"
    assert _file_violations(tmp_path, source) == ["5: os.environ replaced wholesale"]


@pytest.mark.parametrize(
    "source",
    [
        'import os\n\n\ndef fixture(value=os.environ.pop("X", None)):\n    return value\n',
        'import os\n\n\n@register(os.environ.setdefault("X", "1"))\ndef fixture():\n    return 1\n',
        'import os\n\nhook = lambda value=os.environ.pop("X", None): value\n',
    ],
    ids=["default", "decorator", "lambda-default"],
)
def test_conftest_writes_in_defaults_and_decorators_run_at_import(tmp_path: Path, source: str) -> None:
    """Defaults and decorators are evaluated when the ``def`` executes — at
    conftest import, before the runtime guard is configured — so they are
    module-level writes; only a function / lambda BODY is deferred.

    Sabotage proof (executed): treat any node under a ``def`` / ``lambda`` as
    deferred (the old ancestor walk) → nothing is flagged and every case
    fails; restored.
    """
    conftest = tmp_path / "conftest.py"
    conftest.write_text(source, encoding="utf-8")
    assert [v.split(": ", 1)[1] for v in file_violations(conftest)] == [
        "module-level os.environ write in conftest.py (any key)"
    ]


def test_conftest_write_inside_a_lambda_body_is_deferred(tmp_path: Path) -> None:
    conftest = tmp_path / "conftest.py"
    conftest.write_text('import os\n\nhook = lambda: os.environ.pop("X", None)\n', encoding="utf-8")
    assert not any("module-level" in v for v in file_violations(conftest))


def test_setattr_environ_replacement_is_flagged_even_when_restored(tmp_path: Path) -> None:
    """``setattr(os, "environ", ...)`` replaces the mapping without an
    assignment target, and a constant-folded name (``"envi" + "ron"``) hides
    it from a literal match; a replace-and-restore inside one phase is
    invisible to the runtime identity check, so the static half flags every
    such call (``delattr`` included).

    Sabotage proof (executed): drop the ``setattr`` branch in ``_shape`` → no
    violations and this fails; restored.
    """
    source = (
        "import os\nimport os as _os\n\n\n"
        "def test_x():\n"
        "    original = os.environ\n"
        '    setattr(os, "envi" + "ron", {})\n'
        '    setattr(_os, "environ", original)\n'
        '    delattr(os, "environ")\n'
        '    setattr(os, f"envi{"ron"}", original)\n'
        '    setattr(os, "sep", "/")\n'
        '    setattr(config, "environ", {})\n'
        '    setattr(os, "envi" + name, {})\n'
    )
    assert _file_violations(tmp_path, source) == [
        "7: os.environ replaced wholesale",
        "8: os.environ replaced wholesale",
        "9: os.environ replaced wholesale",
        "10: os.environ replaced wholesale",
    ]


def test_runtime_guard_restore_is_the_one_exempt_wholesale_assignment(tmp_path: Path) -> None:
    """The runtime guard puts the snapshotted mapping back after a test
    deleted or replaced ``os.environ``; that file alone may assign it. The
    same statement anywhere else is a violation, and a KAIRIX_* write in the
    guard file would still be one.

    Sabotage proof (executed): drop the ``_GUARD_HOME`` filter → the guard's
    restore is flagged and this fails; restored.
    """
    guard = _REPO_ROOT / "tests" / "fixtures" / "process_state_guard.py"
    assert "os.environ = _STATE.environ" in guard.read_text(encoding="utf-8")
    assert file_violations(guard) == []
    assert _file_violations(tmp_path, "import os\n\nos.environ = _STATE.environ\n") == [
        "3: os.environ replaced wholesale"
    ]


def test_setattr_environ_replacement_at_conftest_import_is_a_module_level_write(tmp_path: Path) -> None:
    conftest = tmp_path / "conftest.py"
    conftest.write_text('import os\n\nsetattr(os, "environ", {})\n', encoding="utf-8")
    assert file_violations(conftest) == [
        "3: module-level os.environ write in conftest.py (any key)",
        "3: os.environ replaced wholesale",
    ]


@pytest.mark.parametrize(
    "source",
    [
        'from os import putenv as write_env\n\nwrite_env("X", "1")\n',
        'from os import putenv, unsetenv\n\nunsetenv("X")\n',
        'from os import unsetenv as drop\n\ndrop("X")\n',
    ],
    ids=["putenv-alias", "unsetenv", "unsetenv-alias"],
)
def test_directly_imported_putenv_at_conftest_import_is_flagged(tmp_path: Path, source: str) -> None:
    """``from os import putenv [as name]`` is still ``os.putenv``: at conftest
    import it runs before the audit hook exists.

    Sabotage proof (executed): recognise only ``<os>.putenv`` attribute calls →
    nothing is flagged and every case fails; restored.
    """
    conftest = tmp_path / "conftest.py"
    conftest.write_text(source, encoding="utf-8")
    assert file_violations(conftest) == ["3: module-level os.environ write in conftest.py (any key)"]


def test_other_imported_functions_at_conftest_import_are_clean(tmp_path: Path) -> None:
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        'from os import getcwd, putenv\nfrom shutil import putenv as other\n\nHERE = getcwd()\nother("X", "1")\n'
        "\n\ndef putenv_later():\n    putenv('X', '1')\n",
        encoding="utf-8",
    )
    assert file_violations(conftest) == []


def test_postponed_annotations_are_not_import_time_writes(tmp_path: Path) -> None:
    """Under ``from __future__ import annotations`` a parameter or return
    annotation is a string that never runs; the same expression as a default
    or a decorator still runs at ``def`` time.

    Sabotage proof (executed): ignore the ``__future__`` import in
    ``_runs_at_import`` → the postponed case is flagged and this fails;
    restored.
    """
    postponed = (
        "from __future__ import annotations\n\nimport os\n\n\n"
        'def fixture(x: os.environ.pop("X", None)) -> os.environ.pop("Y", None):\n'
        "    return x\n"
    )
    conftest = tmp_path / "conftest.py"
    conftest.write_text(postponed, encoding="utf-8")
    assert file_violations(conftest) == []
    eager = postponed.replace("from __future__ import annotations\n\n", "")
    conftest.write_text(eager, encoding="utf-8")
    assert file_violations(conftest) == ["4: module-level os.environ write in conftest.py (any key)"]
    still_eager = (
        "from __future__ import annotations\n\nimport os\n\n\n"
        '@register(os.environ.setdefault("X", "1"))\n'
        'def fixture(x: int = os.environ.pop("X", None)) -> None:\n'
        "    return x\n"
    )
    conftest.write_text(still_eager, encoding="utf-8")
    assert file_violations(conftest) == [
        "6: module-level os.environ write in conftest.py (any key)",
        "7: module-level os.environ write in conftest.py (any key)",
    ]


@pytest.mark.parametrize(
    "source",
    [
        'def fixture(value=environ.pop("X", None)):\n    environ = {}\n    return value\n',
        '@register(environ.pop("X", None))\ndef fixture():\n    environ = {}\n    return 1\n',
        'def fixture(value: environ.pop("X", None)):\n    environ = {}\n    return value\n',
        'def fixture(environ=environ.pop("X", None)):\n    return environ\n',
        'hook = lambda environ=environ.pop("X", None): environ\n',
    ],
    ids=["default", "decorator", "annotation", "shadowing-parameter", "lambda-default"],
)
def test_imported_environ_in_defaults_and_decorators_resolves_in_the_defining_scope(
    tmp_path: Path, source: str
) -> None:
    """A decorator, default or annotation runs in the scope that defines the
    function, so a same-named store in the body (or the parameter the default
    initialises) does not rebind the name it uses.

    Sabotage proof (executed): resolve against the ``FunctionDef`` the name
    sits under → every case is clean and fails; restored.
    """
    conftest = tmp_path / "conftest.py"
    conftest.write_text("from os import environ\n\n\n" + source, encoding="utf-8")
    assert file_violations(conftest) == ["4: module-level os.environ write in conftest.py (any key)"]


def test_imported_environ_kairix_write_in_a_default_is_flagged_despite_a_body_store(tmp_path: Path) -> None:
    source = (
        "from os import environ\n\n\n"
        'def test_x(value=environ.pop("KAIRIX_DB_PATH", None)):\n'
        "    environ = {}\n"
        '    environ["KAIRIX_DB_PATH"] = value\n'
    )
    assert _file_violations(tmp_path, source) == ["4: os.environ.pop(KAIRIX_*)"]


def test_os_alias_resolves_in_its_lexical_scope(tmp_path: Path) -> None:
    """``import os as state`` inside one helper binds ``state`` there only;
    another function's local ``state`` object is its own mapping. A parameter
    named ``os`` shadows the module too. The alias still counts inside the
    function that imports it and at module level.

    Sabotage proof (executed): collect aliases file-wide again → the local
    ``Holder`` case is flagged and this fails; restored.
    """
    source = (
        "import os\n\n\n"
        "def helper():\n"
        "    import os as state\n"
        '    return state.environ.get("X")\n\n\n'
        "def test_local_object():\n"
        "    state = Holder()\n"
        '    state.environ["KAIRIX_DB_PATH"] = "x"\n\n\n'
        "def test_shadowing_parameter(os):\n"
        '    os.environ["KAIRIX_DB_PATH"] = "x"\n\n\n'
        "def test_alias_in_scope():\n"
        "    import os as state\n"
        '    state.environ["KAIRIX_DB_PATH"] = "x"\n\n\n'
        "def test_module_os():\n"
        '    os.environ["KAIRIX_DB_PATH"] = "x"\n'
    )
    assert _file_violations(tmp_path, source) == [
        "20: assign os.environ[KAIRIX_*]",
        "24: assign os.environ[KAIRIX_*]",
    ]


def test_module_dict_environ_replacement_is_flagged_and_reads_are_clean(tmp_path: Path) -> None:
    """``os.__dict__["environ"] = ...`` / ``vars(os)["environ"] = ...`` replace
    the mapping with no ``setattr`` and no audit event, and a restore within
    the phase hides it from the runtime identity check; every such store (and
    ``del``) is a wholesale replacement, a read is not.

    Sabotage proof (executed): drop ``_module_dict_environ`` from the
    wholesale branch → no violations and this fails; restored.
    """
    source = (
        "import os\n\n\n"
        "def test_x():\n"
        '    original = os.__dict__["environ"]\n'
        '    os.__dict__["environ"] = {}\n'
        '    vars(os)["envi" + "ron"] = original\n'
        '    del os.__dict__["environ"]\n'
        '    os.__dict__["sep"] = "/"\n'
        '    state.__dict__["environ"] = {}\n'
    )
    assert _file_violations(tmp_path, source) == [
        "6: os.environ replaced wholesale",
        "7: os.environ replaced wholesale",
        "8: os.environ replaced wholesale",
    ]


def test_helper_imported_at_conftest_import_is_held_to_the_module_level_rule(tmp_path: Path) -> None:
    """A module the conftest imports at module level executes before the
    runtime guard is configured, exactly like the conftest's own top level,
    so its module-level env writes (any key, transitively) fail — reported
    against the conftest's import line. A helper imported inside a function
    is deferred to guarded time and left to the runtime hook.

    Sabotage proof (executed): skip ``_imported_helper_writes`` → nothing is
    flagged and this fails; restored.
    """
    (tmp_path / "helper.py").write_text('import os\n\nos.environ["FAKEPKG_" + "EARLY"] = "1"\n', encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import os\n\n\ndef setup():\n    os.putenv('A', '1')\n", encoding="utf-8")
    (pkg / "sub.py").write_text("from os import environ\n\nfrom . import deeper\n", encoding="utf-8")
    (pkg / "deeper.py").write_text("from os import environ as e\n\ne.update(VALUES)\n", encoding="utf-8")
    (tmp_path / "lazy.py").write_text('import os\n\nos.environ["X"] = "1"\n', encoding="utf-8")
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        "from helper import X\nimport pkg.sub\nimport os\n\n\ndef pytest_configure(config):\n    import lazy\n",
        encoding="utf-8",
    )
    assert file_violations(conftest) == [
        "1: module-level os.environ write in imported helper.py:3 (any key)",
        "2: module-level os.environ write in imported pkg/deeper.py:3 (any key)",
    ]


def test_in_place_environ_union_is_a_key_write_not_a_replacement(tmp_path: Path) -> None:
    """``os.environ |= {...}`` mutates the same mapping (``__ior__``), so it
    is judged by its keys like any other write: a literal ``KAIRIX_*`` key
    fails, other keys are clean in a test (the runtime hook still sees them)
    and still a module-level write in a conftest.

    Sabotage proof (executed): keep ``AugAssign`` in the wholesale branch →
    the other-keys case is flagged and this fails; restored.
    """
    assert _violations(tmp_path, 'os.environ |= {"KAIRIX_DB_PATH": "x"}') == ["5: os.environ |= {KAIRIX_*}"]
    assert _violations(tmp_path, 'environ |= {"OTHER": "1", "KAIRIX_DB_PATH": "x"}') == ["5: os.environ |= {KAIRIX_*}"]
    assert _violations(tmp_path, 'os.environ |= {"OTHER_TEST_KEY": "1"}') == []
    assert _violations(tmp_path, "os.environ |= extra") == []
    conftest = tmp_path / "conftest.py"
    conftest.write_text('import os\n\nos.environ |= {"OTHER_TEST_KEY": "1"}\n', encoding="utf-8")
    assert file_violations(conftest) == ["3: module-level os.environ write in conftest.py (any key)"]


def test_lazy_type_alias_value_is_not_an_import_time_write(tmp_path: Path) -> None:
    """A PEP 695 ``type`` alias value is evaluated only when ``__value__`` is
    read, so the alias statement itself writes nothing at conftest import;
    reading ``__value__`` at module level does run it and is flagged there.

    Sabotage proof (executed): drop the ``ast.TypeAlias`` branch from the
    deferral walk → the alias statement is flagged and this fails; restored.
    """
    conftest = tmp_path / "conftest.py"
    conftest.write_text('import os\n\ntype Deferred = os.environ.pop("X", None)\n', encoding="utf-8")
    assert file_violations(conftest) == []
    conftest.write_text(
        'import os\n\ntype Deferred = os.environ.pop("X", None)\n\n\ndef later():\n    return Deferred.__value__\n\n\n'
        "EAGER = Deferred.__value__\n",
        encoding="utf-8",
    )
    assert file_violations(conftest) == ["10: module-level os.environ write in conftest.py (any key)"]


def test_pytest_plugins_modules_are_held_to_the_module_level_rule(tmp_path: Path) -> None:
    """pytest imports every module named in a conftest's ``pytest_plugins``
    before ``pytest_configure`` installs the runtime hook, so those modules
    are import-time code like an explicit import: a literal list, a bare
    string, ``+=`` and ``.append`` / ``.extend`` all resolve.

    Sabotage proof (executed): accept only ``Import`` / ``ImportFrom`` nodes
    in ``_import_time_modules`` → nothing is flagged and this fails; restored.
    """
    (tmp_path / "plug_a.py").write_text('import os\n\nos.environ["KAIRIX_PLUGIN_EARLY"] = "1"\n', encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "plug_b.py").write_text("import os\n\n\ndef step():\n    os.environ.pop('X', None)\n", encoding="utf-8")
    (tmp_path / "plug_c.py").write_text('from os import putenv\n\nputenv("A", "1")\n', encoding="utf-8")
    (tmp_path / "plug_d.py").write_text("import os\n\nos.environ.update(V)\n", encoding="utf-8")
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        'pytest_plugins = ["plug_a", "pkg.plug_b"]\n'
        'pytest_plugins += ["plug_c"]\n'
        "if FLAG:\n"
        '    pytest_plugins.append("plug_d")\n',
        encoding="utf-8",
    )
    assert file_violations(conftest) == [
        "1: module-level os.environ write in imported plug_a.py:3 (any key)",
        "2: module-level os.environ write in imported plug_c.py:3 (any key)",
        "4: module-level os.environ write in imported plug_d.py:3 (any key)",
    ]
    conftest.write_text('pytest_plugins = "plug_a"\n', encoding="utf-8")
    assert file_violations(conftest) == ["1: module-level os.environ write in imported plug_a.py:3 (any key)"]


def test_class_bodies_are_binding_scopes_but_not_closures(tmp_path: Path) -> None:
    """A class body that rebinds ``os`` (or imports its own alias) writes its
    own mapping; a method does not see the class namespace, so a name bound
    there is not ``os`` inside the method. Module ``os`` used in a class body
    is still the process env — and in a conftest, a class body runs at import.

    Sabotage proof (executed): drop ``ast.ClassDef`` from the scope kinds →
    the ``Holder`` case is flagged and this fails; restored.
    """
    source = (
        "import os\n\n\n"
        "class Holder:\n"
        "    os = Fake()\n"
        '    os.environ["KAIRIX_DB_PATH"] = "x"\n\n\n'
        "class Writes:\n"
        '    os.environ["KAIRIX_DB_PATH"] = "x"\n\n\n'
        "class WithAlias:\n"
        "    import os as state\n\n"
        '    state.environ["KAIRIX_DB_PATH"] = "x"\n\n'
        "    def method(self):\n"
        '        state.environ["KAIRIX_DB_PATH"] = "x"\n\n'
        "    def uses_module_os(self):\n"
        '        os.environ["KAIRIX_DB_PATH"] = "x"\n'
    )
    assert _file_violations(tmp_path, source) == [
        "10: assign os.environ[KAIRIX_*]",
        "16: assign os.environ[KAIRIX_*]",
        "22: assign os.environ[KAIRIX_*]",
    ]
    conftest = tmp_path / "conftest.py"
    conftest.write_text('import os\n\n\nclass Early:\n    os.environ["X"] = "1"\n', encoding="utf-8")
    assert file_violations(conftest) == ["5: module-level os.environ write in conftest.py (any key)"]


@pytest.mark.parametrize(
    "statement",
    [
        'os.environ["X"], value = "1", 2',
        'first, *os.environ["X"] = 1, 2, 3',
        'for os.environ["X"] in ["1"]:\n    pass',
        '[0 for os.environ["X"] in ["1"]]',
        'with ctx() as os.environ["X"]:\n    pass',
        "for os.environ in [{}]:\n    pass",
    ],
    ids=["tuple", "starred", "for", "comprehension", "with", "for-wholesale"],
)
def test_compound_and_loop_store_targets_in_a_conftest_are_flagged(tmp_path: Path, statement: str) -> None:
    """A store reaches ``os.environ`` through a tuple / starred unpacking, a
    ``for`` target, a comprehension target or a ``with ... as`` target just as
    through a plain assignment.

    Sabotage proof (executed): inspect only the outer target node → every
    case is clean and fails; restored.
    """
    conftest = tmp_path / "conftest.py"
    conftest.write_text(f"import os\n\n{statement}\n", encoding="utf-8")
    assert "3: module-level os.environ write in conftest.py (any key)" in file_violations(conftest)


def test_compound_targets_on_other_mappings_are_clean_and_kairix_targets_are_shaped(tmp_path: Path) -> None:
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        'import os\n\nmapping["X"], value = "1", 2\nfor mapping["X"] in ["1"]:\n    pass\n', encoding="utf-8"
    )
    assert file_violations(conftest) == []
    assert _violations(tmp_path, 'os.environ["KAIRIX_DB_PATH"], value = "x", 2') == ["5: assign os.environ[KAIRIX_*]"]
    assert _violations(tmp_path, 'for os.environ["KAIRIX_DB_PATH"] in ["x"]:\n    pass') == [
        "5: assign os.environ[KAIRIX_*]"
    ]
    assert _violations(tmp_path, "for os.environ in [{}]:\n    pass") == ["5: os.environ replaced wholesale"]


@pytest.mark.parametrize(
    "statement",
    [
        'setattr(os, "environ")',
        'delattr(os, "environ", None)',
        'setattr(os, "environ", {}, extra)',
        'setattr(os, "environ", value={})',
    ],
    ids=["setattr-short", "delattr-long", "setattr-long", "setattr-keyword"],
)
def test_malformed_setattr_calls_are_clean(tmp_path: Path, statement: str) -> None:
    """A ``setattr`` / ``delattr`` with the wrong arity raises ``TypeError``
    before touching ``os``; only a well-formed call replaces the mapping.

    Sabotage proof (executed): accept ``len(args) >= 2`` again → every case
    is flagged and this fails; restored.
    """
    assert _violations(tmp_path, statement) == []


def test_guard_exemption_covers_only_the_marked_restore_inside_a_function(tmp_path: Path) -> None:
    """The runtime guard's exemption is the one ``F2-RESTORE``-marked
    assignment in a function body; any other wholesale replacement in the
    guard source — module level or not — is a violation, and the marker
    exempts nothing in any other file.

    Sabotage proof (executed): restore the file-wide ``_REPLACED`` filter →
    the inserted module-level replacement is accepted and this fails;
    restored.
    """
    real_guard = _REPO_ROOT / "tests" / "fixtures" / "process_state_guard.py"
    source = real_guard.read_text(encoding="utf-8")
    assert source.count(_GUARD_RESTORE_MARKER) == 1
    copy = tmp_path / "process_state_guard.py"
    copy.write_text(source, encoding="utf-8")
    assert file_violations(copy, guard_home=copy) == []
    assert file_violations(copy) == ["412: os.environ replaced wholesale"]
    extra = source + '\nos.environ = {}\nsetattr(os, "environ", {})\nos.__dict__["environ"] = {}\n'
    copy.write_text(extra, encoding="utf-8")
    lines = extra.count("\n")
    assert file_violations(copy, guard_home=copy) == [
        f"{lines - 2}: os.environ replaced wholesale",
        f"{lines - 1}: os.environ replaced wholesale",
        f"{lines}: os.environ replaced wholesale",
    ]
    marked_at_module_level = f"import os\n\nos.environ = {{}}  # {_GUARD_RESTORE_MARKER}\n"
    copy.write_text(marked_at_module_level, encoding="utf-8")
    assert file_violations(copy, guard_home=copy) == ["3: os.environ replaced wholesale"]
    other = tmp_path / "test_other.py"
    other.write_text(
        f"import os\n\n\ndef test_x():\n    os.environ = {{}}  # {_GUARD_RESTORE_MARKER}\n", encoding="utf-8"
    )
    assert file_violations(other) == ["5: os.environ replaced wholesale"]


def test_class_body_shadowing_counts_only_after_the_store_executes(tmp_path: Path) -> None:
    """A class body runs top to bottom: a write before ``os = Fake()`` still
    resolves the module ``os``; one after it writes the fake's mapping.

    Sabotage proof (executed): use the whole class's store set again → the
    write before the store is clean and this fails; restored.
    """
    source = (
        "import os\n\n\n"
        "class Early:\n"
        '    os.environ["KAIRIX_DB_PATH"] = "x"\n'
        "    os = Fake()\n"
        '    os.environ["KAIRIX_DB_PATH"] = "x"\n'
    )
    assert _file_violations(tmp_path, source) == ["5: assign os.environ[KAIRIX_*]"]
    conftest = tmp_path / "conftest.py"
    conftest.write_text(source.replace("KAIRIX_DB_PATH", "X"), encoding="utf-8")
    assert file_violations(conftest) == ["5: module-level os.environ write in conftest.py (any key)"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('os = FakeState()\nos.environ["KAIRIX_DB_PATH"] = "1"\n', []),
        ('import os\nos = FakeState()\n\n\ndef test_x():\n    os.environ["KAIRIX_DB_PATH"] = "1"\n', []),
        (
            'import os\nos.environ["KAIRIX_DB_PATH"] = "1"\nos = FakeState()\nos.environ["KAIRIX_DB_PATH"] = "1"\n',
            ["2: assign os.environ[KAIRIX_*]"],
        ),
        ('os = FakeState()\nimport os\nos.environ["KAIRIX_DB_PATH"] = "1"\n', ["3: assign os.environ[KAIRIX_*]"]),
        (
            'import os.path\n\n\ndef test_x():\n    os.environ["KAIRIX_DB_PATH"] = "1"\n',
            ["5: assign os.environ[KAIRIX_*]"],
        ),
        ('import os.path as p\n\n\ndef test_x():\n    p.environ["KAIRIX_DB_PATH"] = "1"\n', []),
        ('from fakes import os\n\n\ndef test_x():\n    os.environ["KAIRIX_DB_PATH"] = "1"\n', []),
        ('def os():\n    pass\n\n\nos.environ["KAIRIX_DB_PATH"] = "1"\n', []),
    ],
    ids=[
        "no-import",
        "rebound-before-function-runs",
        "rebound-after-direct-write",
        "import-after-store",
        "import-os-path-binds-os",
        "os-path-alias-is-not-os",
        "os-from-elsewhere",
        "def-named-os",
    ],
)
def test_module_level_os_binding_comes_from_a_real_import_in_execution_order(
    tmp_path: Path, source: str, expected: list[str]
) -> None:
    """``os`` is the module only through a live ``import os`` / ``import
    os.<sub>`` binding: with no import, or after a module-level rebinding, a
    bare ``os`` is somebody's object. Module code is sequential for a direct
    use; a function sees the module's last binding.

    Sabotage proof (executed): restore the synthetic ``{"os": {None}}``
    binding → the no-import case is flagged and this fails; restored.
    """
    assert _file_violations(tmp_path, source) == expected


def test_statically_dead_conftest_branches_are_not_import_time(tmp_path: Path) -> None:
    """``if False:`` / ``if TYPE_CHECKING:`` bodies, the dead arm of a
    constant ``if`` / conditional expression, a ``while False:`` body and the
    short-circuited operands of ``False and`` / ``True or`` never execute, so
    neither their writes nor their imports run at conftest import.

    Sabotage proof (executed): drop ``_in_dead_branch`` from
    ``_runs_at_import`` → every dead write is flagged and this fails; restored.
    """
    (tmp_path / "helper.py").write_text('import os\n\nos.environ["EARLY"] = "1"\n', encoding="utf-8")
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        "import os\nimport typing\nfrom typing import TYPE_CHECKING\n\n"
        "if False:\n"
        '    os.environ["A"] = "1"\n'
        "if TYPE_CHECKING:\n"
        "    from helper import X\n"
        "if typing.TYPE_CHECKING:\n"
        '    os.environ["B"] = "1"\n'
        "while False:\n"
        '    os.environ["C"] = "1"\n'
        'VALUE = 1 if True else os.environ.pop("D", None)\n'
        'False and os.environ.pop("E", None)\n'
        'True or os.environ.pop("F", None)\n'
        "if True:\n"
        "    pass\n"
        "else:\n"
        '    os.environ["G"] = "1"\n'
        "if not False:\n"
        '    os.environ["H"] = "1"\n'
        "if FLAG:\n"
        '    os.environ["I"] = "1"\n'
        'os.environ.pop("J", None) or True\n',
        encoding="utf-8",
    )
    assert file_violations(conftest) == [
        "21: module-level os.environ write in conftest.py (any key)",
        "23: module-level os.environ write in conftest.py (any key)",
        "24: module-level os.environ write in conftest.py (any key)",
    ]


def test_type_checking_is_dead_only_through_the_typing_binding(tmp_path: Path) -> None:
    """Only the real ``typing.TYPE_CHECKING`` (imported name or attribute of
    the imported module) is statically false; a local ``TYPE_CHECKING = True``
    or another object's ``TYPE_CHECKING`` attribute is reachable.

    Sabotage proof (executed): judge ``TYPE_CHECKING`` by spelling again →
    the local-name case is clean and this fails; restored.
    """
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        "import os\nimport typing\nimport typing as t\nfrom typing import TYPE_CHECKING\n\n"
        'if TYPE_CHECKING:\n    os.environ["A"] = "1"\n'
        'if typing.TYPE_CHECKING:\n    os.environ["B"] = "1"\n'
        'if t.TYPE_CHECKING:\n    os.environ["C"] = "1"\n'
        "TYPE_CHECKING = True\n"
        'if TYPE_CHECKING:\n    os.environ["D"] = "1"\n'
        'if flags.TYPE_CHECKING:\n    os.environ["E"] = "1"\n',
        encoding="utf-8",
    )
    assert file_violations(conftest) == [
        "14: module-level os.environ write in conftest.py (any key)",
        "16: module-level os.environ write in conftest.py (any key)",
    ]
    conftest.write_text(
        'import os\n\nTYPE_CHECKING = False\nif TYPE_CHECKING:\n    os.environ["F"] = "1"\n', encoding="utf-8"
    )
    assert file_violations(conftest) == ["5: module-level os.environ write in conftest.py (any key)"]


def test_main_guard_bodies_do_not_run_on_import(tmp_path: Path) -> None:
    """``if __name__ == "__main__":`` (either spelling) never runs when a
    conftest or a helper is imported; its ``else`` and ``!=`` form do.

    Sabotage proof (executed): drop the ``__name__`` comparison from
    ``_static_truth`` → the guarded writes are flagged and this fails;
    restored.
    """
    (tmp_path / "helper.py").write_text(
        'import os\n\nif __name__ == "__main__":\n    os.environ["OTHER"] = "1"\n', encoding="utf-8"
    )
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        "import os\nimport helper\n\n"
        'if __name__ == "__main__":\n    os.environ["A"] = "1"\n'
        'if "__main__" == __name__:\n    os.environ["B"] = "1"\nelse:\n    os.environ["C"] = "1"\n'
        'if __name__ != "__main__":\n    os.environ["D"] = "1"\n',
        encoding="utf-8",
    )
    assert file_violations(conftest) == [
        "9: module-level os.environ write in conftest.py (any key)",
        "11: module-level os.environ write in conftest.py (any key)",
    ]


def test_shadowed_builtin_helpers_are_not_the_builtins(tmp_path: Path) -> None:
    """``setattr`` / ``delattr`` / ``vars`` count only through the real
    builtins — the bare name with no local binding, or ``builtins.<name>``;
    a parameter or local function of that name is somebody's callback.

    Sabotage proof (executed): match the helper names by spelling again →
    the parameter case is flagged and this fails; restored.
    """
    source = (
        "import builtins\nimport os\n\n\n"
        "def test_parameter(setattr):\n"
        '    setattr(os, "environ", {})\n\n\n'
        "def test_local():\n"
        "    def vars(obj):\n"
        "        return {}\n\n"
        '    vars(os)["environ"] = {}\n\n\n'
        "def test_real():\n"
        '    setattr(os, "environ", {})\n'
        '    builtins.delattr(os, "environ")\n'
    )
    assert _file_violations(tmp_path, source) == [
        "17: os.environ replaced wholesale",
        "18: os.environ replaced wholesale",
    ]


def test_class_local_pytest_plugins_is_not_a_registration(tmp_path: Path) -> None:
    """Only the module-level ``pytest_plugins`` is read by pytest; a class
    attribute of that name registers nothing, so its modules are not walked.

    Sabotage proof (executed): accept any ``pytest_plugins`` target → the
    class-local case walks ``pkg.plug`` and this fails; restored.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "plug.py").write_text('import os\n\nos.environ["X"] = "1"\n', encoding="utf-8")
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        'class Metadata:\n    pytest_plugins = "pkg.plug"\n\n'
        '    def add(self):\n        pytest_plugins.append("pkg.plug")\n',
        encoding="utf-8",
    )
    assert file_violations(conftest) == []
    conftest.write_text('pytest_plugins = "pkg.plug"\n', encoding="utf-8")
    assert file_violations(conftest) == ["1: module-level os.environ write in imported pkg/plug.py:3 (any key)"]


def test_docs_state_the_static_check_is_a_pre_screen_and_the_runtime_guard_the_boundary() -> None:
    """§F2 says the static conftest import-time check is a best-effort
    pre-screen over listed common shapes and that the runtime process-state
    guard is the enforced boundary for anything it cannot model, so the
    static pass is never mistaken for the complete check.

    Sabotage proof (executed): delete the paragraph from the doc → this
    fails; restored.
    """
    doc = (Path(__file__).resolve().parents[2] / "docs" / "architecture" / "fitness-functions.md").read_text(
        encoding="utf-8"
    )
    flat = " ".join(doc.split())
    assert "best-effort pre-screen, not the boundary" in flat
    assert "is the enforced boundary for anything the static pass cannot model" in flat
    assert "tests/fixtures/process_state_guard.py" in flat.split("best-effort pre-screen")[1]
