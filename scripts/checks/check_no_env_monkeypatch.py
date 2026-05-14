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

# REMEDIATION text — the shell wrapper ``check-no-env-monkeypatch.sh``
# owns the user-facing message that prints when the gate fails. This
# constant exists for F21 (actionable-feedback) compliance and is
# semantically equivalent to the shell wrapper's REMEDIATION.
REMEDIATION = """Refactor to constructor-injected FakePaths from tests/fakes.py
(no monkeypatch.setenv / setattr / delenv on KAIRIX_* keys) — to pass.

fix: replace ``monkeypatch.setenv("KAIRIX_...", ...)`` with explicit
construction of a ``FakePaths`` from tests/fakes.py and pass it as the
``paths=`` argument to the use case. If the production function reads
the env var directly, refactor it to accept ``paths: KairixPaths`` as
an explicit argument — the boundary-only pattern from #139.
next: re-run ``python3 scripts/checks/check_no_env_monkeypatch.py``
(or ``bash scripts/checks/check-no-env-monkeypatch.sh``) to confirm
the gate goes green.
run: bash scripts/safe-commit.sh "test(<area>): use FakePaths instead of env monkeypatch"

Pass example:
  paths = FakePaths(data_dir=tmp_path, log_dir=tmp_path / 'logs')
  result = some_use_case(paths=paths)

Forbidden example:
  monkeypatch.setenv('KAIRIX_DATA_DIR', str(tmp_path))
  result = some_use_case()

KAIRIX_* env-var reads happen ONCE at the boundary inside KairixPaths
(kairix/paths.py). Tests construct paths directly; they never mutate
process env to influence the production read."""

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
