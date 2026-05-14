"""F1 detector: no @patch / with patch on kairix internals.

Walks every test file via AST and emits the path of any file that uses
``@patch("kairix.X.Y", ...)`` or ``with patch("kairix.X.Y", ...):`` as a
decorator/context manager, regardless of line layout.

Resolves #214 (multi-line ``with patch(\\n  "kairix...")`` escaped the
prior grep-based detector).

Output: one violation file path per line on stdout, sorted, deduplicated.
Pipes into ``arch_gate`` from ``_lib.sh`` for baseline diff.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_patch_call(node: ast.expr) -> bool:
    """Return True when ``node`` is a Call to ``patch`` or ``mock.patch`` /
    ``unittest.mock.patch`` (or aliases like ``mock_patch``).

    Conservative: only matches the literal name ``patch`` or attribute
    access ending in ``.patch``. Other helpers (``patch.dict``,
    ``patch.object``) are NOT covered by F1 — they have a different
    arg shape and are tracked separately if needed.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "patch"
    if isinstance(func, ast.Attribute):
        return func.attr == "patch"
    return False


def _first_arg_is_kairix_internal(call: ast.Call) -> bool:
    """First positional arg of patch(...) is a string starting with ``kairix.``."""
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value.startswith("kairix.")
    return False


def file_has_internal_patch(path: Path) -> bool:
    """Return True iff ``path`` contains @patch or with patch on a kairix.* target."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return False

    for node in ast.walk(tree):
        # Decorator: @patch("kairix.X.Y")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for deco in node.decorator_list:
                if _is_patch_call(deco) and _first_arg_is_kairix_internal(deco):
                    return True
        # Context manager: with patch("kairix.X.Y"): ...
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if _is_patch_call(ctx) and _first_arg_is_kairix_internal(ctx):
                    return True
    return False


def main() -> int:
    root = Path("tests")
    if not root.is_dir():
        return 0

    violators: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if file_has_internal_patch(path):
            violators.append(str(path))

    for v in violators:
        print(v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
