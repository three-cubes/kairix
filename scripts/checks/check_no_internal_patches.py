"""F1 detector: flag tests that substitute kairix-internal implementations.

Walks every test file via AST and reports the path of any file that
matches one of eight shapes:

1. ``patch("kairix.X.Y", ...)`` — decorator, ``with``, or ``.start()``;
   ``target`` bound by signature (``patch(target="kairix.X.Y")`` counts)
2. ``patch.object(<kairix ref>, "attr", ...)`` — positional or keyword
3. ``kairix.X.Y = <expr>`` — full-path attribute assignment
4. ``<alias>.Y = <expr>`` where ``<alias>`` resolves to a kairix module
5. ``<monkeypatch>.setattr / delattr("kairix.X.Y", ...)`` — string-target form
6. ``<monkeypatch>.setattr / delattr(<kairix module ref>, "attr", ...)`` —
   ref-target form. ``<monkeypatch>`` is the fixture, any
   ``pytest.MonkeyPatch()`` instance, or a ``MonkeyPatch.context()`` target;
   arguments are bound by signature (``target=`` / ``name=``).
7. any reference to ``sys.modules`` outside the read allow-list (DEFAULT-DENY,
   the shared ``_mapping_writes.MappingGuard``) unless it is a write PROVEN to
   touch only non-kairix keys. Historically enumerated as: the full
   ``MutableMapping`` mutation surface of the shared engine
   ``_mapping_writes`` (subscript assign / augassign / del, ``|=``,
   ``__setitem__`` / ``__delitem__`` / ``pop`` / ``setdefault`` /
   ``update`` / ``__ior__``, ``clear()`` / ``popitem()`` always,
   ``<monkeypatch>.setitem / delitem``, ``patch.dict(in_dict, values,
   clear)``, and replacing ``sys.modules`` wholesale via ``setattr`` /
   ``patch.object`` / ``patch("sys.modules")`` / assignment), through any
   receiver spelling (``import sys as s``, ``from sys import modules``,
   ``mods = sys.modules; m2 = mods``). Replacing (or evicting, then
   re-importing) a module object substitutes the whole kairix
   implementation — the same anti-pattern as ``@patch``, one level up.
8. any reference to ``importlib.reload`` — called or aliased — unless it is
   a direct call whose ``module`` is PROVABLY a non-kairix module (DEFAULT-DENY:
   an unresolved argument fails), through ``importlib`` / ``reload`` /
   ``import_module`` aliases.
   Re-executes module-level code to reset hidden singleton state, so the
   test passes against a module object no production process ever sees.
   Inject the state holder instead.

Third-party ``sys.modules`` entries (``sys.modules["openai"] = stub``) stay
allowed — that fakes a genuinely external import at the kairix edge.

Stdlib roots (``os``, ``time``, ``pathlib``, ``sys``, ``importlib``,
``builtins``, ``threading``, ``functools``, ``re``, ``json``,
``logging``, ...) and external SDK roots (``httpx``, ``openai``,
``boto3``, ``anthropic``, ``requests``, ``numpy``, ``neo4j``,
``usearch``, ``sentence_transformers``, ``spacy``, ``rich``,
``click``, ``unittest``, ``pytest``) are exempt — patching these is
fixturing genuinely external state at the kairix edge.

To extend with a new shape: add the detection branch to
``file_has_internal_patch`` (or, for a new mapping-write spelling, to the
shared ``_mapping_writes`` engine) and add the matching positive + negative
rows to ``tests/architecture/test_check_no_internal_patches.py`` /
``tests/checks/test_mapping_write_surface.py``.

Output: one violation file path per line on stdout, sorted,
deduplicated. Pipes into ``arch_gate`` from ``_lib.sh``, which fails on
any path.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import re

from _ast_key_taint import UNKNOWN, VALUE, ConstantTable, ModuleIndex, ProtectedKeys, fold_string, parse_index
from _fitness_rule import FitnessRule
from _mapping_writes import (
    BUILTIN_GUARDED_NAMES,
    IMPORT_MODULE_SIGNATURE,
    MONKEYPATCH_SIGNATURES,
    PATCH_OBJECT_SIGNATURE,
    PATCH_SIGNATURE,
    RELOAD_SIGNATURE,
    GuardedName,
    MappingGuard,
    ProcessMapping,
    bind_call,
    guarded_rebinds,
)

REMEDIATION = """kairix-internal substitution found in a test (@patch / monkeypatch.setattr /
attribute assignment on a kairix target, a sys.modules swap of a kairix
module, importlib.reload of one, any reference to builtin __import__, or a
rebinding of a guarded name). Refactor to constructor injection
with a fake from tests/fakes.py to pass.

