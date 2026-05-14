"""F11: Test-skip mechanisms require rationale.

A silently-skipping test is a worse signal than a missing test — it
looks present, but never runs. The starlette/transport regression
earlier in this branch (F7 measured 0% on a file whose unit tests
silently skipped) is the canonical example.

Detection (AST walk over ``tests/**.py``):

  - ``@pytest.mark.skip`` MUST take a ``reason=`` kwarg. Bare
    ``@pytest.mark.skip`` (no parens, or empty parens, or no reason)
    is rejected.
  - ``@pytest.mark.skipif(condition, reason=...)`` MUST take a
    ``reason=`` kwarg. Bare ``skipif(condition)`` is rejected.
  - ``pytest.importorskip("X")`` MUST be followed (within 3 lines) by
    a comment explaining why the optional dependency model is correct
    here, OR have a ``reason=`` kwarg.
  - ``@pytest.mark.xfail`` MUST take a ``reason=`` kwarg.

The rationale is read at every code review and is the receipt that the
skip is intentional, not a way to silence a flaky or broken test.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate, python_files, repo_relative

REMEDIATION = """Refactor to add a ``reason=`` kwarg to each skip/skipif/xfail
(or an adjacent ``#`` comment to each importorskip) — or delete the skip
entirely and fix the underlying issue — to pass.

Pass example:
  @pytest.mark.skip(reason="re-enabled once #214 lands")
  def test_xyz(): ...

  @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only path")
  def test_paths(): ...

  @pytest.mark.xfail(reason="known flake on macOS CI runners", strict=True)
  def test_flaky(): ...

  starlette = pytest.importorskip("starlette", reason="MCP transport is optional")
  # OR
  # starlette is an optional dependency — skip if missing
  starlette = pytest.importorskip("starlette")

Forbidden example:
  @pytest.mark.skip                                  # bare — no reason
  @pytest.mark.skipif(sys.platform == "win32")       # condition but no reason
  @pytest.mark.xfail                                 # bare xfail
  pytest.importorskip("starlette")                   # no comment / kwarg

A bare skip is invisible — the test looks present in the file but
silently never runs. Silent skips have caused real regressions (F7
transport coverage measured 0% because the unit test silently skipped
on missing starlette).

If the test is broken, fix it. If the dependency is mandatory, install
it. If the test is duplicated by integration coverage, delete it."""


def _is_pytest_mark(decorator: ast.expr, mark_name: str) -> ast.Call | ast.Attribute | None:
    """Return the matched node (Call or Attribute) for a pytest.mark.<mark_name>
    decorator, or None.
    """
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute) and target.attr == mark_name:
        inner = target.value
        if isinstance(inner, ast.Attribute) and inner.attr == "mark":
            return decorator
        if isinstance(inner, ast.Name) and inner.id == "mark":
            return decorator
    return None


def _has_reason_kwarg(call: ast.Call) -> bool:
    """True if call has a `reason=...` kwarg with non-empty string value."""
    for kw in call.keywords:
        if kw.arg == "reason":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str) and kw.value.value.strip():
                return True
    return False


def _decorator_violates_skip_rationale(decorator: ast.expr) -> bool:
    """True if decorator is a skip/skipif/xfail without rationale."""
    for mark_name in ("skip", "skipif", "xfail"):
        match = _is_pytest_mark(decorator, mark_name)
        if match is None:
            continue
        # Bare mark (e.g. `@pytest.mark.skip`) — Attribute, not Call
        if not isinstance(match, ast.Call):
            return True
        if not _has_reason_kwarg(match):
            return True
    return False


def _is_importorskip_call(node: ast.expr) -> ast.Call | None:
    """True if ``node`` is ``pytest.importorskip(...)``."""
    if not isinstance(node, ast.Call):
        return None
    target = node.func
    if isinstance(target, ast.Attribute) and target.attr == "importorskip":
        inner = target.value
        if isinstance(inner, ast.Name) and inner.id == "pytest":
            return node
    return None


def _importorskip_has_rationale(call: ast.Call, source_lines: list[str]) -> bool:
    """importorskip is OK if it has a `reason=` kwarg, a same-line trailing
    comment, OR an immediately-preceding ``# …`` comment block (within 3
    lines above with no blank gap separating them).

    The comment-block-above pattern is standard Python convention for
    documenting why a top-level statement is the way it is, so we accept
    it as rationale for test-skip mechanisms too.
    """
    if _has_reason_kwarg(call):
        return True
    # Same-line trailing comment (1-indexed lineno → 0-indexed list)
    line_idx = call.lineno - 1
    if 0 <= line_idx < len(source_lines):
        line = source_lines[line_idx]
        if "#" in line:
            # Strip the call's source slice; anything after the call's last
            # closing paren that starts with `#` counts as a trailing comment.
            after_hash = line.split("#", 1)[1].strip()
            if after_hash:
                return True
    # Comment block immediately above (no blank line gap), up to 3 lines
    for offset in range(1, 4):
        prev_idx = line_idx - offset
        if prev_idx < 0:
            return False
        stripped = source_lines[prev_idx].strip()
        if stripped == "":
            return False  # blank line breaks the block — call is undocumented
        if stripped.startswith("#"):
            return True
    return False


def file_has_violation(path: Path) -> bool:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    source_lines = source.splitlines()

    # Walk decorators on functions and classes
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for d in node.decorator_list:
                if _decorator_violates_skip_rationale(d):
                    return True

        # Walk module-level pytestmark assignments — they may use skip/skipif
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    val = node.value
                    candidates = [val] if not isinstance(val, (ast.List, ast.Tuple)) else list(val.elts)
                    for c in candidates:
                        if _decorator_violates_skip_rationale(c):
                            return True

        # Walk top-level expressions for `pytest.importorskip(...)` calls
        if isinstance(node, ast.Expr) and (call := _is_importorskip_call(node.value)):
            if not _importorskip_has_rationale(call, source_lines):
                return True
        # Also catch assigned form: `mod = pytest.importorskip("X")`
        if isinstance(node, ast.Assign) and (call := _is_importorskip_call(node.value)):
            if not _importorskip_has_rationale(call, source_lines):
                return True

    return False


def main() -> int:
    violations = {repo_relative(p) for p in python_files("tests") if file_has_violation(p)}
    return gate("test-skip-rationale", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
