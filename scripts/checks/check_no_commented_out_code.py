"""F18: No commented-out code.

Sonar S125: ``#``-prefixed lines that *would* parse as Python statements
are commented-out code. Git history is the archive — commented-out code
accumulates confusion (is this still relevant? was it disabled in a
hurry? is this the intended replacement for the line below?). The fix
is always: delete it. ``git log -p`` retains the prior state if anyone
needs to recover it.

Detection (per-line scan):

  1. Read each ``.py`` file line by line.
  2. Identify contiguous ``#``-prefixed lines (>= 3 in a row) whose
     stripped contents lex as a valid Python statement (``ast.parse``
     succeeds on the dedented union of the comment block).
  3. Skip headers, license blocks, shebangs, docstrings.

Tolerances:

  - A block of 1-2 commented lines is not flagged (could be a TODO or a
    one-line note).
  - A block where one or more lines is plain prose (``# This handles…``)
    is not flagged — the parser will fail and the block is treated as
    documentation.
  - Shebangs (``#!``), encoding cookies, and ``# pyright:``/``# type:``
    directive lines are excluded.

Allow-list: ``.architecture/baseline/no-commented-out-code-files.txt``
grandfathers existing offenders.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate, python_files, repo_relative

MIN_RUN = 3  # require at least N contiguous lines before flagging

REMEDIATION = f"""Refactor to delete commented-out code (git history is
the archive — ``git log -p <file>`` recovers any prior state) to pass.

A run of {MIN_RUN}+ consecutive ``#``-prefixed lines that lex as valid
Python statements is commented-out code (Sonar S125). Real comments
describe WHY in prose; if it parses as Python, it was code.

Pass example:
  # Strip leading slash so we can join cleanly with PathLib.
  path = path.lstrip('/')

Forbidden example:
  # old_path = path.replace('/old/', '/new/')
  # if old_path.startswith('/data'):
  #     old_path = old_path[6:]
  # path = old_path
  path = _new_path(path)

If the code might come back, leave a referenced TODO with a ticket
number ("# TODO #251 — re-enable after refactor") instead of the dead
code itself."""


# Lines we always ignore (shebangs, encoding cookies, directives)
_DIRECTIVE_RE = re.compile(r"^\s*#\s*(!|pyright:|type:\s*ignore|noqa|nosec|pragma:|coding[:=])")

# Lines that look like rule-key boilerplate (# F1: ..., # ── Section ───)
_BOILERPLATE_RE = re.compile(r"^\s*#\s*[─=\-─—]{3,}")


def _strip_comment_prefix(line: str) -> str:
    """Strip leading whitespace + ``#`` + one optional space, preserving
    the rest of the line so indentation inside the comment block is
    retained.
    """
    # Find the # after any leading whitespace
    leading_ws = len(line) - len(line.lstrip())
    rest = line[leading_ws:]
    if not rest.startswith("#"):
        return ""
    rest = rest[1:]
    if rest.startswith(" "):
        rest = rest[1:]
    return rest


def _is_commentlike_directive(line: str) -> bool:
    return bool(_DIRECTIVE_RE.match(line) or _BOILERPLATE_RE.match(line))


def _looks_like_code(block_text: str) -> bool:
    """True if ``block_text`` parses as one or more Python statements.

    We dedent the block first (each line was prefixed with ``#`` plus
    optional space, so all lines lose the same prefix and indentation
    is preserved relative to the leftmost line).
    """
    stripped = block_text.strip("\n")
    if not stripped:
        return False
    # Must contain at least one syntactic anchor (assignment, call, def,
    # import, return, raise, if/for/while) — otherwise this is prose.
    # A bare identifier "foo" parses fine but isn't really code-shaped.
    has_anchor = any(
        marker in stripped
        for marker in ("=", "(", "import ", "def ", "class ", "return", "raise", "if ", "for ", "while ", "with ")
    )
    if not has_anchor:
        return False
    try:
        # Dedent: find min leading whitespace across non-empty lines
        lines = stripped.splitlines()
        non_empty = [line for line in lines if line.strip()]
        if not non_empty:
            return False
        min_indent = min(len(line) - len(line.lstrip()) for line in non_empty)
        dedented = "\n".join(line[min_indent:] if line.strip() else "" for line in lines)
        ast.parse(dedented)
        return True
    except (SyntaxError, ValueError, IndentationError):
        return False


def file_has_violation(path: Path) -> bool:
    """True if ``path`` contains a run of MIN_RUN+ consecutive comment
    lines that lex as Python code.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False

    # Parse the file once so we can identify docstring spans to skip.
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return False
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                start = first.lineno
                end = first.end_lineno or first.lineno
                for line_no in range(start, end + 1):
                    docstring_lines.add(line_no)

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if i + 1 in docstring_lines:
            i += 1
            continue
        stripped = line.strip()
        if not stripped.startswith("#") or _is_commentlike_directive(line):
            i += 1
            continue
        # Start of a potential comment block
        block_lines: list[str] = []
        j = i
        while j < len(lines):
            cur = lines[j]
            cur_strip = cur.strip()
            if not cur_strip.startswith("#") or _is_commentlike_directive(cur):
                break
            if j + 1 in docstring_lines:
                break
            block_lines.append(_strip_comment_prefix(cur))
            j += 1

        if len(block_lines) >= MIN_RUN:
            block_text = "\n".join(block_lines)
            if _looks_like_code(block_text):
                return True
        i = j + 1 if j == i else j  # skip past the block we just inspected

    return False


def main() -> int:
    violations = {repo_relative(p) for p in python_files("kairix") if file_has_violation(p)}
    return gate("no-commented-out-code", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
