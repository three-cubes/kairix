"""F1 detector (static half): flag tests that substitute kairix-internal implementations.

Fast pre-commit feedback for the common spellings. Walks every test file via
AST and reports each line matching one of these shapes:

1. ``patch("kairix.X.Y", ...)`` — decorator, ``with``, or ``.start()``
2. ``patch.object(<kairix ref>, "attr", ...)``
3. ``kairix.X.Y = <expr>`` — full-path attribute assignment
4. ``<alias>.Y = <expr>`` where ``<alias>`` resolves to a kairix module
5. ``monkeypatch.setattr / delattr("kairix.X.Y", ...)`` — string-target form
6. ``monkeypatch.setattr / delattr(<kairix module ref>, "attr", ...)``
7. ``sys.modules["kairix.X"] = ...`` / ``del sys.modules["kairix.X"]``
8. ``importlib.reload(<imported kairix module>)``
9. builtin ``setattr / delattr(<imported kairix module or attribute>, ...)``

The exact half is the runtime guard ``tests/fixtures/process_state_guard.py``:
it fails a running test that swaps or removes a ``sys.modules`` kairix entry,
re-executes an imported kairix module, or patches a kairix object through
``monkeypatch`` / ``mock.patch`` — however it is spelled. This file
deliberately stays a small AST match — it is the fast loop, not the proof.

Scope: the static half resolves kairix references through the file's imports
only. A runtime-computed module reference plus a manual restore (e.g.
``setattr(importlib.import_module(name), ...)``) is deliberate evasion and is
out of scope for both halves by design.

Stdlib roots (``os``, ``time``, ``pathlib``, ``sys``, ``importlib``,
``builtins``, ``threading``, ``functools``, ``re``, ``json``,
``logging``, ...) and external SDK roots (``httpx``, ``openai``,
``boto3``, ``anthropic``, ``requests``, ``numpy``, ``neo4j``,
``usearch``, ``sentence_transformers``, ``spacy``, ``rich``,
``click``, ``unittest``, ``pytest``) are exempt — patching these is
fixturing genuinely external state at the kairix edge.

Output: ``path:line: shape`` per violation; the gate fails on any.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _fitness_rule import FitnessRule
from tc_fitness import gate_keys

REMEDIATION = """Refactor to constructor injection with a fake from tests/fakes.py to pass.

fix: rewrite the test to construct the unit under test with a Fake*
from tests/fakes.py (e.g. ``SearchPipeline(retriever=FakeRetriever(...))``).
If the production class lacks a constructor seam, add one — same shape
as ``GoldBuilder(llm_judge=, retriever=, db_path=)``.

A ``sys.modules`` swap / ``importlib.reload`` usually resets module-level
singleton state or simulates a failed import. Reset the state through the
module's public reset function (``reset_cross_encoder_cache()``,
``reset_api_key_cache()``), simulate a missing THIRD-PARTY dependency with
``monkeypatch.setitem(sys.modules, "yaml", None)``, and test a fresh import
of kairix itself in a subprocess
(``subprocess.run([sys.executable, "-c", "import kairix"])``).

When production resolves dependencies via function-local imports at
call time, move that resolution to construction time via the existing
``*Deps`` dataclass with ``default_factory`` — see
``EmbedDependencies`` / ``LLMBackendDeps`` / ``BenchmarkDeps`` for the
canonical shape, then inject the Fake* at construction.

next: re-run ``python3 scripts/checks/check_no_internal_patches.py``
(or ``python3 scripts/checks/run_checks.py --gate F1``) to confirm the gate goes green.
run: bash scripts/safe-commit.sh "refactor(<area>): inject Fake via DI seam"

Pass example:
  pipeline = SearchPipeline(retriever=FakeRetriever(hits=[...]))
  assert pipeline.run(query='x') == ...

Forbidden example:
  Shapes that fire the gate (all the same anti-pattern):
  @patch('kairix.core.search.bm25.bm25_search')
  with patch.object(paths_mod, 'provider_name'):
  kairix.paths.provider_name = lambda: "fake"
  paths_mod.provider_name = lambda: "fake"
  monkeypatch.setattr("kairix.paths.provider_name", ...)
  monkeypatch.setattr(kairix.paths, "provider_name", ...)
  sys.modules["kairix.core.search.rerank"] = stub
  importlib.reload(rerank_mod)

Stdlib boundaries (os.*, time.*, etc.) and external SDK boundaries
(httpx.*, openai.*, boto3.*, etc.) remain allowed — F1 only flags
kairix.* targets. The runtime half (tests/fixtures/process_state_guard.py)
fails any spelling this static check misses when the test runs."""


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


def _is_patch_object_call(node: ast.Call, aliases: dict[str, str]) -> bool:
    """``patch.object(<kairix ref>, ...)`` / ``mock.patch.object(<kairix ref>, ...)``."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "object"):
        return False
    owner = func.value
    is_patch = (isinstance(owner, ast.Name) and owner.id == "patch") or (
        isinstance(owner, ast.Attribute) and owner.attr == "patch"
    )
    return is_patch and bool(node.args) and _resolves_to_kairix(node.args[0], aliases)


