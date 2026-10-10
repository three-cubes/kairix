"""F2 detector (static half): no test writes a ``KAIRIX_*`` process-env key.

Fast pre-commit feedback for the common spellings, with a string-literal key
starting with ``KAIRIX_``:

* ``monkeypatch.setenv("KAIRIX_X", ...)`` / ``monkeypatch.delenv("KAIRIX_X")``
  (any fixture name);
* ``os.environ["KAIRIX_X"] = ...`` / ``+=`` / ``del os.environ["KAIRIX_X"]``
  (or the name bound by ``from os import environ [as e]``, unless the
  enclosing function rebinds it);
* ``os.environ.pop("KAIRIX_X")`` / ``os.environ.setdefault("KAIRIX_X", ...)``;
* ``os.environ = ...`` with ANY value, and ``setattr(os, "environ", ...)`` /
  ``delattr(os, "environ")`` with a literal or constant-folded name (a
  wholesale replacement — even one restored within the same phase, which the
  runtime identity check misses).

``os`` includes every ``import os as <alias>``; names resolve in the scope
that binds them (functions, lambdas and class bodies).

Writes inside ``with allow_baseline_writes():`` in ``tests/conftest.py`` (the
session env baseline) are exempt, mirroring the runtime guard, as is the
runtime guard's own restore of the snapshotted ``os.environ``. In any
``conftest.py`` an ``os.environ`` write at module level (import time) fails
for ANY key (``os.putenv`` / ``os.unsetenv`` and their ``from os import``
names included), and so does one in any module the conftest imports or names
in ``pytest_plugins``: that code runs before the runtime guard is
configured. Decorators, defaults and annotations run at ``def`` time (a class
body with its ``class`` statement), except annotations under ``from
__future__ import annotations`` and ``type`` alias values, which never run.

The exact half is the runtime guard ``tests/fixtures/process_state_guard.py``:
an audit hook sees EVERY env write (any spelling, any computed key) while a
test runs and fails that test. This file deliberately stays a small AST
match — it is the fast loop, not the proof.

Output: ``path:line: shape`` per violation; the gate fails on any.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _fitness_rule import FitnessRule
from tc_fitness import gate_keys

REMEDIATION = """KAIRIX_* process-env write found in a test. Refactor to an explicit
``env=`` mapping / ``paths=FakePaths(...)`` / Deps seam to pass.

fix: pass the value through the production seam instead of the process
env — ``paths=FakePaths(...)`` from tests/fakes.py, an ``env={...}``
mapping on the reader (``read_int_env(..., env=...)``,
``load_secrets(path, env=...)``), or a ``*Deps`` dataclass. If the
production function reads the env var directly, add an
``env: Mapping[str, str] | None = None`` parameter that production leaves
as ``None`` (reads os.environ at the kairix.paths boundary) — the
boundary-only pattern from #139. A subprocess test passes ``env=`` to the
child instead.
next: re-run ``python3 scripts/checks/check_no_env_monkeypatch.py``
(or ``python3 scripts/checks/run_checks.py --gate F2``). The runtime half
(tests/fixtures/process_state_guard.py) fails any spelling this static
check misses when the test runs.
run: bash scripts/safe-commit.sh "test(<area>): inject env via seam instead of mutating os.environ"

Pass example:
  paths = FakePaths(data_dir=tmp_path, log_dir=tmp_path / 'logs')
  result = some_use_case(paths=paths)
  assert resolve_dispatch_concurrency(env={'KAIRIX_MAX_CONCURRENCY': '3'}) == 3

Forbidden example:
  monkeypatch.setenv('KAIRIX_DATA_DIR', str(tmp_path))
  os.environ['KAIRIX_DB_PATH'] = str(tmp_path / 'db.sqlite')
  os.environ.pop('KAIRIX_DB_PATH', None)

The one sanctioned writer is the session baseline in tests/conftest.py,
whose writes sit in ``with allow_baseline_writes():`` (both halves exempt
that block; using it anywhere else fails).

