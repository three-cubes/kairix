"""Default-deny model for F1 / F2: every reference to the guarded mapping must be
an allow-listed READ or a PROVABLY safe write — anything else fails.

``scripts/checks/_mapping_writes.py`` (``MappingGuard``) classifies the parent
context of EVERY reference to ``os.environ`` (F2) / ``sys.modules`` (F1).
Instead of enumerating write spellings, an unknown context, an unresolved key
or an unfoldable target is a violation. This module pins:

* the allow-listed reads stay clean (for both mappings);
* provably safe writes (keys that resolve statically to non-protected values,
  incl. via parameters, loops and constant containers) stay clean;
* the default-deny property — an arbitrary helper call ``some_helper(R)``,
  returning / yielding ``R``, comparing it, putting it in a container — fails;
* the six spellings a review round found against the old denylist;
* the one remaining exemption (the session baseline) covers only statements
  directly in the fixture body — nested callbacks inherit nothing.

Sabotage proofs (executed — mutate, confirm red, restore, confirm green):
  * make ``MappingGuard.classify`` treat an unknown context as a read
    (``return []`` instead of the default violation) → the cases that reach
    the default verdict (the ``==`` comparisons; the rest are caught by the
    alias ban or a specific handler) report clean;
  * drop the ``ast.Compare`` / ``ast.For`` read handlers from
    ``_CONTEXT_HANDLERS`` → the matching allow-listed reads are flagged.
"""

from __future__ import annotations

import re
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

_HEADER = "import importlib\nimport os\nimport subprocess\nimport sys\nfrom unittest.mock import patch\n\n\n"


def _f2(path: Path) -> bool:
    return bool(file_violations(path))


DETECTORS: dict[str, tuple[str, str, Callable[[Path], bool]]] = {
    # name: (receiver expr, a protected key, flags-the-file)
    "os.environ": ("os.environ", '"KAIRIX_DB_PATH"', _f2),
    "sys.modules": ("sys.modules", '"kairix.paths"', file_has_internal_patch),
}


def _flagged(tmp_path: Path, rname: str, body: str, name: str = "test_sample.py") -> bool:
    receiver, key, flagged = DETECTORS[rname]
    path = tmp_path / name
    source = _HEADER + re.sub(r"\bKEY\b", key, re.sub(r"\bR\b", receiver, body))
    compile(source, str(path), "exec")  # a malformed sample must fail loudly, never read as "clean"
    path.write_text(source, encoding="utf-8")
    return flagged(path)


def _in_test(*lines: str) -> str:
    return "def test_x(monkeypatch, stub):\n" + "".join(f"    {line}\n" for line in lines)


# ---------------------------------------------------------------------------
# Allow-listed reads stay clean.
# ---------------------------------------------------------------------------

COMMON_READS = [
    'value = R["PATH"]',
    'value = R.get("PATH")',
    "value = R.get(KEY)",
    "snapshot = R.copy()",
    "pairs = list(R.items())",
    "names = R.keys()",
    "values = R.values()",
    'present = R.__contains__("PATH")',
    "present = KEY in R",
    "absent = KEY not in R",
    "count = len(R)",
    "snapshot = dict(R)",
    "snapshot = {**R}",
    "for name in R:\n        pass",
    "names = [name for name in R]",
]
ENV_ONLY_READS = [
    'subprocess.run(["true"], env=R, check=False)',
    'subprocess.run(["true"], env=dict(R, EXTRA="1"), check=False)',
    'os.execve("/bin/true", ["true"], env=R)',
]


@pytest.mark.parametrize(
    ("rname", "statement"),
    [pytest.param(r, s, id=f"{r}-{s.splitlines()[0]}") for r in DETECTORS for s in COMMON_READS]
    + [pytest.param("os.environ", s, id=f"os.environ-{s}") for s in ENV_ONLY_READS],
)
def test_allow_listed_reads_are_clean(tmp_path: Path, rname: str, statement: str) -> None:
    """Every allow-listed READ context of the mapping stays clean."""
    assert _flagged(tmp_path, rname, _in_test(statement)) is False