def _is_sys_modules_kairix_item(target: ast.expr) -> bool:
    """``sys.modules["kairix..."]`` subscript with a string-literal kairix key."""
    if not isinstance(target, ast.Subscript):
        return False
    value, key = target.value, target.slice
    is_sys_modules = (
        isinstance(value, ast.Attribute)
        and value.attr == "modules"
        and isinstance(value.value, ast.Name)
        and value.value.id == "sys"
    )
    return (
        is_sys_modules
        and isinstance(key, ast.Constant)
        and isinstance(key.value, str)
        and (key.value == "kairix" or key.value.startswith("kairix."))
    )


def _is_kairix_reload(node: ast.Call, aliases: dict[str, str]) -> bool:
    """``importlib.reload(<kairix module ref>)`` / ``reload(<kairix module ref>)``."""
    func = node.func
    is_reload = (isinstance(func, ast.Name) and func.id == "reload") or (
        isinstance(func, ast.Attribute)
        and func.attr == "reload"
        and isinstance(func.value, ast.Name)
        and func.value.id == "importlib"
    )
    return is_reload and bool(node.args) and _resolves_to_kairix(node.args[0], aliases)


def _is_builtin_attr_write(node: ast.Call, aliases: dict[str, str]) -> bool:
    """Builtin ``setattr(<kairix ref>, ...)`` / ``delattr(<kairix ref>, ...)``."""
    func = node.func
    return (
        isinstance(func, ast.Name)
        and func.id in ("setattr", "delattr")
        and bool(node.args)
        and _resolves_to_kairix(node.args[0], aliases)
    )


def _first_arg_is_kairix_string(call: ast.Call) -> bool:
    """First positional arg of patch(...) / setattr(...) is a string starting with ``kairix.``."""
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value == "kairix" or first.value.startswith("kairix.")
    return False


def _is_monkeypatch_setattr(node: ast.Call) -> bool:
    """``node`` is ``monkeypatch.setattr(...)`` / ``monkeypatch.delattr(...)``."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in ("setattr", "delattr")
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


def _call_shape(node: ast.Call, aliases: dict[str, str]) -> str | None:
    if _is_patch_call(node) and _first_arg_is_kairix_string(node):
        return "patch(kairix.*)"
    if _is_patch_object_call(node, aliases):
        return "patch.object(<kairix ref>)"
    if _is_monkeypatch_setattr(node) and (
        _first_arg_is_kairix_string(node) or (bool(node.args) and _resolves_to_kairix(node.args[0], aliases))
    ):
        return "monkeypatch.setattr/delattr(<kairix target>)"
    if _is_kairix_reload(node, aliases):
        return "importlib.reload(<kairix module>)"
    if _is_builtin_attr_write(node, aliases):
        return "setattr/delattr(<kairix target>)"
    return None


def _node_shape(node: ast.AST, aliases: dict[str, str], parent_map: dict[ast.AST, ast.AST]) -> str | None:
    if isinstance(node, ast.Call):
        return _call_shape(node, aliases)
    if isinstance(node, (ast.Assign, ast.Delete)) and any(_is_sys_modules_kairix_item(t) for t in node.targets):
        return "sys.modules[kairix.*] swap"
    if isinstance(node, ast.Assign):
        # Shape 3 + 4. Skip assignments inside ``with pytest.raises(...):`` —
        # a frozen-attribute contract test, not a patch.
        for target in node.targets:
            if isinstance(target, ast.Attribute) and _resolves_to_kairix(target, aliases):
                if not _is_inside_pytest_raises(parent_map, node):
                    return "assignment to a kairix attribute"
    return None


def file_violations(path: Path) -> list[str]:
    """Every ``line: shape`` F1 violation in ``path`` (sorted by line)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return []
    aliases = _resolve_kairix_aliases(tree)
    parent_map: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent
    found = {
        (getattr(node, "lineno", 0), shape)
        for node in ast.walk(tree)
        if (shape := _node_shape(node, aliases, parent_map))
    }
    return [f"{line}: {shape}" for line, shape in sorted(found)]


def file_has_internal_patch(path: Path) -> bool:
    """Return True iff ``path`` contains any detected F1 violation shape."""
    return bool(file_violations(path))


class F1(FitnessRule):
    """F1 static half as an in-process rule over ``tests/`` (staged runs narrow it)."""

    name = "no-internal-patches"
    remediation = REMEDIATION
    roots = ("tests",)

    def file_has_violation(self, path: Path) -> bool:
        return file_has_internal_patch(path)

    def run(self) -> int:
        found: set[str] = set()
        for path in self.enumerate_files():
            rel = str(self._repo_relative(path))
            if self.is_in_scope(rel):
                found.update(f"{rel}:{violation}" for violation in file_violations(path))
        return int(gate_keys(self.name, found, self.remediation))


def main() -> int:
    return F1().run()


if __name__ == "__main__":
    sys.exit(main())