KAIRIX_* env-var reads happen ONCE at the boundary inside KairixPaths
(kairix/paths.py). Tests construct paths directly; they never mutate
process env to influence the production read."""

_ENV_HELPERS = {"setenv", "delenv"}
_BASELINE_CONTEXT = "allow_baseline_writes"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_HOME = (_REPO_ROOT / "tests" / "conftest.py").resolve()
_GUARD_HOME = (_REPO_ROOT / "tests" / "fixtures" / "process_state_guard.py").resolve()
_REPLACED = "os.environ replaced wholesale"
_ENVIRON_METHODS = {"pop", "setdefault"}
_OS_MUTATORS = frozenset({"putenv", "unsetenv"})
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_Scope = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef
_GUARD_RESTORE_MARKER = "F2-RESTORE"


def _is_kairix_literal(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("KAIRIX_")


def _in_body(scope: _Scope, child: ast.AST) -> bool:
    if isinstance(scope, ast.Lambda):
        return child is scope.body
    return child in scope.body


def _shallow_walk(scope: ast.AST) -> Iterator[ast.AST]:
    """``scope``'s body nodes, not descending into nested functions / classes."""
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, _SCOPES):
            continue
        yield child
        yield from _shallow_walk(child)


def _leaf_targets(target: ast.expr) -> Iterator[ast.expr]:
    """The individual store targets inside a (nested) tuple / list / starred target."""
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _leaf_targets(elt)
    elif isinstance(target, ast.Starred):
        yield from _leaf_targets(target.value)
    else:
        yield target


def _store_targets(node: ast.AST) -> list[ast.expr]:
    """Every leaf store target a statement or expression binds: assignment
    targets, ``for`` / comprehension targets, ``with ... as`` targets, ``del``."""
    if isinstance(node, (ast.Assign, ast.Delete)):
        targets: list[ast.expr] = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
        targets = [node.target]
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        targets = [g.target for g in node.generators]
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        targets = [item.optional_vars for item in node.items if item.optional_vars is not None]
    else:
        return []
    return [leaf for target in targets for leaf in _leaf_targets(target)]


_ASSIGNING = (
    ast.Assign,
    ast.AnnAssign,
    ast.For,
    ast.AsyncFor,
    ast.ListComp,
    ast.SetComp,
    ast.GeneratorExp,
    ast.DictComp,
    ast.With,
    ast.AsyncWith,
)


