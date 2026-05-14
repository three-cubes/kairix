"""F17: No string literal of ≥10 chars duplicated ≥3 times in a module.

Sonar S1192: a duplicated string literal is a refactor smell — the
reader can't tell whether the three sites are coupled (they all
reference the same conceptual thing and should change together) or
coincidentally identical (renaming one shouldn't affect the others).
Extracting to a module-level UPPER_SNAKE_CASE constant makes the
coupling explicit and gives renaming a single edit site.

Detection (AST walk over each module):

  1. Collect every ``ast.Constant`` whose value is a ``str`` and whose
     length is ≥ ``MIN_LENGTH``.
  2. Skip docstrings (first stmt of Module / Class / FunctionDef) and
     ``__all__`` / ``__doc__`` assignments.
  3. Skip f-string components — those live in JoinedStr nodes and have
     their own conventions.
  4. Count occurrences per literal value within the module.
  5. Flag if any value appears ≥ ``MIN_OCCURRENCES`` times.

Allow-list: ``.architecture/baseline/no-duplicate-string-files.txt``
grandfathers existing offenders.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate, python_files, repo_relative

MIN_LENGTH = 10  # only flag literals at least this long
MIN_OCCURRENCES = 3  # only flag literals duplicated this many times

REMEDIATION = f"""Refactor to extract each ≥{MIN_LENGTH}-char string
literal duplicated ≥{MIN_OCCURRENCES} times into a module-level
UPPER_SNAKE_CASE constant near the top of the file, and replace every
occurrence with the constant name — to pass.

Pass example:
  # near the top of the module
  _ERROR_BAD_QUERY = "search query must be a non-empty string"

  def search(q: str) -> list[Hit]:
      if not q:
          raise ValueError(_ERROR_BAD_QUERY)

  def reindex(q: str) -> None:
      if not q:
          raise ValueError(_ERROR_BAD_QUERY)

Forbidden example:
  def search(q: str) -> list[Hit]:
      if not q:
          raise ValueError("search query must be a non-empty string")
  def reindex(q: str) -> None:
      if not q:
          raise ValueError("search query must be a non-empty string")
  def validate(q: str) -> None:
      if not q:
          raise ValueError("search query must be a non-empty string")

The extracted constant makes the coupling between sites explicit — when
the message text changes, only one edit is required and every site
updates."""


def _collect_docstring_nodes(tree: ast.AST) -> set[int]:
    """Return the ``id()`` of every Constant node that is a docstring."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                out.add(id(first.value))
    return out


def file_has_violation(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    docstring_ids = _collect_docstring_nodes(tree)
    counts: Counter[str] = Counter()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, str):
            continue
        if id(node) in docstring_ids:
            continue
        value = node.value
        if len(value) < MIN_LENGTH:
            continue
        # Skip whitespace-only / formatting strings (e.g. "    ", "\n\n")
        if not value.strip():
            continue
        counts[value] += 1

    return any(c >= MIN_OCCURRENCES for c in counts.values())


def main() -> int:
    violations = {repo_relative(p) for p in python_files("kairix") if file_has_violation(p)}
    return gate("no-duplicate-string", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