fix: rewrite the test to construct the unit under test with a Fake*
from tests/fakes.py (e.g. ``SearchPipeline(retriever=FakeRetriever(...))``).
If the production class lacks a constructor seam, add one — same shape
as ``GoldBuilder(llm_judge=, retriever=, db_path=)``.

When production resolves dependencies via function-local imports at
call time, move that resolution to construction time via the existing
``*Deps`` dataclass with ``default_factory`` — see
``EmbedDependencies`` / ``LLMBackendDeps`` / ``BenchmarkDeps`` for the
canonical shape, then inject the Fake* at construction.

A ``sys.modules`` swap / ``importlib.reload`` usually resets module-level
singleton state or simulates an import failure. Move that state onto an
injectable holder (``CrossEncoderCache`` in kairix/core/search/rerank.py)
or the import onto a Deps seam (``PackageInitDeps.import_module`` in
kairix/package_meta.py) and pass a fresh instance / failing importer.
For "importing X has no side effects", import X in a fresh interpreter
(``subprocess.run([sys.executable, "-c", "import X"])``) instead of
reloading it in the shared test process.

Import normally, or call ``importlib.import_module`` with a static name —
never ``__import__`` (by name, ``builtins.__import__``, or a string that
folds to it). To simulate an optional dependency that is not installed, put
``None`` in ``sys.modules`` for it: ``monkeypatch.setitem(sys.modules,
"yaml", None)`` makes ``import yaml`` raise ImportError and is undone
automatically. Never rebind ``sys`` / ``importlib`` / ``modules`` /
``reload`` / ``getattr`` / ``setattr`` / ``delattr`` / ``__import__`` at
module level or in a class body (or alongside its import in a function).

next: re-run ``python3 scripts/checks/check_no_internal_patches.py``
to confirm the gate goes green. The gate is DEFAULT-DENY for
``sys.modules``, ``importlib.reload`` and dotted patch targets: a reference
must be an allow-listed read (``sys.modules.get`` / ``in`` / subscript load /
iteration) or a write / reload / target PROVEN non-kairix; an unresolved key,
module or target counts as kairix. Make a genuinely external name a literal /
constant the gate can prove, or inject the dependency through a seam rather
than reshaping the patch.
run: bash scripts/safe-commit.sh "refactor(<area>): inject Fake via DI seam"

Pass example:
  pipeline = SearchPipeline(retriever=FakeRetriever(hits=[...]))
  assert pipeline.run(query='x') == ...
  cache = CrossEncoderCache()
  assert get_cross_encoder("m", cache=cache) is None
  monkeypatch.setitem(sys.modules, "yaml", None)   # yaml "not installed"

Forbidden example:
  Shapes that fire the gate (all eight are the same anti-pattern):
  @patch('kairix.core.search.bm25.bm25_search')
  with patch('kairix.providers.get_provider'):
  kairix.paths.provider_name = lambda: "fake"
  paths_mod.provider_name = lambda: "fake"
  monkeypatch.setattr("kairix.paths.provider_name", ...)
  monkeypatch.setattr(kairix.paths, "provider_name", ...)
  with pytest.MonkeyPatch.context() as mp: mp.setattr(check_mod, "run_all_checks", fake)
  with mock.patch.object(warm_cli, "run_warm", return_value=result):
  sys.modules["kairix.core.search.pipeline"] = BrokenModule(...)
  mods = sys.modules; mods.update(fakes)
  importlib.reload(kairix.core.search.rerank)   # or reload(module=...)
  mod = __import__("kairix.paths")
  monkeypatch.setattr(builtins, "__import__", blocking_import)
  sys = FakeSys()   # module-level rebind of a guarded name