# ---------------------------------------------------------------------------
# Provably safe writes stay clean; unresolved ones fail.
# ---------------------------------------------------------------------------

SAFE_WRITES = {
    "os.environ": [
        'R["PATH"] = "x"',
        'R.pop("HOME", None)',
        'R.update({"PATH": "x"})',
        'monkeypatch.setitem(R, "PATH", "x")',
        'monkeypatch.setenv("PATH", "x")',
        'patch.dict(R, {"PATH": "x"})',
        'NAMES = ("PATH", "HOME")\n    for name in NAMES:\n        R.pop(name, None)',
        'NAMES = ("PATH", "HOME")\n    R[NAMES[1]] = "x"',
    ],
    "sys.modules": [
        'R["openai"] = stub',
        'R.pop("jwt", None)',
        'monkeypatch.setitem(R, "jwt", stub)',
        'monkeypatch.delitem(R, "jwt", raising=False)',
        'patch.dict(R, {"openai": stub})',
        'NAMES = ("jwt", "openai")\n    for name in NAMES:\n        R.pop(name, None)',
    ],
}


@pytest.mark.parametrize(
    ("rname", "statement"),
    [pytest.param(r, s, id=f"{r}-{s.splitlines()[0]}") for r, rows in SAFE_WRITES.items() for s in rows],
)
def test_provably_safe_writes_are_clean(tmp_path: Path, rname: str, statement: str) -> None:
    """A write whose key resolves statically to a non-protected value passes."""
    assert _flagged(tmp_path, rname, _in_test(statement)) is False


@pytest.mark.parametrize("rname", list(DETECTORS))
def test_unresolved_key_is_protected(tmp_path: Path, rname: str) -> None:
    """A key that cannot be resolved (a parameter with no call site) counts as
    protected under default-deny."""
    assert _flagged(tmp_path, rname, "def test_x(name, stub):\n    R[name] = stub\n") is True


@pytest.mark.parametrize(
    ("rname", "argument", "expected"),
    [
        ("os.environ", '"PATH"', False),
        ("os.environ", "KEY", True),
        ("sys.modules", '"openai"', False),
        ("sys.modules", "KEY", True),
    ],
)
def test_parameter_key_resolves_through_its_call_sites(
    tmp_path: Path, rname: str, argument: str, expected: bool
) -> None:
    """A helper parameter used as the key resolves to the union of the
    arguments its (non-escaping) call sites pass.

    Sabotage proof (executed): make ``ConstantTable._param_values`` return
    ``None`` → the ``"PATH"`` / ``"openai"`` cases are flagged; restored.
    """
    body = "def _put(name, stub):\n    R[name] = stub\n\n\n" + _in_test(f"_put({argument}, stub)")
    assert _flagged(tmp_path, rname, body) is expected


# ---------------------------------------------------------------------------
# The default-deny property.
# ---------------------------------------------------------------------------

UNKNOWN_CONTEXTS = [
    "some_helper(R)",
    "some_helper(mapping=R)",
    "print(R)",
    "return R",
    "yield R",
    "items = [R]",
    "same = R == {}",
    "R.something_new()",
    "method = R.pop",
    "a, b = R, None",
]


@pytest.mark.parametrize(
    ("rname", "statement"),
    [pytest.param(r, s, id=f"{r}-{s}") for r in DETECTORS for s in UNKNOWN_CONTEXTS],
)
def test_any_use_outside_the_allow_list_is_flagged(tmp_path: Path, rname: str, statement: str) -> None:
    """``some_helper(os.environ)`` and every other unknown context fail — the
    detector does not need to know what the helper does.

    Sabotage proof (executed): make ``MappingGuard.classify`` treat an unknown
    context as a read → the ``==`` comparison cases report clean (the others
    are also caught by the alias ban or a specific handler); restored.
    """
    assert _flagged(tmp_path, rname, "def some_helper(*args, **kwargs):\n    pass\n\n\n" + _in_test(statement)) is True


