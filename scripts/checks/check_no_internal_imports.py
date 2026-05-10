"""F5: No internal-name imports in test files.

Tests must drive every branch through the public surface; ``_``-prefixed
helpers are implementation detail. AST-based detection so the rule
correctly distinguishes:

  REJECTED:
      from kairix.foo import _bar
      from kairix.foo import bar, _baz
      from kairix.foo._impl import x  # importing FROM a private module

  ALLOWED:
      from kairix.foo import bar as _alias    # test-local rename
      from kairix.foo import _Bar as Bar      # rename of private name
                                              # (same as above; the test
                                              # is renaming away from the
                                              # private form, not preserving it)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate, python_files, repo_relative  # noqa: E402

REMEDIATION = """Refactor: rewrite the test to drive the public function/class
that calls the private helper. If the public surface doesn't reach the
branch you wanted to pin, the branch is either dead code or the public
contract is missing — in either case, the answer is not to test the
private name directly."""


def _is_kairix_module(module: str | None) -> bool:
    return module is not None and (module == "kairix" or module.startswith("kairix."))


def _module_is_private(module: str) -> bool:
    """True if any segment of the module path starts with ``_`` (excluding
    the ``__init__`` and ``__main__`` patterns).
    """
    return any(segment.startswith("_") and not segment.startswith("__") for segment in module.split("."))


def file_has_violation(path: Path) -> bool:
    """Return True if ``path`` imports a private name from kairix.*.

    Each ``ImportFrom`` node is inspected:
      - source module is kairix.*
      - any name imported is ``_x`` AND was NOT renamed (no ``as`` clause)
        OR the source module path contains a private segment
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not _is_kairix_module(node.module):
            continue

        if _module_is_private(node.module or ""):
            return True

        for alias in node.names:
            name = alias.name
            if name.startswith("_") and not name.startswith("__"):
                # `as` rename means the test is choosing a local name;
                # the ORIGINAL name is private though, so this is still
                # a private-import violation.
                if alias.asname is None:
                    return True
                # Renamed: still importing private. Could allow this if we
                # want; for now flag it — the test is depending on a
                # private name's contract regardless of what the local
                # binding is called.
                return True

    return False


def main() -> int:
    violations = {repo_relative(p) for p in python_files("tests") if file_has_violation(p)}
    return gate("no-internal-test-imports", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