Stdlib boundaries (os.*, time.*, etc.) and external SDK boundaries
(httpx.*, openai.*, boto3.*, etc.) remain allowed — F1 only flags
kairix.* targets."""


# Exempt module roots — stdlib and external SDKs whose patching is a
# legitimate boundary fake, not an internals violation.
_EXEMPT_ROOTS = frozenset(
    {
        # Stdlib
        "os",
        "sys",
        "time",
        "pathlib",
        "importlib",
        "builtins",
        "threading",
        "functools",
        "re",
        "json",
        "logging",
        "asyncio",
        "subprocess",
        "shutil",
        "tempfile",
        "datetime",
        "collections",
        "io",
        "ast",
        "typing",
        "contextlib",
        "warnings",
        "uuid",
        "hashlib",
        "hmac",
        "secrets",
        "struct",
        "copy",
        "itertools",
        # External SDKs
        "httpx",
        "openai",
        "boto3",
        "anthropic",
        "requests",
        "numpy",
        "yaml",
        "ruamel",
        "neo4j",
        "usearch",
        "sentence_transformers",
        "spacy",
        "rich",
        "click",
        # Testing infra
        "unittest",
        "pytest",
        "mock",
    }
)


def _resolve_kairix_aliases(index: ModuleIndex) -> dict[str, str]:
    """Map local name -> fully-qualified kairix path from the file's imports.

    Examples:
      ``import kairix.paths as paths_mod`` -> {"paths_mod": "kairix.paths"}
      ``from kairix import providers as p`` -> {"p": "kairix.providers"}
      ``from kairix.paths import provider_name`` -> {"provider_name": "kairix.paths.provider_name"}
      ``import kairix.paths`` -> {"kairix": "kairix"}  (root binding only)
    """
    aliases: dict[str, str] = {}
    for node in [*index.imports, *index.import_froms]:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith("kairix"):
                    continue
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    # `import kairix.paths` binds `kairix` (not `kairix.paths`)
                    # in the local namespace; track that so `kairix.paths.X = ...`
                    # is detected.
                    aliases[alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod != "kairix" and not mod.startswith("kairix."):
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                # `from kairix.paths import provider_name`
                #   -> {"provider_name": "kairix.paths.provider_name"}
                # `from kairix import providers as p`
                #   -> {"p": "kairix.providers"}
                aliases[local] = f"{mod}.{alias.name}" if mod else f"kairix.{alias.name}"
    return aliases


def _attribute_root_name(node: ast.expr) -> str | None:
    """For an Attribute chain like ``a.b.c``, return ``"a"`` (the leftmost Name)."""
    cur = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id
    return None


def _resolves_to_kairix(expr: ast.expr, aliases: dict[str, str]) -> bool:
    """Does ``expr`` (a Name or Attribute) resolve to a kairix module?

    True when:
      - ``expr`` is a Name whose id is in the alias map and points at
        a kairix qualified path
      - ``expr`` is an Attribute whose root is the literal name ``kairix``
        or an alias of one
    """
    if isinstance(expr, ast.Name):
        if expr.id == "kairix":
            return True
        return expr.id in aliases and aliases[expr.id].startswith("kairix")
    if isinstance(expr, ast.Attribute):
        root = _attribute_root_name(expr)
        if root is None:
            return False
        if root == "kairix":
            return True
        if root in _EXEMPT_ROOTS:
            return False
        return root in aliases and aliases[root].startswith("kairix")
    return False


def _is_inside_pytest_raises(parent_map: dict[ast.AST, ast.AST], node: ast.AST) -> bool:
    """Return True iff ``node`` is lexically inside a ``with pytest.raises(...):`` block.

    Assigning to a kairix module attribute *inside* ``pytest.raises`` is a
    contract test — the test is asserting the assignment raises (e.g.
    ``FrozenInstanceError`` on a frozen dataclass). That is the opposite
    of monkey-patching: the test is pinning that the patch path is
    blocked. Skip those Assigns.
    """
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, ast.With):
            for item in current.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call):
                    func = ctx.func
                    # pytest.raises(...) — match by attribute name; conservative.
                    if isinstance(func, ast.Attribute) and func.attr == "raises":
                        return True
                    if isinstance(func, ast.Name) and func.id == "raises":
                        return True
        current = parent_map.get(current)
    return False


_KAIRIX_MODULES = ProtectedKeys(prefixes=("kairix.",), exact=("kairix",))

#: Cheap pre-parse filter on the CALL / RECEIVER tokens every F1 shape needs —
#: never on the kairix NAME alone, which constant folding can assemble
#: (``"kai" + "rix.paths"``): a ``kairix`` import or string, ``sys.modules``,
#: ``reload`` / ``import_module``, a ``patch`` / ``setattr`` / ``delattr``
#: helper, or ``__import__`` / ``builtins``. A file with none of these tokens
#: cannot violate, so it is never parsed.
_PREFILTER = re.compile(r"kairix|modules|reload|import_module|patch|setattr|delattr|__import__|builtins")

#: Names F1 guards against rebinding at module / class level (or alongside
#: their own import in a function) — see ``_mapping_writes.guarded_rebinds``.
_F1_GUARDED_NAMES: dict[str, GuardedName] = {
    "sys": ("module", "sys"),
    "importlib": ("module", "importlib"),
    "modules": ("from", "sys", "modules"),
    "reload": ("from", "importlib", "reload"),
    **BUILTIN_GUARDED_NAMES,
}
_DUNDER_IMPORT = "__import__"


def _is_kairix_module_name(value: str) -> bool:
    return _KAIRIX_MODULES(value)


class _F1Ctx:
    """Per-file resolution state: kairix aliases, constants and the shared
    default-deny ``sys.modules`` guard."""

    def __init__(self, index: ModuleIndex) -> None:
        self.index = index
        self.constants = ConstantTable(index)
        self.aliases = _resolve_kairix_aliases(index)
        self.external = _external_imports(index)
        self.sys_modules = ProcessMapping.resolve(index, "sys", "modules")
        self.guard = MappingGuard(index, self.sys_modules, _KAIRIX_MODULES, self.constants, "kairix.*")
        self.importlib_names = index.module_aliases("importlib")
        self.reload_names = index.from_imports("importlib", "reload")
        # ``from importlib import import_module [as load]`` — tracked like reload.
        self.import_module_names = index.from_imports("importlib", "import_module") | {"__import__"}
        self.guard.extra_guarded = self._importlib_guarded
        self._bind_local_module_aliases()

    def guard_alias_refs(self) -> set[int]:
        """Node ids the guard reported as aliased (computed by ``guard.findings``)."""
        if not hasattr(self.guard, "_alias_refs"):
            self.guard.alias_findings()
        return self.guard._alias_refs

    def is_importlib(self, expr: ast.expr | None) -> bool:
        """``expr`` is the ``importlib`` module (scope-aware: a shadowing local is not)."""
        return (
            isinstance(expr, ast.Name)
            and expr.id in self.importlib_names
            and self.index.resolves_to_module(expr, "importlib")
        )

    def is_reload_name(self, expr: ast.expr | None) -> bool:
        """``expr`` is ``reload`` imported from ``importlib`` (scope-aware)."""
        return (
            isinstance(expr, ast.Name)
            and expr.id in self.reload_names
            and self.index.resolves_to_from(expr, "importlib", "reload")
        )

    def _importlib_guarded(self, expr: ast.expr) -> str | None:
        """F1's extra guarded objects: the ``importlib`` module and ``reload``."""
        if self.is_importlib(expr):
            return "the importlib module"
        if self.is_reload_name(expr):
            return "importlib.reload"
        if isinstance(expr, ast.Attribute) and expr.attr == "reload" and self.is_importlib(expr.value):
            return "importlib.reload"
        return None

    def _bind_local_module_aliases(self) -> None:
        """Feed locally bound kairix modules into the alias table, to a fixpoint.

        ``module = importlib.import_module("kairix.paths")``,
        ``module = sys.modules["kairix.paths"]`` and ``module = kairix.paths``
        (or a chain of such names) bind a kairix module object exactly like an
        ``import`` does, so ``importlib.reload(module)`` / ``module.x = fake``
        are caught. (This tracks KAIRIX module objects for shapes 3-6; guarded
        objects — ``sys.modules``, ``sys``, ``importlib``, ``reload`` — may not be
        aliased at all.)
        """
        changed = True
        while changed:
            changed = False
            for name, value in self.index.name_bindings:
                if name not in self.aliases and self.is_kairix_module_ref(value):
                    self.aliases[name] = "kairix"
                    changed = True

    # -- string targets / keys (default-deny) ------------------------------

    def provably_not_kairix(self, expr: ast.expr | None) -> bool:
        """``expr`` is PROVABLY a non-kairix name / dotted path (unknown → False)."""
        return expr is not None and self.constants.provably_outside(expr, _KAIRIX_MODULES)

    def could_be_kairix(self, expr: ast.expr | None) -> bool:
        return expr is not None and not self.provably_not_kairix(expr)

    def is_object_target(self, expr: ast.expr) -> bool:
        """``expr`` is PROVABLY an object, not a dotted string: a module / alias
        reference, or a name bound only to non-string expressions (a call, a
        class) — so ``setattr`` is the object overload even with ``value``
        hidden behind a spread."""
        if isinstance(expr, ast.Name) and (expr.id in self.aliases or expr.id in self.external):
            return True
        return self.constants.definitely_object(expr)

    def is_reload(self, func: ast.expr) -> bool:
        if isinstance(func, ast.Attribute):
            return func.attr == "reload" and self.is_importlib(func.value)
        return self.is_reload_name(func)

    def is_importer(self, func: ast.expr) -> bool:
        return (isinstance(func, ast.Attribute) and func.attr == "import_module") or (
            isinstance(func, ast.Name) and func.id in self.import_module_names
        )

    # -- module objects ----------------------------------------------------

    def is_kairix_module_ref(self, expr: ast.expr | None) -> bool:
        """``expr`` may evaluate to a kairix module object (positive evidence)."""
        if expr is None:
            return False
        if _resolves_to_kairix(expr, self.aliases):
            return True
        if isinstance(expr, ast.Subscript) and self.sys_modules.is_receiver(expr.value):
            return self.could_be_kairix(expr.slice)
        if isinstance(expr, ast.Call) and self.is_importer(expr.func):
            return self.could_be_kairix(bind_call(expr, IMPORT_MODULE_SIGNATURE).get("name"))
        return False

    def provably_external_module(self, expr: ast.expr | None, seen: frozenset[str] = frozenset()) -> bool:
        """``expr`` is PROVABLY a non-kairix module object (default-deny for reload)."""
        if expr is None or self.is_kairix_module_ref(expr):
            return False
        if isinstance(expr, ast.Attribute):
            root = _attribute_root_name(expr)
            return root is not None and root in self.external and root not in self.aliases
        if isinstance(expr, ast.Name):
            return self._provably_external_name(expr.id, seen)
        if isinstance(expr, ast.Subscript) and self.sys_modules.is_receiver(expr.value):
            return self.provably_not_kairix(expr.slice)
        if isinstance(expr, ast.Call) and self.is_importer(expr.func):
            return self.provably_not_kairix(bind_call(expr, IMPORT_MODULE_SIGNATURE).get("name"))
        return False

    def _provably_external_name(self, name: str, seen: frozenset[str]) -> bool:
        """Every binding of ``name`` is a non-kairix import or a provably
        external module expression."""
        if name in self.aliases or name in seen:
            return False
        bindings = self.index.value_bindings.get(name, [])
        if not bindings:
            return False
        imported = name in self.external
        for kind, source in bindings:
            if kind == VALUE:
                if not self.provably_external_module(source, seen | {name}):
                    return False
            elif not (kind == UNKNOWN and source is None and imported):
                return False
        return True