# ---------------------------------------------------------------------------
# The six spellings the last review round found against the denylist.
# ---------------------------------------------------------------------------


def test_reload_bound_to_a_local_name_is_flagged(tmp_path: Path) -> None:
    """``r = importlib.reload; r(m)`` — any reference to ``reload`` other than a
    direct call with a provably non-kairix module fails.

    Sabotage proof (executed): turn off BOTH the alias ban and the reload
    rule for non-direct references → reports clean; restored.
    """
    body = "import json\n\n\n" + _in_test("r = importlib.reload", "r(json)")
    assert _flagged(tmp_path, "sys.modules", body) is True


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("json", False),
        ('importlib.import_module("json")', False),
        ("module", True),
        ('sys.modules["kairix.paths"]', True),
    ],
)
def test_reload_argument_must_be_provably_external(tmp_path: Path, argument: str, expected: bool) -> None:
    """``importlib.reload(x)`` passes only when ``x`` is PROVABLY a non-kairix
    module; an unresolved argument (a parameter) fails."""
    body = "import json\n\n\ndef test_x(module):\n    importlib.reload(" + argument + ")\n"
    assert _flagged(tmp_path, "sys.modules", body) is expected


@pytest.mark.parametrize(
    ("rname", "statement"),
    [
        ("os.environ", 'patch("os." + "environ", {})'),
        ("os.environ", 'monkeypatch.setattr("o" + "s.environ", {})'),
        ("os.environ", 'patch.dict("os.en" + "viron", {KEY: "1"})'),
        ("os.environ", 'setattr(os, "envi" + "ron", {})'),
        ("sys.modules", 'patch("sys." + "modules", {})'),
        ("sys.modules", 'monkeypatch.setattr(sys, "mod" + "ules", {})'),
    ],
)
def test_dotted_mapping_target_is_constant_folded(tmp_path: Path, rname: str, statement: str) -> None:
    """A dotted / attribute-name target that folds to the mapping is a write to it."""
    assert _flagged(tmp_path, rname, _in_test(statement)) is True


@pytest.mark.parametrize(
    ("rname", "statement"),
    [
        ("sys.modules", "sys.modules, other = {}, None"),
        ("os.environ", 'os.environ[KEY], other = "a", "b"'),
        ("os.environ", '[os.environ[KEY]] = ["a"]'),
        ("os.environ", 'first, *os.environ[KEY] = ["a", "b"]'),
        ("sys.modules", "[sys.modules[KEY], other] = [stub, None]"),
    ],
)
def test_unpacking_targets_are_writes(tmp_path: Path, rname: str, statement: str) -> None:
    """A store nested in a tuple / list / starred unpack is classified by its
    own context — a protected subscript store, or a replacement, fails.

    Sabotage proof (executed): make ``_subscript_use`` accept Store contexts
    → the subscript-unpack cases report clean; restored.
    """
    assert _flagged(tmp_path, rname, _in_test(statement)) is True


@pytest.mark.parametrize(
    ("rname", "keys_literal", "expected"),
    [
        ("os.environ", '("KAIRIX_DB_PATH", "PATH")', True),
        ("os.environ", '("PATH", "HOME")', False),
        ("sys.modules", '("kairix.paths", "json")', True),
        ("sys.modules", '("openai", "json")', False),
    ],
)
def test_constant_container_indexing_resolves(tmp_path: Path, rname: str, keys_literal: str, expected: bool) -> None:
    """``KEYS[0]`` folds through the constant table; an index into a container
    holding a protected value fails, an all-safe container passes."""
    body = f"KEYS = {keys_literal}\n\n\n" + _in_test("R[KEYS[0]] = stub", "R.pop(KEYS[1], None)")
    assert _flagged(tmp_path, rname, body) is expected


# ---------------------------------------------------------------------------
# Tightened exemptions (F2).
# ---------------------------------------------------------------------------

