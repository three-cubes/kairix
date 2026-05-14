"""F16: Cognitive complexity ≤ 15 per function.

Cognitive complexity (Campbell, SonarSource S3776) measures how hard a
function is to *read*, not how hard it is to test. The score climbs with
each branch (``if``, ``elif``, ``else``, ``for``, ``while``, ``try``,
``except``, ``with``, ``and``, ``or``, ternary) and is amplified by
nesting depth — a triple-nested ``if`` is harder to follow than three
sequential ``if`` statements.

The threshold 15 mirrors SonarSource's default ceiling. Functions above
15 are flagged at file scope; the file is the unit baselined.

Detection (AST walk):

  - For each ``FunctionDef`` / ``AsyncFunctionDef`` in a file, compute
    a cognitive-complexity score using the simple per-construct counter
    described below. If ANY function in the file scores > 15, the file
    is a violation.

Score additions:

  - +1 per ``if`` / ``elif`` / ``else``
  - +1 per ``for`` / ``while`` (loop)
  - +1 per ``try`` (with each ``except`` clause as +1)
  - +1 per boolean operator inside conditions (``and`` / ``or``)
  - +1 per ternary (``IfExp``)
  - +nesting_depth on every branch construct (the nesting amplifier)

Allow-list: ``.architecture/baseline/cognitive-complexity-files.txt``
grandfathers existing offenders. Net-new violations block at pre-commit
and CI.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate, python_files, repo_relative

THRESHOLD = 15

REMEDIATION = f"""Refactor to bring each function's cognitive-complexity
score below {THRESHOLD} — extract helper functions, use early-return,
and/or replace if/elif chains with a strategy/dispatch dict — to pass.

Cognitive complexity measures how hard the code is to READ (Campbell,
SonarSource S3776). It rises with every branch and is amplified by
nesting. The fix is almost always: extract a helper, return early, or
replace nested conditionals with a dispatch dict.

Pass example:
  _HANDLERS = {{
      'search': _handle_search,
      'index': _handle_index,
      'rebuild': _handle_rebuild,
  }}

  def dispatch(cmd: str, args: list[str]) -> int:
      handler = _HANDLERS.get(cmd, _default_handler)
      return handler(args)

Forbidden example:
  def dispatch(cmd: str, args: list[str]) -> int:
      if cmd == 'search':
          if not args:
              ...                            # +1 if, +1 nested
          else:
              for item in args:
                  if item.startswith('-'):
                      ...                    # +1 for, +1 if, +1 nested twice
      elif cmd == 'index':                   # +1 elif
          ...
      elif cmd == 'rebuild':
          ...

See ``kairix/worker.py::WorkerDeps`` for the dataclass-extraction
pattern that flattens orchestrator complexity by moving collaborators
onto a single ``Deps`` object."""


class _Scorer(ast.NodeVisitor):
    """Walks a function body and accumulates a cognitive-complexity
    score. The ``nesting`` counter rises on every branch construct and
    is added to each subsequent branch encountered inside it.
    """

    def __init__(self) -> None:
        self.score = 0
        self.nesting = 0

    def _bump(self, amount: int = 1) -> None:
        self.score += amount + self.nesting

    def _bump_flat(self) -> None:
        """Bump by 1 (no nesting amplifier) — used for boolean operators
        within a condition, which add to the score but do not themselves
        increase nesting.
        """
        self.score += 1

    def _walk_nested(self, body: list[ast.stmt]) -> None:
        self.nesting += 1
        for child in body:
            self.visit(child)
        self.nesting -= 1

    # Branch constructs that bump AND increase nesting for their body
    def visit_If(self, node: ast.If) -> None:
        self._bump()
        self._walk_nested(node.body)
        # elif chains are nested If nodes inside orelse; else (non-elif)
        # is a plain list of stmts in orelse but still counts +1 once
        if node.orelse:
            # Distinguish elif (single If in orelse) from else (other body)
            if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                # elif — already counted by recursive visit_If
                self.visit(node.orelse[0])
            else:
                self._bump()
                self._walk_nested(node.orelse)

    def visit_For(self, node: ast.For) -> None:
        self._bump()
        self._walk_nested(node.body)
        if node.orelse:
            self._walk_nested(node.orelse)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)  # type: ignore[arg-type]  # AsyncFor has identical attrs

    def visit_While(self, node: ast.While) -> None:
        self._bump()
        self._walk_nested(node.body)
        if node.orelse:
            self._walk_nested(node.orelse)

    def visit_Try(self, node: ast.Try) -> None:
        self._bump()
        self._walk_nested(node.body)
        for handler in node.handlers:
            self._bump()
            self._walk_nested(handler.body)
        if node.orelse:
            self._walk_nested(node.orelse)
        if node.finalbody:
            self._walk_nested(node.finalbody)

    def visit_With(self, node: ast.With) -> None:
        # ``with`` does NOT add to cognitive complexity by SonarSource's
        # rule (it doesn't branch). We descend without bumping.
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.generic_visit(node)

    # Inline constructs that bump without amplifier
    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # +1 per boolean operator (and/or). A chain like ``a and b and c``
        # has two operators → +2.
        if isinstance(node.op, (ast.And, ast.Or)):
            self._bump_flat()
            for _ in node.values[1:]:
                self._bump_flat()
        # Subtract 1 because we double-counted the first operand by
        # walking the chain — keep it simple: every BoolOp adds
        # len(values) - 1.
        # Reset: the loop above already adds len(values) flat bumps;
        # the correct count is len(values) - 1.
        self._bump_flat_correction(node)
        self.generic_visit(node)

    def _bump_flat_correction(self, node: ast.BoolOp) -> None:
        # The visit_BoolOp loop adds len(node.values) flat bumps; the
        # correct cognitive cost is len(node.values) - 1. Remove one.
        self.score -= 1

    def visit_IfExp(self, node: ast.IfExp) -> None:
        # ternary: x if cond else y → +1
        self._bump()
        self.generic_visit(node)

    # Recurse into nested function defs as separate scoring units? We
    # treat nested defs as part of the parent's score by walking through
    # them — the test "this function is hard to read" applies to the
    # outer.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.generic_visit(node)


def _score_function(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    scorer = _Scorer()
    for stmt in func.body:
        scorer.visit(stmt)
    return scorer.score


def file_has_violation(path: Path) -> bool:
    """True if any function in ``path`` scores > THRESHOLD."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _score_function(node) > THRESHOLD:
                return True
    return False


def main() -> int:
    violations = {repo_relative(p) for p in python_files("kairix") if file_has_violation(p)}
    return gate("cognitive-complexity", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