def _folded_str(node: ast.expr) -> str | None:
    """The value of a string expression the compiler folds to a constant:
    a literal, ``+`` of such, or an f-string whose every part is a literal."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded_str(node.left), _folded_str(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            part = _folded_str(value.value if isinstance(value, ast.FormattedValue) else value)
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)
    return None


class _Environ:
    """Decides whether an expression is ``os.environ`` in one parsed file.

    ``<os>.environ`` counts for ``os`` and every ``import os as <alias>``; a
    bare name counts when bound by ``from os import environ [as name]``. A
    binding is visible in the scope that holds the import (module level, or
    the function / lambda / class whose body holds it) and the scopes nested
    in it — a class body being no closure for its methods — and not where the
    evaluating scope rebinds the name (a parameter or an assignment of that
    name in the function, a store in the class body) — a simple local check,
    not a scope engine. A decorator, parameter default or annotation is evaluated
    in the scope that DEFINES the function, so a store in the function body
    cannot rebind a name used there.

    ``is_mutator`` applies the same rules to the bare names bound by ``from
    os import putenv / unsetenv [as name]``.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        self.environ_bindings: dict[str, set[ast.AST | None]] = {}
        self.mutator_bindings: dict[str, set[ast.AST | None]] = {}
        self.os_bindings: dict[str, set[ast.AST | None]] = {"os": {None}}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    if alias.name == "environ":
                        self._bind(self.environ_bindings, alias.asname or alias.name, node)
                    elif alias.name in _OS_MUTATORS:
                        self._bind(self.mutator_bindings, alias.asname or alias.name, node)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "os" or (alias.asname is None and alias.name.startswith("os.")):
                        self._bind(self.os_bindings, alias.asname or "os", node)
        self.deferred_annotations = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in ast.walk(tree)
        )

    def _bind(self, bindings: dict[str, set[ast.AST | None]], name: str, node: ast.AST) -> None:
        bindings.setdefault(name, set()).add(self.evaluating_scope(node))

    def evaluating_scope(self, node: ast.AST) -> _Scope | None:
        """The function, lambda or class whose BODY evaluates ``node``; ``None`` at module level.

        A node in a function's decorators, defaults, annotations or return
        annotation (a class's decorators, bases or keywords) is evaluated by
        the enclosing scope when the ``def`` / ``class`` runs, so that scope
        is skipped and the walk continues upward.
        """
        child, scope = node, self.parents.get(node)
        while scope is not None:
            if isinstance(scope, _SCOPES) and _in_body(scope, child):
                return scope
            child, scope = scope, self.parents.get(scope)
        return None

    def _bound(self, node: ast.expr, bindings: dict[str, set[ast.AST | None]]) -> bool:
        """``node`` is a name bound by one of ``bindings`` in a scope that
        encloses its evaluation and not rebound in the evaluating scope."""
        if not isinstance(node, ast.Name) or node.id not in bindings:
            return False
        scope = self.evaluating_scope(node)
        enclosing: set[ast.AST | None] = {scope}
        while scope is not None:
            scope = self.evaluating_scope(scope)
            if not isinstance(scope, ast.ClassDef):  # a class namespace is no closure
                enclosing.add(scope)
        return bool(bindings[node.id] & enclosing) and not self._rebound_locally(node)

    def _rebound_locally(self, node: ast.Name) -> bool:
        scope = self.evaluating_scope(node)
        if scope is None:
            return False
        if isinstance(scope, ast.ClassDef):
            class_stores = {
                n.id for n in _shallow_walk(scope) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
            }
            return node.id in class_stores
        args = scope.args
        params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg) if a}
        stores = {n.id for n in ast.walk(scope) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        return node.id in params | stores

    def is_os(self, node: ast.expr) -> bool:
        return self._bound(node, self.os_bindings)

    def is_mutator(self, node: ast.expr) -> bool:
        """``putenv(...)`` / ``unsetenv(...)`` through a ``from os import`` name."""
        return self._bound(node, self.mutator_bindings)

    def __call__(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Attribute):
            return node.attr == "environ" and self.is_os(node.value)
        return self._bound(node, self.environ_bindings)


def _module_dict(node: ast.expr, is_environ: _Environ) -> bool:
    """``<os>.__dict__`` or ``vars(<os>)``."""
    if isinstance(node, ast.Attribute):
        return node.attr == "__dict__" and is_environ.is_os(node.value)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "vars"
        and len(node.args) == 1
        and is_environ.is_os(node.args[0])
    )


def _replaces_environ(target: ast.expr, is_environ: _Environ) -> bool:
    """An assignment / ``del`` target that rebinds ``os.environ`` itself:
    ``os.environ``, ``os.__dict__["environ"]`` or ``vars(os)["environ"]``
    (the name literal or constant-folded)."""
    if isinstance(target, ast.Attribute):
        return is_environ(target)
    return (
        isinstance(target, ast.Subscript)
        and _module_dict(target.value, is_environ)
        and _folded_str(target.slice) == "environ"
    )


