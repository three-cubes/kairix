"""Shared AST helpers for the F1 / F2 detectors: index a module, resolve a KEY.

F1 (``sys.modules`` swaps of ``kairix.*`` modules) and F2 (``os.environ``
writes of ``KAIRIX_*`` keys) both need to answer the same question for a
subscript / ``.pop`` / ``.setdefault`` key expression: *can this key be one
of the protected names?* A bare string literal is the easy case, but the
evasions the detectors exist to close move the literal one hop away:

    var = "KAIRIX_DB_PATH"
    os.environ.pop(var, None)

    for name in ("KAIRIX_DATA_DIR", "KAIRIX_CACHE_DIR"):
        os.environ[name] = ...

    saved = {k: os.environ.pop(k, None) for k in _SECRET_VARS}

    os.environ["KAIRIX" + "_DB_PATH"] = ...       # constant-folded

So the key resolver (a) folds constant string expressions (``+``, f-strings,
``%`` / ``.format`` on constants) and, for a partially constant one, tests
the constant leading prefix of the whole value; and (b) runs a small
module-wide *taint* pass: a name is tainted when it is bound (assignment,
``for`` / comprehension target, or a function whose ``return`` yields one)
from an expression that is or contains a protected key or another tainted
name. The pass iterates to a fixpoint, so taint flows through any number of
hops.

Performance: every per-file question is answered from one :class:`ModuleIndex`
built by a SINGLE traversal (parents, imports, bindings, ``with`` targets and
the candidate write statements), so the detectors parse each file once and
never re-walk the tree per query.

Conservative by construction: a name reused for an unrelated value in the
same module is still treated as tainted. That only ever produces a
violation the author resolves by injecting the value through a seam — the
fix the gate asks for anyway.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# What counts as a protected key.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtectedKeys:
    """A protected key namespace: exact names and/or required prefixes.

    ``keys(value)`` answers for a COMPLETE value. :meth:`could_complete`
    answers for a known leading prefix of a partially constant value: can
    some completion of it be protected?
    """

    prefixes: tuple[str, ...] = ()
    exact: tuple[str, ...] = ()

    def __call__(self, value: str) -> bool:
        return value in self.exact or value.startswith(self.prefixes)

    def could_complete(self, prefix: str) -> bool:
        if not prefix:
            return False
        return any(p.startswith(prefix) or prefix.startswith(p) for p in (*self.prefixes, *self.exact))


# ---------------------------------------------------------------------------
# One-pass module index.
# ---------------------------------------------------------------------------

_CANDIDATE_TYPES = (ast.Call, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)
_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _target_names(target: ast.expr) -> list[str]:
    """Every plain ``Name`` bound by an assignment / loop target."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_target_names(elt))
        return names
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