def _external_imports(index: ModuleIndex) -> set[str]:
    """Local names bound by an ``import`` / ``from-import`` of a NON-kairix module."""
    names: set[str] = set()
    for node in index.imports:
        for alias in node.names:
            if not _is_kairix_module_name(alias.name):
                names.add(alias.asname or alias.name.split(".")[0])
    for imported in index.import_froms:
        if imported.module and imported.level == 0 and not _is_kairix_module_name(imported.module):
            names.update(alias.asname or alias.name for alias in imported.names)
    return names


def _patch_shapes(call: ast.Call, ctx: _F1Ctx) -> bool:
    """Shapes 1 + 2: ``patch(target)`` — decorator, ``with``, ``.start()`` —
    whose dotted ``target`` is not PROVABLY a non-kairix path (default-deny:
    an unfoldable target fails), and ``patch.object(<kairix ref>, "attr")``."""
    if ctx.guard.is_patch(call.func):
        bound = bind_call(call, PATCH_SIGNATURE)
        target = bound.get("target")
        return bound.has_spread if target is None else ctx.could_be_kairix(target)
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "object" and ctx.guard.is_patch(func.value):
        return ctx.is_kairix_module_ref(bind_call(call, PATCH_OBJECT_SIGNATURE).get("target"))
    return False


