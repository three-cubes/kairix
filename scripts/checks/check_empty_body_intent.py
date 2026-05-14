"""F20: Empty function bodies must declare intent (docstring OR comment).

Sonar S1186: a function body that is exactly ``pass`` or an empty
ellipsis with no documentation is a confusion vector. The reader can't
tell whether this is:

  - An abstract method whose contract is the signature.
  - A no-op satisfying a Protocol.
  - An accidentally-truncated function that should have logic.

The fix: add a one-line docstring describing the Protocol contract this
satisfies, OR an ``# Intentionally empty — <reason>`` comment.

Detection (AST walk):

  1. For each ``FunctionDef`` / ``AsyncFunctionDef`` in ``kairix/**``:
     - If body is exactly ``[ast.Pass()]`` AND no docstring AND no
       intent comment immediately above → violation.
     - If body is exactly ``[ast.Expr(Constant Ellipsis)]`` AND no
       docstring AND no intent comment → violation.
  2. A docstring counts when the body is
     ``[Expr(Constant(str))]`` (alone) OR
     ``[Expr(Constant(str)), Pass()]`` (docstring + pass).
  3. An ``# Intentionally empty —`` comment on the line above the
     function (or on the first line of the body before ``pass``)
     also satisfies the rule.

Exemptions:

  - ``@abstractmethod`` decorated functions (signature IS the contract).
  - ``@overload`` decorated functions (stubs only).
  - Functions whose body is ``raise NotImplementedError`` (explicit
    abstract).

Allow-list: ``.architecture/baseline/empty-body-intent-files.txt``
grandfathers existing offenders.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate, python_files, repo_relative

REMEDIATION = """Refactor to add a one-line docstring (or an
``# Intentionally empty — <reason>`` comment) to each empty function
body — to pass.

fix: add either a one-line docstring describing the Protocol contract
the function satisfies, or an ``# Intentionally empty — <reason>``
comment that explains why the body is genuinely a no-op.
next: re-run ``python3 scripts/checks/check_empty_body_intent.py`` to
confirm the gate goes green.
run: bash scripts/safe-commit.sh "docs(<area>): document intent of empty <function>"

Pass example:
  def on_event(self, event: Event) -> None:
      \"\"\"No-op default; concrete strategies override this.\"\"\"

  def shutdown(self) -> None:
      # Intentionally empty — graceful-shutdown is a Protocol-required
      # method that some adapters genuinely don't need.
      pass

  @abstractmethod
  def fetch(self) -> Hits:
      ...

Forbidden example:
  def on_event(self, event: Event) -> None:
      pass

  def shutdown(self) -> None: ...

An empty body without explanation is indistinguishable from a
truncated/forgotten implementation. The docstring or intent comment is
the receipt that the emptiness is deliberate."""


def _is_abstract_or_overload(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for d in func.decorator_list:
        name = d.attr if isinstance(d, ast.Attribute) else (d.id if isinstance(d, ast.Name) else None)
        if name in {"abstractmethod", "abstractproperty", "overload"}:
            return True
        if isinstance(d, ast.Call):
            inner = d.func
            inner_name = (
                inner.attr if isinstance(inner, ast.Attribute) else (inner.id if isinstance(inner, ast.Name) else None)
            )
            if inner_name in {"abstractmethod", "abstractproperty", "overload"}:
                return True
    return False


def _has_docstring(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if not func.body:
        return False
    first = func.body[0]
    return isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)


def _is_empty_body(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function body is exactly ``pass``, ``...``,
    ``docstring + pass``, or ``docstring + ...``.
    """
    body = func.body
    if len(body) == 1:
        only = body[0]
        if isinstance(only, ast.Pass):
            return True
        if isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant) and only.value.value is Ellipsis:
            return True
        return False
    if len(body) == 2:
        first, second = body
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            # docstring + pass / ...
            if isinstance(second, ast.Pass):
                return True
            if (
                isinstance(second, ast.Expr)
                and isinstance(second.value, ast.Constant)
                and second.value.value is Ellipsis
            ):
                return True
    return False


def _has_intent_comment(func: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str]) -> bool:
    """Return True if an ``# Intentionally empty`` comment appears within
    the function span or on the line immediately preceding ``def``.
    """
    start = (func.lineno or 1) - 1
    end = func.end_lineno or func.lineno or 1
    # Check span (lines start..end inclusive, 1-indexed → list slice)
    snippet = "\n".join(source_lines[max(start - 1, 0) : end])
    return "Intentionally empty" in snippet


def _function_violates(func: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str]) -> bool:
    if _is_abstract_or_overload(func):
        return False
    if not _is_empty_body(func):
        return False
    if _has_docstring(func):
        return False
    if _has_intent_comment(func, source_lines):
        return False
    return True


def file_has_violation(path: Path) -> bool:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    source_lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _function_violates(node, source_lines):
                return True
    return False


def main() -> int:
    violations = {repo_relative(p) for p in python_files("kairix") if file_has_violation(p)}
    return gate("empty-body-intent", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