def _setattr_environ(node: ast.AST, is_environ: _Environ) -> bool:
    """``setattr(<os>, "environ", ...)`` / ``delattr(<os>, "environ")`` — the
    attribute name literal or constant-folded (``"envi" + "ron"``)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("setattr", "delattr")
        and len(node.args) == (3 if node.func.id == "setattr" else 2)
        and not node.keywords
        and is_environ.is_os(node.args[0])
        and _folded_str(node.args[1]) == "environ"
    )


def _union_writes_kairix(node: ast.AugAssign, is_environ: _Environ) -> bool:
    """``os.environ |= {...}`` with a literal ``KAIRIX_*`` key — an in-place
    key write (``__ior__`` keeps the same mapping), not a replacement."""
    return (
        isinstance(node.op, ast.BitOr)
        and is_environ(node.target)
        and isinstance(node.value, ast.Dict)
        and any(_is_kairix_literal(key) for key in node.value.keys)
    )


def _shape(node: ast.AST, is_environ: _Environ) -> str | None:
    def subscript_write(target: ast.expr) -> bool:
        return isinstance(target, ast.Subscript) and is_environ(target.value) and _is_kairix_literal(target.slice)

    if isinstance(node, ast.AugAssign):
        if _union_writes_kairix(node, is_environ):
            return "os.environ |= {KAIRIX_*}"
        if subscript_write(node.target):
            return "assign os.environ[KAIRIX_*]"
    elif isinstance(node, _ASSIGNING):
        if isinstance(node, ast.AnnAssign) and node.value is None:
            return None  # a bare annotation assigns nothing
        targets = _store_targets(node)
        if any(_replaces_environ(t, is_environ) for t in targets):
            return _REPLACED
        if any(subscript_write(t) for t in targets):
            return "assign os.environ[KAIRIX_*]"
    elif isinstance(node, ast.Delete):
        targets = _store_targets(node)
        if any(_replaces_environ(t, is_environ) for t in targets):
            return _REPLACED
        if any(subscript_write(t) for t in targets):
            return "del os.environ[KAIRIX_*]"
    elif _setattr_environ(node, is_environ):
        return _REPLACED
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        func, first = node.func, node.args[0]
        if func.attr in _ENV_HELPERS and _is_kairix_literal(first):
            return f"{func.attr}(KAIRIX_*)"
        if func.attr in _ENVIRON_METHODS and is_environ(func.value) and _is_kairix_literal(first):
            return f"os.environ.{func.attr}(KAIRIX_*)"
    return None


_ENVIRON_WRITE_METHODS = {"pop", "setdefault", "update", "clear", "popitem", "__setitem__", "__delitem__"}
_CONFTEST_MODULE_WRITE = "module-level os.environ write in conftest.py (any key)"


def _any_environ_write(node: ast.AST, is_environ: _Environ) -> bool:
    """An ``os.environ`` write of ANY key (subscript store — through any
    assignment, ``for``, comprehension or ``with`` target — / del / ``|=``, a
    mutating method, ``os.environ = ...``, ``setattr(os, "environ", ...)``,
    ``os.__dict__["environ"] = ...``) or an ``os.putenv`` / ``os.unsetenv``
    call — through ``<os>.`` or a ``from os import putenv [as name]`` name."""
    if isinstance(node, ast.AnnAssign) and node.value is None:
        return False  # a bare annotation assigns nothing
    if isinstance(node, (*_ASSIGNING, ast.AugAssign, ast.Delete)):
        targets = _store_targets(node)
        return any(
            is_environ(t) or _replaces_environ(t, is_environ) or (isinstance(t, ast.Subscript) and is_environ(t.value))
            for t in targets
        )
    if _setattr_environ(node, is_environ):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in _ENVIRON_WRITE_METHODS and is_environ(func.value):
                return True
            return func.attr in _OS_MUTATORS and is_environ.is_os(func.value)
        return is_environ.is_mutator(func)
    return False


def _conftest_module_level_writes(tree: ast.AST, is_environ: _Environ) -> set[tuple[int, str]]:
    """Every env write that runs at conftest IMPORT time (outside any function).

    Root-conftest code runs before the runtime guard is configured, so a
    computed key there is invisible to both halves: in a conftest every
    module-level write fails, whatever the key. The session baseline writes
    inside its fixture body (``with allow_baseline_writes():``) instead.
    """
    found = set()
    lazy_aliases = {
        node.name.id
        for node in ast.walk(tree)
        if isinstance(node, ast.TypeAlias) and any(_any_environ_write(n, is_environ) for n in ast.walk(node.value))
    }
    for node in ast.walk(tree):
        if _any_environ_write(node, is_environ) and _runs_at_import(node, is_environ):
            found.add((getattr(node, "lineno", 0), _CONFTEST_MODULE_WRITE))
        elif _evaluates_lazy_alias(node, lazy_aliases) and _runs_at_import(node, is_environ):
            found.add((getattr(node, "lineno", 0), _CONFTEST_MODULE_WRITE))
    return found


def _evaluates_lazy_alias(node: ast.AST, lazy_aliases: set[str]) -> bool:
    """``<alias>.__value__`` — the read that runs a ``type`` alias's value."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__value__"
        and isinstance(node.value, ast.Name)
        and node.value.id in lazy_aliases
    )


