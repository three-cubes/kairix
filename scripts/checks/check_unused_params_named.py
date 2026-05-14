"""F19: Unused function parameters must be prefixed with ``_``.

Sonar S1172: a function parameter that is never read in the body is
either dead code (delete it from the signature) or a Protocol-required
position that the implementation does not need (rename to ``_unused``
so the reader can see the intent and the linter can stop flagging it).

The kairix convention: an unused parameter is **renamed to ``_x``** if
the function satisfies a Protocol that requires the positional slot;
otherwise it is **deleted** from the signature.

Detection (AST walk):

  1. For each ``FunctionDef`` / ``AsyncFunctionDef`` in ``kairix/**``:
     collect parameter names from ``args.args``, ``args.kwonlyargs``,
     ``args.posonlyargs``. Skip ``*args`` / ``**kwargs`` (they're
     opaque-by-design).
  2. Skip parameters already named ``self``, ``cls``, or prefixed with
     ``_`` — those are exempt.
  3. Walk the function body. If a parameter name is never referenced as
     an ``ast.Name`` (or as ``self.<name>`` / ``cls.<name>``), it's
     unused.
  4. A file is a violation if it contains any unused, non-underscore
     parameter.

Special cases:

  - Abstract methods (``@abstractmethod``, body is ``...`` or ``pass``)
    are exempt — the signature is the contract.
  - Functions whose entire body is ``raise NotImplementedError`` are
    exempt.
  - Property setters whose parameter name is ``value`` are exempt
    (Pythonic convention).
  - Functions decorated with ``@overload`` are exempt (stubs only).

Allow-list: ``.architecture/baseline/unused-params-named-files.txt``
grandfathers existing offenders.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate, python_files, repo_relative

REMEDIATION = """Refactor to either DELETE the unused parameter from the
signature or rename it to ``_unused`` (or any ``_``-prefixed name) if the
position is required by a Protocol/abstract base — to pass.

The ``_``-prefix is the explicit signal that the unused parameter is
load-bearing for the contract, not just leftover code.

Pass example:
  # Protocol requires (event, context); this impl only needs event.
  def handle(event: Event, _context: Context) -> Result:
      return Result(event.id)

  # OR: delete the parameter if no Protocol requires it.
  def handle(event: Event) -> Result:
      return Result(event.id)

Forbidden example:
  def handle(event: Event, context: Context) -> Result:  # context unused
      return Result(event.id)

Exemptions: ``self``, ``cls``, ``*args``, ``**kwargs``, abstract methods
(body = ``...`` / ``pass`` / ``raise NotImplementedError``), property
setters (``value``), and ``@overload`` stubs are all skipped."""


_EXEMPT_NAMES = frozenset({"self", "cls"})


def _is_abstract_or_overload(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function is decorated as abstract / overload, or its
    body is a single ``...`` / ``pass`` / ``raise NotImplementedError``.
    """
    for d in func.decorator_list:
        name = d.attr if isinstance(d, ast.Attribute) else (d.id if isinstance(d, ast.Name) else None)
        if name in {"abstractmethod", "abstractproperty", "overload"}:
            return True
        if isinstance(d, ast.Call):
            inner = d.func
            inner_name = inner.attr if isinstance(inner, ast.Attribute) else (inner.id if isinstance(inner, ast.Name) else None)
            if inner_name in {"abstractmethod", "abstractproperty", "overload"}:
                return True
    if len(func.body) == 1:
        only = func.body[0]
        if isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant):
            # docstring-only (string) or ``...`` (Ellipsis)
            return True
        if isinstance(only, ast.Pass):
            return True
        if isinstance(only, ast.Raise) and isinstance(only.exc, (ast.Name, ast.Call)):
            target = only.exc.func if isinstance(only.exc, ast.Call) else only.exc
            if isinstance(target, ast.Name) and target.id == "NotImplementedError":
                return True
    return False


def _is_property_setter(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function is decorated with ``@<x>.setter``."""
    for d in func.decorator_list:
        if isinstance(d, ast.Attribute) and d.attr == "setter":
            return True
    return False


def _collect_names_referenced(body: list[ast.stmt]) -> set[str]:
    """Collect every name read (Load context) anywhere in ``body``."""
    refs: set[str] = set()
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                refs.add(node.id)
            # ``del x`` and ``x: int`` (annotation-only) still count as use
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Del, ast.Store)):
                # Assignment doesn't count as a read, but capture for completeness.
                # We only consider Load context as "used".
                pass
    return refs


def _function_has_unused_param(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if _is_abstract_or_overload(func) or _is_property_setter(func):
        return False

    args = func.args
    all_args = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    if not all_args:
        return False

    refs = _collect_names_referenced(func.body)

    for arg in all_args:
        name = arg.arg
        if name in _EXEMPT_NAMES:
            continue
        if name.startswith("_"):
            continue  # explicitly marked unused
        if name not in refs:
            return True
    return False


def file_has_violation(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _function_has_unused_param(node):
                return True
    return False


def main() -> int:
    violations = {repo_relative(p) for p in python_files("kairix") if file_has_violation(p)}
    return gate("unused-params-named", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