def _monkeypatch_attr_shapes(call: ast.Call, ctx: _F1Ctx) -> bool:
    """Shapes 5 + 6: ``<monkeypatch>.setattr / delattr``. The dotted-string
    overload (``setattr("a.b.c", value)`` / ``delattr("a.b.c")``) is default-deny:
    the target must fold to a PROVABLY non-kairix path. The object overload
    (``setattr(obj, "name", value)``) fails when ``obj`` is a kairix module ref."""
    func = call.func
    # Matched by METHOD NAME on any receiver — the fixture, an inline
    # ``pytest.MonkeyPatch()``, a ``MonkeyPatch.context()`` target, anything.
    if not (isinstance(func, ast.Attribute) and func.attr in {"setattr", "delattr"}):
        return False
    bound = bind_call(call, MONKEYPATCH_SIGNATURES[func.attr])
    target = bound.get("target")
    if target is None:
        return bound.has_spread
    object_overload = bound.get("value" if func.attr == "setattr" else "name") is not None
    if ctx.is_object_target(target) or (object_overload and not _is_string_expr(target)):
        return ctx.is_kairix_module_ref(target)
    return ctx.could_be_kairix(target)


def _is_string_expr(expr: ast.expr) -> bool:
    return isinstance(expr, (ast.JoinedStr, ast.BinOp)) or (
        isinstance(expr, ast.Constant) and isinstance(expr.value, str)
    )