def _runs_at_import(node: ast.AST, is_environ: _Environ) -> bool:
    """True unless ``node`` sits inside a function or lambda BODY (a class
    body runs with the ``class`` statement) — or inside
    a lazily evaluated expression: a ``type`` alias value or type parameter,
    or an annotation under ``from __future__ import annotations``.

    Decorators, parameter defaults and annotations are evaluated when the
    ``def`` / ``lambda`` executes, i.e. at module import, so they count as
    import-time code; only the body is deferred to call time. With postponed
    evaluation every annotation is kept as a string and never runs.
    """
    if _in_lazy_expression(node, is_environ.parents, is_environ.deferred_annotations):
        return False
    scope = is_environ.evaluating_scope(node)
    while isinstance(scope, ast.ClassDef):  # a class body runs when the ``class`` statement does
        scope = is_environ.evaluating_scope(scope)
    return scope is None


def _in_lazy_expression(node: ast.AST, parents: dict[ast.AST, ast.AST], deferred_annotations: bool) -> bool:
    child, parent = node, parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.TypeAlias) and child is parent.value:
            return True
        if child in getattr(parent, "type_params", ()):
            return True
        if deferred_annotations:
            if isinstance(parent, (ast.arg, ast.AnnAssign)) and child is parent.annotation:
                return True
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is parent.returns:
                return True
        child, parent = parent, parents.get(parent)
    return False


