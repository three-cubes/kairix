"""F1 detector: flag tests that substitute kairix-internal implementations.

Walks every test file via AST and reports the path of any file that
matches one of eight shapes:

1. ``@patch("kairix.X.Y", ...)`` — decorator
2. ``with patch("kairix.X.Y", ...):`` — context manager
3. ``kairix.X.Y = <expr>`` — full-path attribute assignment
4. ``<alias>.Y = <expr>`` where ``<alias>`` resolves to a kairix module
5. ``monkeypatch.setattr("kairix.X.Y", ...)`` — string-target form
6. ``monkeypatch.setattr(<kairix module ref>, "attr", fake)`` — ref-target form
7. ``sys.modules`` swap of a kairix module — ``sys.modules["kairix.X"] = m``,
   ``del sys.modules["kairix.X"]``, ``sys.modules.pop("kairix.X")``,
   ``sys.modules.setdefault("kairix.X", m)``, ``sys.modules.update({...})``
   and ``monkeypatch.setitem / delitem(sys.modules, "kairix.X", ...)``.
   Replacing (or evicting, then re-importing) a module object substitutes
   the whole kairix implementation — the same anti-pattern as ``@patch``,
   one level up. A key held in a variable bound from a ``"kairix..."``
   literal counts (see ``_ast_key_taint``).
8. ``importlib.reload(<kairix module>)`` — re-executes module-level code
   to reset hidden singleton state, so the test passes against a module
   object no production process ever sees. Inject the state holder
   instead.

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
``file_has_internal_patch`` and add the matching positive + negative
tests to ``tests/architecture/test_check_no_internal_patches.py``.

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
  sys.modules["kairix.core.search.pipeline"] = BrokenModule(...)
  importlib.reload(kairix.core.search.rerank)

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


def _is_patch_call(node: ast.expr) -> bool:
    """Return True when ``node`` is a Call to ``patch`` / ``mock.patch`` etc.

    Conservative: only matches the literal name ``patch`` or attribute
    access ending in ``.patch``. Other helpers (``patch.dict``,
    ``patch.object``) have a different arg shape and are NOT covered by F1.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "patch"
    if isinstance(func, ast.Attribute):
        return func.attr == "patch"
    return False


def _first_arg_is_kairix_string(call: ast.Call) -> bool:
    """First positional arg of patch(...) / setattr(...) is a string starting with ``kairix.``."""
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value == "kairix" or first.value.startswith("kairix.")
    return False


def _is_monkeypatch_setattr(node: ast.Call) -> bool:
    """``node`` is ``monkeypatch.setattr(...)``."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "setattr"
        and isinstance(func.value, ast.Name)
        and func.value.id == "monkeypatch"
    )


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


_SYS_MODULES_KEY_METHODS = frozenset({"pop", "setdefault"})


class _ModuleSwapCtx:
    """Per-file resolution state for shapes 7 + 8."""

    def __init__(self, tree: ast.AST, aliases: dict[str, str]) -> None:
        self.aliases = aliases
        self.tainted = tainted_names(tree, _is_kairix_module_name)
        self.sys_names = module_aliases(tree, "sys")
        self.modules_names = from_imports(tree, "sys", "modules")
        self.importlib_names = module_aliases(tree, "importlib")
        self.reload_names = from_imports(tree, "importlib", "reload")

    def is_sys_modules(self, expr: ast.expr) -> bool:
        return is_module_attr(expr, self.sys_names, "modules", self.modules_names)

    def is_kairix_key(self, expr: ast.expr) -> bool:
        return key_is_protected(expr, _is_kairix_module_name, self.tainted)

    def is_reload(self, func: ast.expr) -> bool:
        return is_module_attr(func, self.importlib_names, "reload", self.reload_names)

    def is_kairix_module_ref(self, expr: ast.expr) -> bool:
        """``expr`` evaluates to a kairix module object."""
        if _resolves_to_kairix(expr, self.aliases):
            return True
        if isinstance(expr, ast.Subscript) and self.is_sys_modules(expr.value):
            return self.is_kairix_key(expr.slice)
        if isinstance(expr, ast.Call) and expr.args:
            func = expr.func
            # importlib.import_module("kairix.X") / __import__("kairix.X")
            is_importer = (isinstance(func, ast.Attribute) and func.attr == "import_module") or (
                isinstance(func, ast.Name) and func.id in {"import_module", "__import__"}
            )
            return is_importer and self.is_kairix_key(expr.args[0])
        return False


def _sys_modules_call_swap(node: ast.Call, ctx: _ModuleSwapCtx) -> bool:
    """``sys.modules.pop/setdefault/update`` or ``monkeypatch.setitem/delitem(sys.modules, ...)``."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if ctx.is_sys_modules(func.value):
        if func.attr in _SYS_MODULES_KEY_METHODS and node.args:
            return ctx.is_kairix_key(node.args[0])
        if func.attr == "update" and node.args and isinstance(node.args[0], ast.Dict):
            return any(k is not None and ctx.is_kairix_key(k) for k in node.args[0].keys)
        return False
    is_monkeypatch_item = (
        func.attr in {"setitem", "delitem"} and isinstance(func.value, ast.Name) and func.value.id == "monkeypatch"
    )
    if not is_monkeypatch_item or len(node.args) < 2:
        return False
    return ctx.is_sys_modules(node.args[0]) and ctx.is_kairix_key(node.args[1])