_SESSION_FIXTURE = """import pytest


@pytest.fixture(scope="session", autouse=True)
def _baseline():
    monkeypatch = pytest.MonkeyPatch()
{body}
    yield
    monkeypatch.undo()
"""


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('    monkeypatch.setenv("KAIRIX_CONNECT_DISABLE_BROWSER", "1")', False),
        ('    for name in ("KAIRIX_DATA_DIR",):\n        monkeypatch.delenv(name, raising=False)', False),
        ('    def _callback():\n        monkeypatch.setenv("KAIRIX_X", "1")\n\n    yield _callback', True),
        ('    _callback = lambda: monkeypatch.setenv("KAIRIX_X", "1")', True),
    ],
    ids=["direct", "loop", "nested-def-callback", "lambda-callback"],
)
def test_session_exemption_covers_only_the_fixture_body(tmp_path: Path, body: str, expected: bool) -> None:
    """Only statements DIRECTLY in the session fixture's body are exempt — a
    nested function, lambda or yielded callback inherits nothing.

    Sabotage proof (executed): resolve the exemption scope with
    ``enclosing_function`` chained outward (the old behaviour) → the callback
    cases report clean; restored.
    """
    path = tmp_path / "conftest.py"
    path.write_text(_SESSION_FIXTURE.format(body=body), encoding="utf-8")
    assert bool(file_violations(path)) is expected


# ---------------------------------------------------------------------------
# Rule (a): the guarded MODULE objects (``os`` / ``sys``) are default-deny too.
# ---------------------------------------------------------------------------

MODULE_CASES: list[tuple[str, str, bool]] = [
    # (detector, statement, expected flagged)
    ("os.environ", "some_helper(os)", True),
    ("sys.modules", "some_helper(sys)", True),
    ("sys.modules", "names = vars(sys)", True),
    ("os.environ", "names = vars(os)", True),
    ("os.environ", "value = getattr(os, name)", True),
    ("os.environ", 'value = getattr(os, "environ")', True),
    ("sys.modules", "items = [sys]", True),
    ("os.environ", 'path = os.path.join("a", "b")', False),
    ("os.environ", "cwd = os.getcwd()", False),
    ("sys.modules", "argv = sys.argv", False),
    ("sys.modules", 'sys.argv = ["kairix", "--help"]', False),
    ("os.environ", 'monkeypatch.setattr(os, "chown", stub)', False),
    ("sys.modules", 'frozen = getattr(sys, "frozen", False)', False),
    ("sys.modules", 'monkeypatch.setattr(sys, "argv", ["x"])', False),
    ("os.environ", 'alias = os\nvalue = alias.environ.get("PATH")', True),
]


@pytest.mark.parametrize(
    ("rname", "statement", "expected"),
    [pytest.param(r, s, e, id=f"{r}-{s.splitlines()[0]}") for r, s, e in MODULE_CASES],
)
def test_guarded_module_object_is_default_deny(tmp_path: Path, rname: str, statement: str, expected: bool) -> None:
    """``os`` / ``sys`` themselves are guarded: a static attribute access is
    fine, but passing the module to ANY callable fails unless the call has no
    spread and an attribute-name argument provably names something other than
    ``environ`` / ``modules``.

    Sabotage proof (executed): make ``MappingGuard.classify_module`` return
    ``[]`` → every ``True`` case except the alias row (the alias ban) reports clean; restored.
    """
    body = "def some_helper(*args, **kwargs):\n    pass\n\n\ndef test_x(monkeypatch, stub, name):\n"
    body += "".join(f"    {line}\n" for line in statement.splitlines())
    assert _flagged(tmp_path, rname, body) is expected


# ---------------------------------------------------------------------------
# Rule (b): MonkeyPatch helper methods are matched by METHOD NAME on any receiver.
# ---------------------------------------------------------------------------

