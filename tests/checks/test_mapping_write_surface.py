"""Table-driven coverage of the shared F1 / F2 mapping-write engine.

``scripts/checks/_mapping_writes.py`` is the one place F1 (``sys.modules``,
kairix module keys) and F2 (``os.environ``, ``KAIRIX_*`` keys) decide whether
a statement writes a process-global mapping. Review rounds kept finding new
SPELLINGS of the same write, so this module enumerates the whole class
systematically:

    every FORM  x  every RECEIVER SPELLING  x  positional / keyword call style
    (+ a NEGATIVE of the same form on an ordinary dict / object)

for both receivers, driven through each detector's public surface
(``file_violations`` for F2, ``file_has_internal_patch`` for F1).

Sabotage proofs (executed — mutate, confirm red, restore, confirm green):
  * resolver: make ``ProcessMapping.resolve`` skip the alias fixpoint →
    every ``local_alias`` case fails; make ``ProcessMapping.is_receiver``
    ignore the ``<module>.<attr>`` attribute form → every ``direct`` and
    ``import_alias`` case fails;
  * binder: make ``bind_call`` ignore keywords → every ``keyword`` case fails;
    make it ignore positionals → every ``positional`` case fails.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

from check_no_env_monkeypatch import file_violations  # noqa: E402 — see _CHECKS_DIR sys.path insert above
from check_no_internal_patches import file_has_internal_patch  # noqa: E402 — see _CHECKS_DIR sys.path insert above

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class Receiver:
    """One guarded process-global mapping and the detector that guards it."""

    module: str
    attr: str
    key: str  # a protected key, as Python source
    flagged: Callable[[Path], bool]

    @property
    def dotted(self) -> str:
        return f"{self.module}.{self.attr}"


RECEIVERS = {
    "os.environ": Receiver("os", "environ", '"KAIRIX_DB_PATH"', lambda path: bool(file_violations(path))),
    "sys.modules": Receiver("sys", "modules", '"kairix.paths"', file_has_internal_patch),
}

# How the mapping itself is spelled: (import header, setup lines, receiver expr).
RECEIVER_SPELLINGS = {
    "direct": ("import {m}", "", "{m}.{a}"),
    "import_alias": ("import {m} as _mod", "", "_mod.{a}"),
    "from_import": ("from {m} import {a} as _mapping", "", "_mapping"),
    "local_alias": ("import {m}", "first = {m}.{a}\n    second = first", "second"),
}

# How the owning module is spelled, for whole-mapping replacement forms.
MODULE_SPELLINGS = {
    "direct": ("import {m}", "", "{m}"),
    "import_alias": ("import {m} as _mod", "", "_mod"),
    "local_alias": ("import {m}", "alias_mod = {m}", "alias_mod"),
}

# (form, positional statement, keyword statement or None). {R}=receiver, {K}=key.
MAPPING_FORMS: list[tuple[str, str, str | None]] = [
    ("subscript_assign", '{R}[{K}] = "v"', None),
    ("subscript_augassign", '{R}[{K}] += "v"', None),
    ("subscript_del", "del {R}[{K}]", None),
    ("ior", '{R} |= {{{K}: "v"}}', None),
    ("update", '{R}.update({{{K}: "v"}})', '{R}.update(**{{{K}: "v"}})'),
    ("setdefault", '{R}.setdefault({K}, "v")', '{R}.setdefault(key={K}, default="v")'),
    ("pop", "{R}.pop({K}, None)", "{R}.pop(key={K})"),
    ("dunder_setitem", '{R}.__setitem__({K}, "v")', '{R}.__setitem__(key={K}, value="v")'),
    ("dunder_delitem", "{R}.__delitem__({K})", "{R}.__delitem__(key={K})"),
    ("dunder_ior", '{R}.__ior__({{{K}: "v"}})', None),
    ("clear", "{R}.clear()", None),
    ("popitem", "{R}.popitem()", None),
    ("monkeypatch_setitem", 'monkeypatch.setitem({R}, {K}, "v")', 'monkeypatch.setitem(dic={R}, name={K}, value="v")'),
    ("monkeypatch_delitem", "monkeypatch.delitem({R}, {K})", "monkeypatch.delitem(dic={R}, name={K})"),
    ("patch_dict_values", 'patch.dict({R}, {{{K}: "v"}})', 'patch.dict(in_dict={R}, values={{{K}: "v"}})'),
    ("patch_dict_clear", "patch.dict({R}, clear=True)", "patch.dict(in_dict={R}, clear=True)"),
]

# (form, positional statement, keyword statement or None). {M}=module, {A}=attr, {D}=dotted.
REPLACEMENT_FORMS: list[tuple[str, str, str | None]] = [
    (
        "monkeypatch_setattr_pair",
        'monkeypatch.setattr({M}, "{A}", {{}})',
        'monkeypatch.setattr(target={M}, name="{A}", value={{}})',
    ),
    ("monkeypatch_setattr_dotted", 'monkeypatch.setattr("{D}", {{}})', 'monkeypatch.setattr(target="{D}", name={{}})'),
    ("monkeypatch_delattr_pair", 'monkeypatch.delattr({M}, "{A}")', 'monkeypatch.delattr(target={M}, name="{A}")'),
    ("monkeypatch_delattr_dotted", 'monkeypatch.delattr("{D}")', 'monkeypatch.delattr(target="{D}")'),
    ("patch_object", 'patch.object({M}, "{A}", {{}})', 'patch.object(target={M}, attribute="{A}", new={{}})'),
    ("patch_dotted", 'patch("{D}", {{}})', 'patch(target="{D}", new={{}})'),
    ("builtin_setattr", 'setattr({M}, "{A}", {{}})', None),
    ("builtin_delattr", 'delattr({M}, "{A}")', None),
    ("attribute_assign", "{M}.{A} = {{}}", None),
    ("attribute_del", "del {M}.{A}", None),
]


def _module_source(header: str, setup: str, statement: str) -> str:
    body = f"    {setup}\n" if setup else ""
    return (
        f"{header}\nfrom types import SimpleNamespace\nfrom unittest import mock\nfrom unittest.mock import patch\n\n\n"
        + f"def test_x(monkeypatch):\n{body}    {statement}\n"
    )


def _flagged(tmp_path: Path, receiver: Receiver, source: str) -> bool:
    path = tmp_path / "test_sample.py"
    compile(source, str(path), "exec")  # a malformed sample must fail loudly, never read as "clean"
    path.write_text(source, encoding="utf-8")
    return receiver.flagged(path)


def _mapping_cases() -> list[object]:
    cases = []
    for rname in RECEIVERS:
        for form, positional, keyword in MAPPING_FORMS:
            for spelling in RECEIVER_SPELLINGS:
                for style, template in (("positional", positional), ("keyword", keyword)):
                    if template is not None:
                        cases.append(pytest.param(rname, spelling, template, id=f"{rname}-{form}-{spelling}-{style}"))
    return cases


def _replacement_cases() -> list[object]:
    cases = []
    for rname in RECEIVERS:
        for form, positional, keyword in REPLACEMENT_FORMS:
            for spelling in MODULE_SPELLINGS:
                for style, template in (("positional", positional), ("keyword", keyword)):
                    if template is not None:
                        cases.append(pytest.param(rname, spelling, template, id=f"{rname}-{form}-{spelling}-{style}"))
    return cases


@pytest.mark.parametrize(("rname", "spelling", "template"), _mapping_cases())
def test_mapping_write_is_flagged(tmp_path: Path, rname: str, spelling: str, template: str) -> None:
    """Every MutableMapping write form, through every receiver spelling and
    call style, touching a protected key is reported."""
    receiver = RECEIVERS[rname]
    header, setup, expr = (part.format(m=receiver.module, a=receiver.attr) for part in RECEIVER_SPELLINGS[spelling])
    statement = template.format(R=expr, K=receiver.key)
    assert _flagged(tmp_path, receiver, _module_source(header, setup, statement)) is True


@pytest.mark.parametrize(("rname", "form", "template"), [
    pytest.param(rname, form, tpl, id=f"{rname}-{form}-{style}")
    for rname in RECEIVERS
    for form, pos, kw in MAPPING_FORMS
    for style, tpl in (("positional", pos), ("keyword", kw))
    if tpl is not None
])  # fmt: skip
def test_same_mapping_write_on_an_ordinary_dict_is_not_flagged(
    tmp_path: Path, rname: str, form: str, template: str
) -> None:
    """The identical statement on a plain local dict is not a process write."""
    receiver = RECEIVERS[rname]
    statement = template.format(R="plain", K=receiver.key)
    source = _module_source(f"import {receiver.module}", "plain = {}", statement)
    assert _flagged(tmp_path, receiver, source) is False, form


@pytest.mark.parametrize(("rname", "spelling", "template"), _replacement_cases())
def test_mapping_replacement_is_flagged(tmp_path: Path, rname: str, spelling: str, template: str) -> None:
    """Replacing / deleting the whole mapping, through every module spelling
    and call style (both monkeypatch overloads, patch, patch.object, builtins)."""
    receiver = RECEIVERS[rname]
    header, setup, expr = (part.format(m=receiver.module) for part in MODULE_SPELLINGS[spelling])
    statement = template.format(M=expr, A=receiver.attr, D=receiver.dotted)
    assert _flagged(tmp_path, receiver, _module_source(header, setup, statement)) is True


@pytest.mark.parametrize(("rname", "form", "template"), [
    pytest.param(rname, form, tpl, id=f"{rname}-{form}-{style}")
    for rname in RECEIVERS
    for form, pos, kw in REPLACEMENT_FORMS
    for style, tpl in (("positional", pos), ("keyword", kw))
    if tpl is not None
])  # fmt: skip
def test_same_replacement_on_an_ordinary_object_is_not_flagged(
    tmp_path: Path, rname: str, form: str, template: str
) -> None:
    """Replacing an attribute of an ordinary object (or a non-guarded dotted
    path) is not a write to the guarded mapping."""
    receiver = RECEIVERS[rname]
    statement = template.format(M="holder", A=receiver.attr, D="json.dumps")
    source = _module_source(f"import {receiver.module}", "holder = SimpleNamespace()", statement)
    assert _flagged(tmp_path, receiver, source) is False, form


# ---------------------------------------------------------------------------
# os.environ-only helpers + MonkeyPatch owner spellings.
# ---------------------------------------------------------------------------

_ENV = RECEIVERS["os.environ"]

MONKEYPATCH_OWNERS = {
    "fixture": ("", "monkeypatch"),
    "instance": ("mp_instance = pytest.MonkeyPatch()", "mp_instance"),
    "instance_alias": ("mp_instance = pytest.MonkeyPatch()\n    mp_alias = mp_instance", "mp_alias"),
}


@pytest.mark.parametrize("owner", list(MONKEYPATCH_OWNERS))
@pytest.mark.parametrize(
    "template",
    [
        'OWNER.setenv("KAIRIX_DB_PATH", "v")',
        'OWNER.setenv(name="KAIRIX_DB_PATH", value="v")',
        'OWNER.delenv("KAIRIX_DB_PATH")',
        'OWNER.delenv(name="KAIRIX_DB_PATH", raising=False)',
        'OWNER.setitem(os.environ, "KAIRIX_DB_PATH", "v")',
        'OWNER.setattr(os, "environ", {})',
        'OWNER.setattr(target="os.environ", name={})',
    ],
)
def test_monkeypatch_env_writes_through_every_owner_spelling_are_flagged(
    tmp_path: Path, owner: str, template: str
) -> None:
    """``setenv`` / ``delenv`` / ``setitem`` / ``setattr`` resolve whether the
    MonkeyPatch is the fixture, a ``pytest.MonkeyPatch()`` instance, or an alias."""
    setup, name = MONKEYPATCH_OWNERS[owner]
    source = _module_source("import os\nimport pytest", setup, template.replace("OWNER", name))
    assert _flagged(tmp_path, _ENV, source) is True


def test_monkeypatch_context_target_is_resolved(tmp_path: Path) -> None:
    """``with pytest.MonkeyPatch.context() as mp:`` — ``mp`` is a MonkeyPatch."""
    source = (
        "import pytest\n\n\ndef test_x():\n"
        + "    with pytest.MonkeyPatch.context() as mp:\n"
        + '        mp.setenv("KAIRIX_DB_PATH", "v")\n'
    )
    assert _flagged(tmp_path, _ENV, source) is True


@pytest.mark.parametrize(
    "template",
    [
        'monkeypatch.setenv("PATH", "v")',
        'monkeypatch.delenv(name="HOME")',
        'plain.update(other={"KAIRIX_DB_PATH": "v"})',
        'os.environ.update(other="v")',
    ],
)
def test_env_negatives_are_not_flagged(tmp_path: Path, template: str) -> None:
    """Non-KAIRIX names and ``update(other=...)`` (a ``.setenv`` on ANY receiver is
    classified by method name — see ``test_mapping_default_deny.py``)
    (``other`` is positional-only, so that writes the harmless key ``"other"``)."""
    source = _module_source("import os", "plain = {}\n    other = object()", template)
    assert _flagged(tmp_path, _ENV, source) is False


# ---------------------------------------------------------------------------
# sys.modules-only: importlib.reload by signature.
# ---------------------------------------------------------------------------

_MODS = RECEIVERS["sys.modules"]


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimport kairix.paths\nimportlib.reload(kairix.paths)\n",
        "import importlib\nimport kairix.paths\nimportlib.reload(module=kairix.paths)\n",
        "import importlib as il\nimport kairix.paths as kp\nil.reload(module=kp)\n",
        "from importlib import reload as again\nimport kairix.paths\nagain(module=kairix.paths)\n",
        'from importlib import import_module as load, reload\nreload(module=load(name="kairix.paths"))\n',
    ],
)
def test_reload_of_kairix_module_is_flagged_by_signature(tmp_path: Path, source: str) -> None:
    """``importlib.reload(module)`` bound positionally or by keyword, through aliases."""
    path = tmp_path / "test_sample.py"
    path.write_text(source, encoding="utf-8")
    assert _MODS.flagged(path) is True


def test_reload_of_ordinary_module_by_keyword_is_not_flagged(tmp_path: Path) -> None:
    path = tmp_path / "test_sample.py"
    path.write_text("import importlib\nimport json\nimportlib.reload(module=json)\n", encoding="utf-8")
    assert _MODS.flagged(path) is False


@pytest.mark.parametrize(
    "source",
    [
        'import importlib\nmodule = importlib.import_module("kairix.paths")\nimportlib.reload(module)\n',
        'import importlib\nimport sys\nmodule = sys.modules["kairix.paths"]\nimportlib.reload(module)\n',
        "import importlib\nimport kairix.paths\nmodule = kairix.paths\nimportlib.reload(module)\n",
        'import importlib\nfirst = importlib.import_module("kairix.paths")\nsecond = first\nimportlib.reload(second)\n',
        'import importlib\nmodule = importlib.import_module("kairix.paths")\nmodule.provider_name = None\n',
    ],
    ids=["import_module", "sys_modules", "attribute", "chain", "attribute_assign"],
)
def test_locally_bound_kairix_module_is_resolved(tmp_path: Path, source: str) -> None:
    """A local name bound to a kairix module (``import_module``,
    ``sys.modules[...]``, an imported module, or a chain of names) is a kairix
    module ref — reloading or patching it is F1.

    Sabotage proof (executed): make ``_bind_local_module_aliases`` a no-op →
    every case reports clean; restored.
    """
    path = tmp_path / "test_sample.py"
    path.write_text(source, encoding="utf-8")
    assert _MODS.flagged(path) is True


@pytest.mark.parametrize(
    "source",
    [
        'import importlib\nmodule = importlib.import_module("json")\nimportlib.reload(module)\n',
        'import importlib\nimport sys\nmodule = sys.modules["openai"]\nimportlib.reload(module)\n',
    ],
    ids=["import_module_json", "sys_modules_openai"],
)
def test_locally_bound_external_module_reload_is_not_flagged(tmp_path: Path, source: str) -> None:
    """Reloading a locally bound stdlib / third-party module stays allowed."""
    path = tmp_path / "test_sample.py"
    path.write_text(source, encoding="utf-8")
    assert _MODS.flagged(path) is False


@pytest.mark.parametrize(
    "source",
    [
        'import importlib\nimportlib.reload(importlib.import_module("kai" + "rix.paths"))\n',
        'from unittest.mock import patch\npatch("kai" + "rix.paths.provider_name").start()\n',
        'def test_x(monkeypatch):\n    monkeypatch.setattr("kai" + "rix.paths.provider_name", None)\n',
        'def test_x(monkeypatch):\n    monkeypatch.delattr("kai" + "rix.paths.provider_name")\n',
    ],
    ids=["reload", "patch", "setattr", "delattr"],
)
def test_folded_kairix_name_without_the_kairix_token_is_flagged(tmp_path: Path, source: str) -> None:
    """The F1 prefilter keys on call / receiver tokens, never on the
    ``kairix`` name alone — a file that only ASSEMBLES the name by constant
    folding is still parsed, and its folded string targets resolve.

    Sabotage proof (executed): narrow ``_PREFILTER`` back to
    ``kairix|modules`` → every case reports clean (the file is never
    parsed); restored. (Folding itself is not what keeps these red — under
    default-deny an unfoldable target fails too.)
    """
    assert "kairix" not in source
    path = tmp_path / "test_sample.py"
    path.write_text(source, encoding="utf-8")
    assert _MODS.flagged(path) is True


# ---------------------------------------------------------------------------
# Spread-hidden targets / keys (PR #814 thread): an argument a ``*args`` /
# ``**kw`` spread leaves unbound counts as possibly the guarded mapping / a
# protected key — the documented binding contract.
# ---------------------------------------------------------------------------

# (form, setup building the spread from the receiver R / module M, call)
SPREAD_FORMS: list[tuple[str, str, str]] = [
    ("setitem_args", 'args = ({R}, {K}, "v")', "monkeypatch.setitem(*args)"),
    ("setitem_kwargs", 'kw = {{"dic": {R}, "name": {K}, "value": "v"}}', "monkeypatch.setitem(**kw)"),
    ("delitem_args", "args = ({R}, {K})", "monkeypatch.delitem(*args)"),
    ("delitem_kwargs", 'kw = {{"dic": {R}, "name": {K}}}', "monkeypatch.delitem(**kw)"),
    ("patch_dict_args", 'args = ({R}, {{{K}: "v"}})', "patch.dict(*args)"),
    ("patch_dict_kwargs", 'kw = {{"in_dict": {R}, "values": {{{K}: "v"}}}}', "patch.dict(**kw)"),
    ("setattr_args", 'args = ({M}, "{A}", {{}})', "monkeypatch.setattr(*args)"),
    ("setattr_kwargs", 'kw = {{"target": {M}, "name": "{A}", "value": {{}}}}', "monkeypatch.setattr(**kw)"),
    ("delattr_args", 'args = ({M}, "{A}")', "monkeypatch.delattr(*args)"),
    ("update_args", 'args = ({{{K}: "v"}},)', "{R}.update(*args)"),
    ("pop_args", "args = ({K},)", "{R}.pop(*args)"),
]

# setenv / delenv only exist for os.environ.
ENV_SPREAD_FORMS: list[tuple[str, str, str]] = [
    ("setenv_args", 'args = ({K}, "v")', "monkeypatch.setenv(*args)"),
    ("setenv_kwargs", 'kw = {{"name": {K}, "value": "v"}}', "monkeypatch.setenv(**kw)"),
    ("delenv_args", "args = ({K},)", "monkeypatch.delenv(*args)"),
    ("delenv_kwargs", 'kw = {{"name": {K}}}', "monkeypatch.delenv(**kw)"),
]


def _spread_cases() -> list[object]:
    cases = [
        pytest.param(rname, setup, call, id=f"{rname}-{form}")
        for rname in RECEIVERS
        for form, setup, call in SPREAD_FORMS
    ]
    cases += [
        pytest.param("os.environ", setup, call, id=f"os.environ-{form}") for form, setup, call in ENV_SPREAD_FORMS
    ]
    return cases


@pytest.mark.parametrize(("rname", "setup", "call"), _spread_cases())
def test_spread_hidden_target_or_key_is_flagged(tmp_path: Path, rname: str, setup: str, call: str) -> None:
    """``monkeypatch.setitem(*args)``, ``patch.dict(**kw)``, ... — the target /
    key is unbound behind a spread, so it counts as possibly protected.

    Under default-deny the spread's own source already puts the mapping in a
    non-allow-listed context (a tuple / dict literal), so these stay red even
    without the spread rule. Sabotage proof (executed): make
    ``MappingGuard.classify`` treat unknown contexts as reads AND
    ``BoundCall.has_spread`` return ``False`` (and the alias ban off, since the
    spread sources bind the mapping into a tuple / dict) → 18 of the 26 cases
    report clean; restored.
    """
    receiver = RECEIVERS[rname]
    fmt = {"R": receiver.dotted, "K": receiver.key, "M": receiver.module, "A": receiver.attr}
    source = _module_source(f"import {receiver.module}", setup.format(**fmt), call.format(**fmt))
    assert _flagged(tmp_path, receiver, source) is True


@pytest.mark.parametrize(
    "call",
    [
        "monkeypatch.setitem(plain, *rest)",
        "monkeypatch.delitem(plain, **kw)",
        "patch.dict(plain, *rest)",
        "monkeypatch.setattr(holder, *rest)",
    ],
)
@pytest.mark.parametrize("rname", list(RECEIVERS))
def test_spread_with_an_ordinary_target_bound_is_not_flagged(tmp_path: Path, rname: str, call: str) -> None:
    """When the target IS bound (to an ordinary dict / object), a spread
    supplying the remaining arguments cannot make it the guarded mapping."""
    receiver = RECEIVERS[rname]
    setup = 'plain = {}\n    holder = SimpleNamespace()\n    rest = ("x", "y")\n    kw = {"name": "x"}'
    source = _module_source(f"import {receiver.module}", setup, call)
    assert _flagged(tmp_path, receiver, source) is False


# ---------------------------------------------------------------------------
# Constant folding of keys (PR #814 thread).
# ---------------------------------------------------------------------------

FOLDING_CASES: list[tuple[str, str, bool]] = [
    ("os.environ", '"KAIRIX" + "_DB_PATH"', True),
    ("os.environ", "f\"KAIRIX{'_X'}\"", True),
    ("os.environ", '"KAIRIX_%s" % "DB_PATH"', True),
    ("os.environ", '"{}_DB_PATH".format("KAIRIX")', True),
    ("os.environ", '"KAIRIX" + suffix', True),
    ("os.environ", 'f"KAIRIX{suffix}"', True),
    ("os.environ", '"KAIRIX" + "DB_PATH"', False),
    ("os.environ", '"X" + "KAIRIX_A"', False),
    ("os.environ", "f\"{'PATH'}\"", False),
    ("os.environ", '"PATH" + suffix', False),
    ("os.environ", '"%s_DB" % "OTHER"', False),
    ("sys.modules", '"kai" + "rix.paths"', True),
    ("sys.modules", '"kairix" + "." + "paths"', True),
    ("sys.modules", '"kairix" + suffix', True),
    ("sys.modules", '"kairixish" + ".paths"', False),
    ("sys.modules", '"open" + "ai"', False),
]


@pytest.mark.parametrize(
    ("rname", "key_expr", "expected"),
    [pytest.param(r, k, e, id=f"{r}-{k}") for r, k, e in FOLDING_CASES],
)
def test_constant_folded_keys(tmp_path: Path, rname: str, key_expr: str, expected: bool) -> None:
    """Constant string expressions are folded before the protected-prefix
    test; a partially constant one is judged by its constant leading prefix.

    Sabotage proof (executed): make ``fold_string`` fold bare literals only
    (``None`` for ``+`` / f-strings / ``%`` / ``.format``) → the
    seven folded positives (``"KAIRIX" + "_DB_PATH"``, the f-string, ``%``,
    ``.format``, ``"kai" + "rix.paths"``, ...) report clean; restored.
    """
    receiver = RECEIVERS[rname]
    statement = f'{receiver.dotted}[{key_expr}] = "v"'
    source = _module_source(f"import {receiver.module}", "suffix = input()", statement)
    assert _flagged(tmp_path, receiver, source) is expected