def _import_time_modules(tree: ast.AST, is_environ: _Environ, base: Path) -> list[tuple[int, Path]]:
    """``(import line, file)`` for every repository module an import that runs
    at module level brings in — a package's ``__init__.py`` files included —
    resolved from the importing file's directory and the repository root.
    Modules named in ``pytest_plugins`` count: pytest imports them before
    ``pytest_configure``."""
    found: list[tuple[int, Path]] = []
    for node in ast.walk(tree):
        if not _runs_at_import(node, is_environ):
            continue
        targets: list[tuple[int, str, str | None]]
        if isinstance(node, ast.Import):
            targets = [(0, alias.name, None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets = [(node.level, node.module or "", alias.name) for alias in node.names]
        else:
            targets = [(0, name, None) for name in _pytest_plugin_names(node)]
        for level, module, name in targets:
            roots = [_relative_root(base, level)] if level else [base, _REPO_ROOT]
            for root in dict.fromkeys(roots):
                for path in _module_files(root, module.split(".") if module else [], name):
                    found.append((getattr(node, "lineno", 0), path))
    return found


def _pytest_plugin_names(node: ast.AST) -> list[str]:
    """Literal module names a ``pytest_plugins = [...]`` / ``+= [...]`` /
    ``.append(...)`` / ``.extend(...)`` statement registers."""
    if isinstance(node, (ast.Assign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in targets):
            return _str_literals(node.value)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("append", "extend")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest_plugins"
    ):
        return [name for arg in node.args for name in _str_literals(arg)]
    return []


def _str_literals(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [name for elt in node.elts for name in _str_literals(elt)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _str_literals(node.left) + _str_literals(node.right)
    return []


def _relative_root(base: Path, level: int) -> Path:
    """The package directory a ``from .[.] import`` of ``level`` dots names, from the importing module's directory."""
    return base if level == 1 else base.parents[level - 2]


def _module_files(root: Path, parts: list[str], name: str | None) -> list[Path]:
    """The files executed by importing ``parts`` (and ``name`` under it) from ``root``."""
    files: list[Path] = []
    here = root
    for part in parts:
        here = here / part
        if (here / "__init__.py").is_file():
            files.append(here / "__init__.py")
        elif here.with_suffix(".py").is_file():
            files.append(here.with_suffix(".py"))
            break
        else:
            return []
    if name is not None:
        if (here / name / "__init__.py").is_file():
            files.append(here / name / "__init__.py")
        elif (here / name).with_suffix(".py").is_file():
            files.append((here / name).with_suffix(".py"))
    return files


def _imported_helper_writes(tree: ast.AST, path: Path, is_environ: _Environ) -> set[tuple[int, str]]:
    """Module-level env writes in every module a conftest imports at import
    time, transitively: those modules run in the same pre-configuration
    window as the conftest's own top level. Keyed by the conftest import line."""
    found: set[tuple[int, str]] = set()
    seen = {path.resolve()}
    pending = _import_time_modules(tree, is_environ, path.resolve().parent)
    while pending:
        line, helper = pending.pop()
        helper = helper.resolve()
        if helper in seen:
            continue
        seen.add(helper)
        try:
            helper_tree = ast.parse(helper.read_text(encoding="utf-8"), filename=str(helper))
        except (SyntaxError, OSError):
            continue
        helper_environ = _Environ(helper_tree)
        rel = _display_path(helper, path.resolve().parent)
        for write_line, _ in _conftest_module_level_writes(helper_tree, helper_environ):
            found.add((line, f"module-level os.environ write in imported {rel}:{write_line} (any key)"))
        pending.extend((line, nested) for _, nested in _import_time_modules(helper_tree, helper_environ, helper.parent))
    return found


def _marked_restores(tree: ast.AST, source: str, is_environ: _Environ) -> set[int]:
    """Lines of the guard's own restore: a wholesale assignment inside a
    function body whose source line carries ``F2-RESTORE``."""
    marked = {number for number, text in enumerate(source.splitlines(), 1) if _GUARD_RESTORE_MARKER in text}
    return {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
        and node.lineno in marked
        and _shape(node, is_environ) == _REPLACED
        and not _runs_at_import(node, is_environ)
    }


def _display_path(path: Path, base: Path) -> str:
    for root in (base, _REPO_ROOT):
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            continue
    return str(path)


def _baseline_blocks(tree: ast.AST) -> list[ast.With]:
    """``with allow_baseline_writes():`` blocks — the session baseline's exemption."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == _BASELINE_CONTEXT
            for item in node.items
        )
    ]


def file_violations(path: Path, *, guard_home: Path = _GUARD_HOME) -> list[str]:
    """Every ``line: shape`` KAIRIX_* env write in ``path`` (sorted by line).

    Writes inside ``with allow_baseline_writes():`` are the session baseline's
    (the runtime guard exempts the same block); the block itself is a
    violation anywhere but the root ``tests/conftest.py``. The runtime guard
    (``guard_home``) puts the snapshotted ``os.environ`` back after a test
    deleted or replaced it: in that file alone, a wholesale assignment inside
    a function body whose line carries the ``F2-RESTORE`` marker is exempt.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, OSError):
        return []
    blocks = _baseline_blocks(tree)
    exempt = {line for b in blocks for line in range(b.lineno, (b.end_lineno or b.lineno) + 1)}
    is_environ = _Environ(tree)
    found = {(getattr(node, "lineno", 0), shape) for node in ast.walk(tree) if (shape := _shape(node, is_environ))}
    found = {(line, shape) for line, shape in found if line not in exempt}
    if path.resolve() == guard_home.resolve():
        found -= {(line, _REPLACED) for line in _marked_restores(tree, source, is_environ)}
    if path.name == "conftest.py":
        found |= _conftest_module_level_writes(tree, is_environ)
        found |= _imported_helper_writes(tree, path, is_environ)
    if path.resolve() != _BASELINE_HOME:
        found |= {(b.lineno, f"{_BASELINE_CONTEXT}() outside tests/conftest.py") for b in blocks}
    return [f"{line}: {shape}" for line, shape in sorted(found)]


def file_has_env_monkeypatch(path: Path) -> bool:
    """Return True iff ``path`` writes a KAIRIX_* process-env key in a detected spelling."""
    return bool(file_violations(path))


class F2(FitnessRule):
    """F2 static half as an in-process rule over ``tests/`` (staged runs narrow it)."""

    name = "no-env-monkeypatch"
    remediation = REMEDIATION
    roots = ("tests",)

    def file_has_violation(self, path: Path) -> bool:
        return file_has_env_monkeypatch(path)

    def run(self) -> int:
        found: set[str] = set()
        for path in self.enumerate_files():
            rel = str(self._repo_relative(path))
            if self.is_in_scope(rel):
                found.update(f"{rel}:{violation}" for violation in file_violations(path))
        return int(gate_keys(self.name, found, self.remediation))


def main() -> int:
    return F2().run()


if __name__ == "__main__":
    sys.exit(main())