HELPER_METHOD_CASES: list[tuple[str, str, bool]] = [
    ("os.environ", "anything.setenv(name, stub)", True),
    ("os.environ", 'anything.setenv("KAIRIX_DB_PATH", stub)', True),
    ("os.environ", 'anything.delenv("KAIRIX_DB_PATH")', True),
    ("os.environ", 'anything.setenv("PATH", stub)', False),
    ("sys.modules", 'anything.setattr("kairix.paths.provider_name", stub)', True),
    ("sys.modules", "anything.delattr(name)", True),
    ("sys.modules", 'anything.setattr("os.path.exists", stub)', False),
    ("sys.modules", 'anything.setitem(sys.modules, "kairix.paths", stub)', True),
    ("sys.modules", 'anything.setitem(sys.modules, "openai", stub)', False),
]


@pytest.mark.parametrize(
    ("rname", "statement", "expected"),
    [pytest.param(r, s, e, id=f"{r}-{s}") for r, s, e in HELPER_METHOD_CASES],
)
def test_helper_methods_are_classified_on_any_receiver(
    tmp_path: Path, rname: str, statement: str, expected: bool
) -> None:
    """``.setenv`` / ``.delenv`` / ``.setitem`` / ``.delitem`` / ``.setattr`` /
    ``.delattr`` are classified by method name and bound arguments whatever the
    receiver is — an unresolved key / target fails.

    Sabotage proof (executed): require the receiver to be the name
    ``monkeypatch`` again (in ``MappingGuard._helper_writes`` /
    ``_argument_use`` and F1 ``_monkeypatch_attr_shapes``) → the ``True``
    cases report clean; restored.
    """
    body = "def test_x(anything, stub, name):\n" + f"    {statement}\n"
    assert _flagged(tmp_path, rname, body) is expected


# ---------------------------------------------------------------------------
# The four spellings Codex found at 3479015.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rname", "body"),
    [
        (
            "os.environ",
            "from builtins import getattr as fetch\n\n\n"
            + 'def test_x():\n    fetch(os, "environ")["KAIRIX_DB_PATH"] = "v"\n',
        ),
        ("os.environ", 'def test_x(monkeypatch, x):\n    monkeypatch.setattr(os, "environ", **{"value": x})\n'),
        ("os.environ", 'import pytest\n\n\ndef test_x():\n    pytest.MonkeyPatch().setenv("KAIRIX_DB_PATH", "v")\n'),
        (
            "sys.modules",
            "import pytest\n\n\n"
            + 'def test_x():\n    pytest.MonkeyPatch().setattr("kairix.paths.provider_name", None)\n',
        ),
    ],
    ids=["aliased-getattr", "spread-value", "inline-instance-setenv", "inline-instance-setattr"],
)
def test_codex_receiver_side_spellings_are_flagged(tmp_path: Path, rname: str, body: str) -> None:
    """Closed by the two rules, not per spelling: the module object passed to
    any callable (a), and helper methods on any receiver (b)."""
    assert _flagged(tmp_path, rname, body) is True


# ---------------------------------------------------------------------------
# Aliasing a guarded object is itself a violation (no alias chasing).
# ---------------------------------------------------------------------------

#: (detector, the guarded object as a direct expression)
GUARDED_OBJECTS: list[tuple[str, str]] = [
    ("os.environ", "os.environ"),
    ("os.environ", "os"),
    ("os.environ", "os.environ.get"),
    ("os.environ", "monkeypatch.setenv"),
    ("os.environ", "getattr"),
    ("sys.modules", "sys.modules"),
    ("sys.modules", "sys"),
    ("sys.modules", "sys.modules.pop"),
    ("sys.modules", "importlib"),
    ("sys.modules", "importlib.reload"),
    ("sys.modules", "monkeypatch.setattr"),
]

#: binding forms, ``G`` = the guarded object
BINDING_FORMS: dict[str, str] = {
    "assignment": "alias = G",
    "annotated": "alias: object = G",
    "walrus": "if (alias := G):\n        pass",
    "default-argument": "def inner(alias=G):\n        return None",
    "for-target": "for alias in (G,):\n        pass",
    "with-target": "with G as alias:\n        pass",
    "tuple-unpacking": "alias, other = G, None",
    "conditional": "alias = G if stub else None",
    "return": "return G",
    "yield": "yield G",
    "lambda": "inner = lambda: G",
}