def _sys_modules_subscript_swap(target: ast.expr, ctx: _ModuleSwapCtx) -> bool:
    return isinstance(target, ast.Subscript) and ctx.is_sys_modules(target.value) and ctx.is_kairix_key(target.slice)


def _is_module_swap(node: ast.AST, ctx: _ModuleSwapCtx) -> bool:
    """Shapes 7 + 8: a sys.modules swap of, or importlib.reload on, a kairix module."""
    if isinstance(node, ast.Call):
        if ctx.is_reload(node.func) and node.args and ctx.is_kairix_module_ref(node.args[0]):
            return True
        return _sys_modules_call_swap(node, ctx)
    if isinstance(node, (ast.Assign, ast.Delete)):
        return any(_sys_modules_subscript_swap(t, ctx) for t in node.targets)
    if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        return _sys_modules_subscript_swap(node.target, ctx)
    return False


def file_has_internal_patch(path: Path) -> bool:
    """Return True iff ``path`` contains any of the eight F1 violation shapes."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return False

    aliases = _resolve_kairix_aliases(tree)
    swap_ctx = _ModuleSwapCtx(tree, aliases)

    # Build a child->parent map so we can ask "is this Assign inside a
    # pytest.raises With block?" without re-traversing the whole tree.
    parent_map: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent

    for node in ast.walk(tree):
        # Shape 1: @patch decorator on kairix target
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for deco in node.decorator_list:
                if _is_patch_call(deco) and _first_arg_is_kairix_string(deco):
                    return True

        # Shape 2: with patch(...) on kairix target
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if _is_patch_call(ctx) and _first_arg_is_kairix_string(ctx):
                    return True

        # Shape 3 + 4: attribute assignment ``<...>.attr = expr`` where root
        # resolves to a kairix module. Skip when the assignment sits inside
        # a ``with pytest.raises(...):`` — that's a frozen-attribute
        # contract test, not a patch.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and _resolves_to_kairix(target, aliases):
                    if not _is_inside_pytest_raises(parent_map, node):
                        return True

        # Shape 5 + 6: monkeypatch.setattr(...) on kairix target
        if isinstance(node, ast.Call) and _is_monkeypatch_setattr(node):
            if _first_arg_is_kairix_string(node):
                return True
            if node.args and _resolves_to_kairix(node.args[0], aliases):
                return True

        # Shape 7 + 8: sys.modules swap / importlib.reload of a kairix module
        if _is_module_swap(node, swap_ctx):
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
