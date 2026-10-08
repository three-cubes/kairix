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
7. any WRITE to ``sys.modules`` that can touch a kairix module — the full
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
8. ``importlib.reload(module=<kairix module>)`` — positional or keyword,
   through ``importlib`` / ``reload`` / ``import_module`` aliases.
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
from _ast_key_taint import from_imports, is_module_attr, key_is_protected, module_aliases, tainted_names
from _mapping_writes import (
    IMPORT_MODULE_SIGNATURE,
    MONKEYPATCH_SIGNATURES,
    PATCH_OBJECT_SIGNATURE,
    PATCH_SIGNATURE,
    RELOAD_SIGNATURE,
    ProcessMapping,
    WriteSurface,
    bind_call,
    is_patch_ref,
)

REMEDIATION = """kairix-internal substitution found in a test (@patch / monkeypatch.setattr /
attribute assignment on a kairix target, a sys.modules swap of a kairix
module, or importlib.reload of one). Refactor to constructor injection
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

next: re-run ``python3 scripts/checks/check_no_internal_patches.py``
to confirm the gate goes green.
run: bash scripts/safe-commit.sh "refactor(<area>): inject Fake via DI seam"

Pass example:
  pipeline = SearchPipeline(retriever=FakeRetriever(hits=[...]))
  assert pipeline.run(query='x') == ...
  cache = CrossEncoderCache()
  assert get_cross_encoder("m", cache=cache) is None

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


def _resolve_kairix_aliases(tree: ast.AST) -> dict[str, str]:
    """Map local name -> fully-qualified kairix path from the file's imports.

    Examples:
      ``import kairix.paths as paths_mod`` -> {"paths_mod": "kairix.paths"}
      ``from kairix import providers as p`` -> {"p": "kairix.providers"}
      ``from kairix.paths import provider_name`` -> {"provider_name": "kairix.paths.provider_name"}
      ``import kairix.paths`` -> {"kairix": "kairix"}  (root binding only)
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
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


def _is_kairix_module_name(value: str) -> bool:
    return value == "kairix" or value.startswith("kairix.")


class _F1Ctx:
    """Per-file resolution state: kairix aliases + the shared ``sys.modules`` surface."""

    def __init__(self, tree: ast.AST) -> None:
        self.aliases = _resolve_kairix_aliases(tree)
        self.tainted = tainted_names(tree, _is_kairix_module_name)
        self.sys_modules = ProcessMapping.resolve(tree, "sys", "modules")
        self.surface = WriteSurface(tree, self.sys_modules, _is_kairix_module_name, self.tainted, "kairix.*")
        self.importlib_names = module_aliases(tree, "importlib")
        self.reload_names = from_imports(tree, "importlib", "reload")
        # ``from importlib import import_module [as load]`` — tracked like reload.
        self.import_module_names = from_imports(tree, "importlib", "import_module") | {"__import__"}

    def is_kairix_key(self, expr: ast.expr | None) -> bool:
        return expr is not None and key_is_protected(expr, _is_kairix_module_name, self.tainted)

    def is_kairix_string(self, expr: ast.expr | None) -> bool:
        return isinstance(expr, ast.Constant) and isinstance(expr.value, str) and _is_kairix_module_name(expr.value)

    def is_reload(self, func: ast.expr) -> bool:
        return is_module_attr(func, self.importlib_names, "reload", self.reload_names)

    def is_kairix_module_ref(self, expr: ast.expr | None) -> bool:
        """``expr`` evaluates to a kairix module object."""
        if expr is None:
            return False
        if _resolves_to_kairix(expr, self.aliases):
            return True
        if isinstance(expr, ast.Subscript) and self.sys_modules.is_receiver(expr.value):
            return self.is_kairix_key(expr.slice)
        if isinstance(expr, ast.Call):
            func = expr.func
            # importlib.import_module("kairix.X") / an aliased
            # ``from importlib import import_module as load`` / __import__("kairix.X")
            is_importer = (isinstance(func, ast.Attribute) and func.attr == "import_module") or (
                isinstance(func, ast.Name) and func.id in self.import_module_names
            )
            return is_importer and self.is_kairix_key(bind_call(expr, IMPORT_MODULE_SIGNATURE).get("name"))
        return False


def _patch_shapes(call: ast.Call, ctx: _F1Ctx) -> bool:
    """Shapes 1 + 2: ``patch("kairix.X")`` (decorator, ``with``, ``.start()``) and
    ``patch.object(<kairix ref>, "attr")`` — target bound by signature."""
    func = ctx.surface.patch
    if is_patch_ref(call.func, func):
        return ctx.is_kairix_string(bind_call(call, PATCH_SIGNATURE).get("target"))
    if isinstance(call.func, ast.Attribute) and call.func.attr == "object" and is_patch_ref(call.func.value, func):
        return ctx.is_kairix_module_ref(bind_call(call, PATCH_OBJECT_SIGNATURE).get("target"))
    return False


def _monkeypatch_attr_shapes(call: ast.Call, ctx: _F1Ctx) -> bool:
    """Shapes 5 + 6: ``<monkeypatch>.setattr / delattr`` on a kairix target, either
    overload (``("kairix.X.y", v)`` or ``(<kairix ref>, "y", v)``), any MonkeyPatch name."""
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr in {"setattr", "delattr"}
        and isinstance(func.value, ast.Name)
        and func.value.id in ctx.surface.monkeypatch
    ):
        return False
    target = bind_call(call, MONKEYPATCH_SIGNATURES[func.attr]).get("target")
    return ctx.is_kairix_string(target) or ctx.is_kairix_module_ref(target)


def _is_module_swap(node: ast.AST, ctx: _F1Ctx) -> bool:
    """Shapes 7 + 8: any write to ``sys.modules`` that can touch a kairix module
    (the shared ``_mapping_writes`` surface), or ``importlib.reload`` of one."""
    if isinstance(node, ast.Call) and ctx.is_reload(node.func):
        if ctx.is_kairix_module_ref(bind_call(node, RELOAD_SIGNATURE).get("module")):
            return True
    return bool(ctx.surface.writes(node))


def file_has_internal_patch(path: Path) -> bool:
    """Return True iff ``path`` contains any of the eight F1 violation shapes."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return False

    ctx = _F1Ctx(tree)

    # Build a child->parent map so we can ask "is this Assign inside a
    # pytest.raises With block?" without re-traversing the whole tree.
    parent_map: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent

    for node in ast.walk(tree):
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

        if _is_module_swap(node, ctx):
            return True

    return False


def main() -> int:
    root = Path("tests")
    if not root.is_dir():
        return 0

    violators: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if file_has_internal_patch(path):
            violators.append(str(path))

    for v in violators:
        print(v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