@pytest.mark.parametrize(
    ("rname", "obj", "form"),
    [pytest.param(r, o, f, id=f"{o}-{f}") for r, o in GUARDED_OBJECTS for f in BINDING_FORMS],
)
def test_aliasing_a_guarded_object_is_forbidden(tmp_path: Path, rname: str, obj: str, form: str) -> None:
    """Binding a guarded object to a name, by any binding form, fails — guarded
    objects are only ever used in the direct form, so nothing is chased.

    Sabotage proof (executed): make ``MappingGuard.alias_findings`` return
    ``[]`` → the cases with no other violating use report clean; restored.
    """
    statement = BINDING_FORMS[form].replace("G", obj)
    body = "def test_x(monkeypatch, stub):\n" + "".join(f"    {line}\n" for line in statement.splitlines())
    path = tmp_path / "test_sample.py"
    header = "import importlib\nimport os\nimport sys\n\n\n"
    source = header + body + "\n# environ modules setenv setattr reload\n"
    compile(source, str(path), "exec")  # a malformed sample must fail loudly
    path.write_text(source, encoding="utf-8")
    assert DETECTORS[rname][2](path) is True


@pytest.mark.parametrize("copy_expr", ["dict(os.environ)", "os.environ.copy()", "{**os.environ}"])
def test_binding_a_copy_is_allowed(tmp_path: Path, copy_expr: str) -> None:
    """A COPY is a different object — binding it is fine."""
    assert _flagged(tmp_path, "os.environ", _in_test(f"snapshot = {copy_expr}")) is False


# ---------------------------------------------------------------------------
# The five Codex items at a6409d6.
# ---------------------------------------------------------------------------


def test_unrelated_local_named_like_an_alias_is_not_tainted(tmp_path: Path) -> None:
    """The false positive: ``env = os.environ`` in one test must not make an
    unrelated ``env = {}; env["KAIRIX_X"] = 1`` in another test a violation.
    Only the aliasing line itself is reported.

    Sabotage proof (executed): re-introduce the receiver alias fixpoint in
    ``ProcessMapping.resolve`` → line 10 is reported too; restored.
    """
    source = """import os


def test_a():
    env = os.environ
    return None


def test_b():
    env = {}
    env["KAIRIX_X"] = 1
"""
    path = tmp_path / "test_sample.py"
    path.write_text(source, encoding="utf-8")
    assert file_violations(path) == ["5: aliases os.environ (assignment)"]


def test_module_passed_to_an_arbitrary_helper_with_a_harmless_string_is_flagged(tmp_path: Path) -> None:
    """``mutate(os, "path")`` — no "next argument is a harmless string"
    exemption: only builtin getattr / hasattr and ``<x>.setattr`` / ``.delattr``
    / ``patch.object`` may receive the module, with a static attribute name.

    Sabotage proof (executed): accept any call whose next argument is a static
    non-environ string (the old rule) → reports clean; restored.
    """
    body = "def mutate(module, name):\n    pass\n\n\n" + _in_test('mutate(os, "path")')
    assert _flagged(tmp_path, "os.environ", body) is True


def test_importlib_module_bound_to_a_name_is_flagged(tmp_path: Path) -> None:
    """``loader = importlib; loader.reload(kairix.paths)`` — binding the
    importlib module is itself a violation."""
    body = "import kairix.paths\n\n\n" + _in_test("loader = importlib", "loader.reload(kairix.paths)")
    assert _flagged(tmp_path, "sys.modules", body) is True


def test_bound_setenv_helper_is_flagged(tmp_path: Path) -> None:
    """``setter = monkeypatch.setenv; setter("KAIRIX_X", v)`` — binding the helper
    method is itself a violation."""
    body = _in_test("setter = monkeypatch.setenv", 'setter("KAIRIX_X", "v")')
    assert _flagged(tmp_path, "os.environ", body) is True