def _may_name_dunder_import(ctx: _F1Ctx, key: ast.expr | None) -> bool:
    """``key`` (a getattr name / subscript key) may evaluate to ``"__import__"``.

    Resolved strings decide; otherwise a constant leading prefix decides
    (``f"__im{x}"``); a wholly unknown key does not count (it is everywhere)."""
    if key is None:
        return False
    values = ctx.constants.strings(key)
    if values is not None:
        return _DUNDER_IMPORT in values
    folded = fold_string(key)
    return folded is not None and bool(folded[0]) and _DUNDER_IMPORT.startswith(folded[0])


def _names_dunder_import(ctx: _F1Ctx, arg: ast.expr) -> bool:
    """A call argument that resolves to ``"__import__"`` or a dotted
    ``"<x>.__import__"`` target — ``monkeypatch.setattr(builtins, "__import__",
    ...)``, ``patch("builtins.__import__")``."""
    values = ctx.constants.strings(arg)
    return values is not None and any(
        value == _DUNDER_IMPORT or value.endswith(f".{_DUNDER_IMPORT}") for value in values
    )


def _dunder_import_violation(ctx: _F1Ctx) -> bool:
    """ANY reference to builtin ``__import__`` is an F1 violation — tests import
    normally, or call ``importlib.import_module`` with a static name.

    Caught: the bare builtin (or ``from builtins import __import__``), any
    ``<x>.__import__`` attribute (``builtins`` / ``__builtins__`` /
    ``importlib``), and a dynamic lookup whose name / key folds to it —
    ``getattr(builtins, "__im" + "port__")``, ``__builtins__["__import__"]``,
    ``vars(builtins)["__import__"]``."""
    index = ctx.index
    if index.attributes.get(_DUNDER_IMPORT):
        return True
    for ref in index.name_loads.get(_DUNDER_IMPORT, []):
        if index.is_builtin_ref(ref):
            return True
    for node in index.candidates:
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_getattr = isinstance(func, ast.Name) and func.id == "getattr" and index.is_builtin_ref(func)
        if is_getattr and len(node.args) >= 2 and _may_name_dunder_import(ctx, node.args[1]):
            return True
        if any(_names_dunder_import(ctx, arg) for arg in (*node.args, *(kw.value for kw in node.keywords))):
            return True
    return any(_may_name_dunder_import(ctx, sub.slice) for sub in index.subscripts)


def _reload_violation(ctx: _F1Ctx) -> bool:
    """Shape 8 (default-deny): ANY reference to ``importlib.reload`` — called or
    aliased — fails unless it is a direct call whose ``module`` argument is
    PROVABLY a non-kairix module (an unresolved argument fails)."""
    refs: list[ast.expr] = [node for node in ctx.index.attributes.get("reload", []) if ctx.is_importlib(node.value)]
    for name in ctx.reload_names:
        refs.extend(ref for ref in ctx.index.name_loads.get(name, []) if ctx.is_reload_name(ref))
    for ref in refs:
        call = ctx.index.parents.get(ref)
        if not (isinstance(call, ast.Call) and call.func is ref):
            return True  # aliased / passed around — never chased
        if not ctx.provably_external_module(bind_call(call, RELOAD_SIGNATURE).get("module")):
            return True
    return _importlib_module_misused(ctx)


