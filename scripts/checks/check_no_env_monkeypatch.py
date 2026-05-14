"""F2 detector: no monkeypatch.setenv/setattr/delenv on KAIRIX_* env vars.

Walks every test file via AST and emits the path of any file that calls
``monkeypatch.setenv("KAIRIX_X", ...)`` (or setattr/delenv equivalents)
with a string literal first arg starting with ``KAIRIX_``.

Resolves #217 (prior grep-based detector matched docstring text
containing the literal substring ``monkeypatch.setenv ... KAIRIX_``).

Output: one violation file path per line on stdout, sorted, deduplicated.
Pipes into ``arch_gate`` from ``_lib.sh`` for baseline diff.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_TARGET_METHODS = {"setenv", "setattr", "delenv"}


def _is_monkeypatch_call(call: ast.Call) -> bool:
    """Return True when ``call`` is ``monkeypatch.{setenv,setattr,delenv}(...)``.

    Conservative: only matches the bare receiver name ``monkeypatch``.
    Fixture-renamed variants (``mp``, ``mpatch``) are not detected — by
    convention pytest fixtures use the canonical name.
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _TARGET_METHODS:
        return False
    return isinstance(func.value, ast.Name) and func.value.id == "monkeypatch"


def _first_arg_targets_kairix(call: ast.Call) -> bool:
    """First positional arg is a string literal starting with ``KAIRIX_``."""
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value.startswith("KAIRIX_")
    return False


def file_has_env_monkeypatch(path: Path) -> bool:
    """Return True iff ``path`` calls monkeypatch.{setenv,setattr,delenv}("KAIRIX_...")."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_monkeypatch_call(node) and _first_arg_targets_kairix(node):
            return True
    return False


def main() -> int:
    root = Path("tests")
    if not root.is_dir():
        return 0

    violators: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if file_has_env_monkeypatch(path):
            violators.append(str(path))

    for v in violators:
        print(v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
