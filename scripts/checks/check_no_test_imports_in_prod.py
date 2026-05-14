"""F24: No imports of ``tests.*`` in kairix production code.

Production code under ``kairix/`` MUST NOT import from the ``tests``
package. ``tests/`` is not shipped in the published wheel — so any
``from tests.<x> import ...`` or ``import tests`` line will work in a
local checkout (where ``tests/`` is on ``sys.path`` via pytest's
``rootdir``) but blow up the moment an end user does
``pip install kairix`` and tries to run the code.

This rule was created in response to the v2026.5.15.1 → v2026.5.15.2
incident, where a production module imported ``FakeVectorRepository``
from ``tests.fakes`` to satisfy a default parameter. CI passed
(because tests run from the repo with ``tests/`` importable);
``pip install kairix==2026.5.15.1 && kairix-some-cli`` raised
``ModuleNotFoundError: No module named 'tests'`` on first boot.

Detection (AST):

  - ``ast.ImportFrom`` where ``module`` is ``"tests"`` or starts with
    ``"tests."``  →  flagged.
  - ``ast.Import`` where any ``alias.name`` is ``"tests"`` or starts
    with ``"tests."``  →  flagged.

Scope: every ``kairix/**/*.py`` file. Baseline at
``.architecture/baseline/no-test-imports-in-prod-files.txt`` (expected
to ship empty — the v2026.5.15.2 release cleaned out the only known
violation). Net-new violations block at pre-commit, in
``safe-commit.sh``, and in CI Stage 0.

The legitimate way to share a fake-like default is to put the
production-quality default in ``kairix/`` itself (e.g. an
``InMemoryVectorRepository`` shipped from ``kairix.core.fakes``-style
location, or a ``NullVectorRepository`` in the production package).
``tests/fakes.py`` is for tests only — by convention and by what the
wheel actually ships.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate, python_files, repo_relative

REMEDIATION = """Refactor the kairix module to stop importing from tests.* — to pass.

fix: move the symbol you needed out of tests/ and into kairix/ (the
shipped package). The common case is a production-quality default
implementation that was living in tests/fakes.py — re-home it under
kairix/ (for example as a ``NullX`` / ``InMemoryX`` in the relevant
domain package) so it's part of the wheel. If the import is for a
test seam, the production code shouldn't carry that seam at all —
inject via a constructor argument and let the test pass the fake
explicitly.
next: re-run ``python3 scripts/checks/check_no_test_imports_in_prod.py``
to confirm the gate goes green. Then ``pip install -e .`` and
``python -c "import kairix.<module>"`` to confirm the import works
from an installed-wheel posture (no tests/ on sys.path).
run: bash scripts/safe-commit.sh "fix(<area>): drop tests.* import from production module"

Pass example:
  # kairix/core/search/pipeline.py
  from kairix.core.vector.null import NullVectorRepository

  class SearchPipeline:
      def __init__(self, repo: VectorRepository | None = None) -> None:
          self._repo = repo or NullVectorRepository()

Forbidden example:
  # kairix/core/search/pipeline.py
  from tests.fakes import FakeVectorRepository        # tests/ not in wheel
  import tests                                        # ditto
  from tests import fakes                             # ditto

Why: ``tests/`` is excluded from the published wheel. Any production
import of ``tests.*`` works on the dev machine (pytest puts the repo
root on sys.path) but raises ``ModuleNotFoundError`` the moment an
end user does ``pip install kairix`` and tries to run. This rule
codifies the v2026.5.15.1 → v2026.5.15.2 incident — net-new
violations block at pre-commit, in safe-commit.sh, and in CI."""


def _name_is_tests(name: str | None) -> bool:
    """True if ``name`` is ``tests`` or any dotted child of ``tests``."""
    if name is None:
        return False
    return name == "tests" or name.startswith("tests.")


def file_has_violation(path: Path) -> bool:
    """True if ``path`` contains any ``from tests... import ...`` or
    ``import tests[...]`` statement.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _name_is_tests(node.module):
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _name_is_tests(alias.name):
                    return True
    return False


def main() -> int:
    violations = {repo_relative(p) for p in python_files("kairix") if file_has_violation(p)}
    return gate("no-test-imports-in-prod", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