def _importlib_module_misused(ctx: _F1Ctx) -> bool:
    """The ``importlib`` module object is guarded like ``os`` / ``sys``: an
    attribute access is fine; as an argument it may only go to a direct builtin
    ``getattr`` / ``hasattr`` with a static attribute name other than
    ``reload``. Anything else (``getattr(importlib, name)``, a helper, an
    alias) fails."""
    for name in ctx.importlib_names:
        for ref in ctx.index.name_loads.get(name, []):
            if not ctx.is_importlib(ref):
                continue  # a local / parameter shadows the import
            parent = ctx.index.parents.get(ref)
            if isinstance(parent, ast.Attribute) and parent.value is ref:
                continue
            if id(ref) in ctx.guard_alias_refs():
                return True
            if isinstance(parent, ast.Call) and _importlib_argument_harmless(parent, ref, ctx):
                continue
            return True
    return False


def _importlib_argument_harmless(call: ast.Call, ref: ast.expr, ctx: _F1Ctx) -> bool:
    """The same narrow argument rule as ``os`` / ``sys``: a direct builtin
    ``getattr`` / ``hasattr``, or ``<x>.setattr`` / ``<x>.delattr`` /
    ``patch.object``, with a static attribute name other than ``reload``."""
    if any(isinstance(a, ast.Starred) for a in call.args) or any(kw.arg is None for kw in call.keywords):
        return False
    func = call.func
    name: ast.expr | None = None
    if isinstance(func, ast.Name) and func.id in {"getattr", "hasattr"} and ctx.index.is_builtin_ref(func):
        name = call.args[1] if len(call.args) >= 2 and call.args[0] is ref else None
    elif isinstance(func, ast.Attribute) and func.attr in {"setattr", "delattr"}:
        bound = bind_call(call, MONKEYPATCH_SIGNATURES[func.attr])
        name = bound.get("name") if bound.get("target") is ref else None
    elif isinstance(func, ast.Attribute) and func.attr == "object" and ctx.guard.is_patch(func.value):
        bound = bind_call(call, PATCH_OBJECT_SIGNATURE)
        name = bound.get("attribute") if bound.get("target") is ref else None
    values = ctx.constants.strings(name)
    return values is not None and "reload" not in values


def file_has_internal_patch(path: Path) -> bool:
    """Return True iff ``path`` contains any F1 violation."""
    index = parse_index(path, _PREFILTER)
    if index is None:
        return False

    ctx = _F1Ctx(index)
    parent_map = index.parents

    if ctx.guard.findings() or _reload_violation(ctx) or _dunder_import_violation(ctx):
        return True
    if guarded_rebinds(index, _F1_GUARDED_NAMES):
        return True

    for node in index.candidates:
        if isinstance(node, ast.Call) and (_patch_shapes(node, ctx) or _monkeypatch_attr_shapes(node, ctx)):
            return True

        # Shape 3 + 4: attribute assignment ``<...>.attr = expr`` where root
        # resolves to a kairix module. Skip when the assignment sits inside
        # a ``with pytest.raises(...):`` — that's a frozen-attribute
        # contract test, not a patch.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and _resolves_to_kairix(target, ctx.aliases):
                    if not _is_inside_pytest_raises(parent_map, node):
                        return True

    return False


class F1(FitnessRule):
    """F1 as an in-process :class:`FitnessRule` over ``tests/``.

    In-process (not a shell subprocess) so the shared runner's staged mode
    narrows :meth:`enumerate_files` to the staged test files — ``safe-commit.sh
    --check`` scans only what changed, while ``--all`` / CI scan every file.
    """

    name = "no-internal-patches"
    remediation = REMEDIATION
    roots = ("tests",)

    def file_has_violation(self, path: Path) -> bool:
        return file_has_internal_patch(path)


def main() -> int:
    return F1().run()


if __name__ == "__main__":
    sys.exit(main())
