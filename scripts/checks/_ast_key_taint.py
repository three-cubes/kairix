"""Shared AST helpers for the F1 / F2 detectors: resolve a mapping KEY.

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

So the key resolver runs a small module-wide *taint* pass: a name is
tainted when it is bound (assignment, ``for`` / comprehension target, or a
function whose ``return`` yields one) from an expression that contains a
protected string literal or another tainted name. The pass iterates to a
fixpoint, so taint flows through any number of hops.

Conservative by construction: a name reused for an unrelated value in the
same module is still treated as tainted. That only ever produces a
violation the author resolves by injecting the value through a seam — the
fix the gate asks for anyway.
"""

from __future__ import annotations

import ast
from collections.abc import Callable

StringPredicate = Callable[[str], bool]


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


def _contains_protected(expr: ast.AST, is_protected: StringPredicate, tainted: set[str]) -> bool:
    """``expr`` mentions a protected string literal or a tainted name anywhere."""
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and is_protected(node.value):
            return True
        if isinstance(node, ast.Name) and node.id in tainted:
            return True
    return False


def _bindings(tree: ast.AST) -> list[tuple[list[str], ast.AST]]:
    """Every (bound names, source expression) pair the taint pass inspects."""
    pairs: list[tuple[list[str], ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [n for t in node.targets for n in _target_names(t)]
            pairs.append((names, node.value))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            pairs.append((_target_names(node.target), node.value))
        elif isinstance(node, ast.NamedExpr):
            pairs.append((_target_names(node.target), node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            pairs.append((_target_names(node.target), node.iter))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return) and ret.value is not None:
                    pairs.append(([node.name], ret.value))
    return pairs


def tainted_names(tree: ast.AST, is_protected: StringPredicate) -> set[str]:
    """Names that can hold a protected key, propagated to a fixpoint."""
    pairs = _bindings(tree)
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for names, source in pairs:
            fresh = [n for n in names if n not in tainted]
            if fresh and _contains_protected(source, is_protected, tainted):
                tainted.update(fresh)
                changed = True
    return tainted


def key_is_protected(expr: ast.expr, is_protected: StringPredicate, tainted: set[str]) -> bool:
    """Can the key expression ``expr`` evaluate to a protected name?

    Matches a protected string literal, an f-string / concatenation whose
    leading literal is protected-prefixed, or a tainted name.
    """
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return is_protected(expr.value)
    if isinstance(expr, ast.Name):
        return expr.id in tainted
    if isinstance(expr, ast.JoinedStr) and expr.values:
        return key_is_protected(expr.values[0], is_protected, tainted)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return key_is_protected(expr.left, is_protected, tainted)
    return False


def module_aliases(tree: ast.AST, module: str) -> set[str]:
    """Local names bound to stdlib ``module`` (``import os`` / ``import os as _os``)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module:
                    names.add(alias.asname or alias.name)
    return names


def from_imports(tree: ast.AST, module: str, attr: str) -> set[str]:
    """Local names bound by ``from <module> import <attr> [as x]``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            for alias in node.names:
                if alias.name == attr:
                    names.add(alias.asname or alias.name)
    return names


def is_module_attr(expr: ast.expr, module_names: set[str], attr: str, direct_names: set[str]) -> bool:
    """``expr`` is ``<module alias>.<attr>`` or a name bound by ``from <module> import <attr>``."""
    if isinstance(expr, ast.Attribute) and expr.attr == attr:
        return isinstance(expr.value, ast.Name) and expr.value.id in module_names
    return isinstance(expr, ast.Name) and expr.id in direct_names


def parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Child -> parent map for lexical-context questions."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def enclosing_function(parents: dict[ast.AST, ast.AST], node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Innermost function definition lexically containing ``node``."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None