@dataclass
class ModuleIndex:
    """Everything the F1 / F2 queries need, collected in one traversal."""

    tree: ast.AST
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)
    imports: list[ast.Import] = field(default_factory=list)
    import_froms: list[ast.ImportFrom] = field(default_factory=list)
    #: (bound names, source expression) for the taint pass
    bindings: list[tuple[list[str], ast.expr]] = field(default_factory=list)
    #: plain ``name = value`` bindings (Assign / AnnAssign / walrus) for alias resolution
    name_bindings: list[tuple[str, ast.expr]] = field(default_factory=list)
    #: ``with <ctx> as <name>`` targets
    with_targets: list[tuple[str, ast.expr]] = field(default_factory=list)
    #: statements / calls that can write a mapping or patch an attribute
    candidates: list[ast.AST] = field(default_factory=list)

    @classmethod
    def build(cls, tree: ast.AST) -> ModuleIndex:
        index = cls(tree)
        returns: list[ast.Return] = []
        stack: list[ast.AST] = [tree]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                index.parents[child] = node
                stack.append(child)
            index._classify(node, returns)
        for ret in returns:
            fn = enclosing_function(index.parents, ret)
            if fn is not None and ret.value is not None:
                index.bindings.append(([fn.name], ret.value))
        return index

    def _classify(self, node: ast.AST, returns: list[ast.Return]) -> None:
        if isinstance(node, _CANDIDATE_TYPES):
            self.candidates.append(node)
        if isinstance(node, ast.Import):
            self.imports.append(node)
        elif isinstance(node, ast.ImportFrom):
            self.import_froms.append(node)
        elif isinstance(node, ast.Assign):
            self.bindings.append(([n for t in node.targets for n in _target_names(t)], node.value))
            self.name_bindings.extend((t.id, node.value) for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value is not None:
            self.bindings.append((_target_names(node.target), node.value))
            if not isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                self.name_bindings.append((node.target.id, node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            self.bindings.append((_target_names(node.target), node.iter))
        elif isinstance(node, ast.Return):
            returns.append(node)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            self.with_targets.extend(
                (item.optional_vars.id, item.context_expr)
                for item in node.items
                if isinstance(item.optional_vars, ast.Name)
            )

    def module_aliases(self, module: str) -> set[str]:
        """Local names bound to stdlib ``module`` (``import os`` / ``import os as _os``)."""
        return {a.asname or a.name for node in self.imports for a in node.names if a.name == module}

    def from_imports(self, module: str, attr: str) -> set[str]:
        """Local names bound by ``from <module> import <attr> [as x]``."""
        return {
            a.asname or a.name
            for node in self.import_froms
            if node.module == module
            for a in node.names
            if a.name == attr
        }


# ---------------------------------------------------------------------------
# Constant folding.
# ---------------------------------------------------------------------------


def _literal(expr: ast.expr) -> object:
    try:
        return ast.literal_eval(expr)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return _NOT_LITERAL


_NOT_LITERAL = object()


def fold_string(expr: ast.expr) -> tuple[str, bool] | None:
    """Fold a constant string expression.

    Returns ``(text, complete)``: ``complete`` is True when the whole value is
    known; otherwise ``text`` is the constant LEADING prefix of the value.
    ``None`` when nothing is known (a bare name, a call, ...). Folds string
    literals, ``+`` concatenation, f-strings (constant parts and constant
    ``{...}`` fields), and ``%`` / ``.format`` applied to a constant template
    with literal arguments.
    """
    if isinstance(expr, ast.Constant):
        return (expr.value, True) if isinstance(expr.value, str) else None
    if isinstance(expr, ast.JoinedStr):
        return _fold_joined(expr)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return _fold_concat(expr)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Mod):
        return _fold_template(expr.left, lambda tpl: tpl % _require_literal(expr.right), "%")
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "format":
        return _fold_format(expr)
    return None


def _require_literal(expr: ast.expr) -> object:
    value = _literal(expr)
    if value is _NOT_LITERAL:
        raise ValueError("not a literal")
    return value


def _fold_joined(expr: ast.JoinedStr) -> tuple[str, bool] | None:
    text = ""
    for part in expr.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            text += part.value
            continue
        if isinstance(part, ast.FormattedValue) and part.format_spec is None and part.conversion in (-1, ord("s")):
            inner = fold_string(part.value)
            if inner is not None and inner[1]:
                text += inner[0]
                continue
            return (text + inner[0], False) if inner is not None else ((text, False) if text else None)
        return (text, False) if text else None
    return (text, True)


def _fold_concat(expr: ast.BinOp) -> tuple[str, bool] | None:
    left = fold_string(expr.left)
    if left is None or not left[1]:
        return left
    right = fold_string(expr.right)
    if right is None:
        return (left[0], False)
    return (left[0] + right[0], right[1])


def _fold_template(template_expr: ast.expr, apply: Callable[[str], object], marker: str) -> tuple[str, bool] | None:
    template = fold_string(template_expr)
    if template is None or not template[1]:
        return template
    try:
        result = apply(template[0])
    except (ValueError, TypeError, KeyError, IndexError):
        return (template[0].split(marker, 1)[0], False)
    return (result, True) if isinstance(result, str) else None


def _fold_format(expr: ast.Call) -> tuple[str, bool] | None:
    def apply(tpl: str) -> str:
        args = [_require_literal(a) for a in expr.args]
        kwargs = {kw.arg: _require_literal(kw.value) for kw in expr.keywords if kw.arg is not None}
        if any(kw.arg is None for kw in expr.keywords):
            raise ValueError("** spread")
        return tpl.format(*args, **kwargs)

    assert isinstance(expr.func, ast.Attribute)
    return _fold_template(expr.func.value, apply, "{")


# ---------------------------------------------------------------------------
# Taint + key resolution.
# ---------------------------------------------------------------------------


def _mentions_protected(expr: ast.AST, keys: ProtectedKeys, tainted: set[str]) -> bool:
    """``expr`` is a protected key or mentions a protected literal / tainted name anywhere."""
    for node in ast.walk(expr):
        if isinstance(node, ast.expr) and key_is_protected(node, keys, tainted):
            return True
    return False


def tainted_names(index: ModuleIndex, keys: ProtectedKeys) -> set[str]:
    """Names that can hold a protected key, propagated to a fixpoint."""
    tainted: set[str] = set()
    pending = [(names, source) for names, source in index.bindings if names]
    changed = True
    while changed:
        changed = False
        still: list[tuple[list[str], ast.expr]] = []
        for names, source in pending:
            if _mentions_protected(source, keys, tainted):
                tainted.update(names)
                changed = True
            else:
                still.append((names, source))
        pending = still
    return tainted


def key_is_protected(expr: ast.expr, keys: ProtectedKeys, tainted: set[str]) -> bool:
    """Can the key expression ``expr`` evaluate to a protected name?

    Folds constant string expressions first: a complete value is tested
    directly; a partially constant one is protected when its constant
    leading prefix could complete to a protected key. Otherwise matches a
    tainted name, a call to a tainted helper (one whose ``return`` yields a
    protected key), or a dynamic expression whose leading part is one.
    """
    folded = fold_string(expr)
    if folded is not None:
        text, complete = folded
        if complete:
            return keys(text)
        if text:
            return keys.could_complete(text)
    if isinstance(expr, ast.Name):
        return expr.id in tainted
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Name):
            return func.id in tainted
        if isinstance(func, ast.Attribute):
            return func.attr in tainted
    if isinstance(expr, ast.JoinedStr) and expr.values and isinstance(expr.values[0], ast.FormattedValue):
        return key_is_protected(expr.values[0].value, keys, tainted)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return key_is_protected(expr.left, keys, tainted)
    return False


def is_module_attr(expr: ast.expr, module_names: set[str], attr: str, direct_names: set[str]) -> bool:
    """``expr`` is ``<module alias>.<attr>`` or a name bound by ``from <module> import <attr>``."""
    if isinstance(expr, ast.Attribute) and expr.attr == attr:
        return isinstance(expr.value, ast.Name) and expr.value.id in module_names
    return isinstance(expr, ast.Name) and expr.id in direct_names


def enclosing_function(parents: dict[ast.AST, ast.AST], node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Innermost function definition lexically containing ``node``."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, _FUNCTION_TYPES):
            return current
        current = parents.get(current)
    return None


def parse_index(path: Path, prefilter: re.Pattern[str] | None = None) -> ModuleIndex | None:
    """Read + parse ``path`` once and index it; ``None`` when unreadable, a
    syntax error, or the cheap ``prefilter`` regex finds no relevant token."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if prefilter is not None and not prefilter.search(text):
        return None
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return None
    return ModuleIndex.build(tree)
