"""G6: no ``panic(...)`` in non-``main`` packages.

``panic`` is reserved for unrecoverable startup failures in ``main`` /
``init()``. Library code in ``services/<name>/internal/`` and helper
packages must return errors instead — that's the kairix Go contract,
not a stylistic preference. A panicked library call kills the entire
process; an error-returning one lets the caller decide.

Detection: walk every ``services/*/**/*.go`` whose package is **not**
``main``. The package is read from the ``package <name>`` declaration
at the top of the file. Any line containing a top-level ``panic(``
call (not inside a comment) flags the file.

Test files (``*_test.go``) are exempt — ``t.Fatal`` and helper code
sometimes use panic for explicit test-shaping. The `package main` files
in ``cmd/<name>/`` are also exempt; that's where ``panic`` is allowed
for fatal startup conditions.

Baseline: ``.architecture/baseline/go-no-panic-outside-main-files.txt``
ships empty. New library code lands at zero violations.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVICES_DIR = REPO_ROOT / "services"

# package <name>  — captures the package declaration. First match wins.
_PACKAGE_RE = re.compile(r"^\s*package\s+(\w+)\s*$", re.MULTILINE)

# ``panic(`` not preceded by ``//`` or ``/*`` comment markers. Conservative:
# we deliberately don't try to parse multi-line block comments — a panic
# call disguised inside a /* ... */ block would slip through, but that's
# such an unusual code shape we accept the rare false-negative.
_PANIC_RE = re.compile(r"^\s*[^/]*\bpanic\s*\(", re.MULTILINE)


def _file_violates(go_file: Path) -> bool:
    """True if file is a library package (non-main) that calls ``panic(...)``."""
    text = go_file.read_text(encoding="utf-8")
    pkg_match = _PACKAGE_RE.search(text)
    if pkg_match is None:
        return False  # no package decl — not a Go source file we recognise
    if pkg_match.group(1) == "main":
        return False
    # Strip line-comments before scanning for panic.
    stripped_lines = []
    for line in text.splitlines():
        idx = line.find("//")
        if idx >= 0:
            line = line[:idx]
        stripped_lines.append(line)
    body = "\n".join(stripped_lines)
    return bool(_PANIC_RE.search(body))


REMEDIATION = """``panic(...)`` in a non-``main`` package can kill a
service across abstraction boundaries — a library caller has no
recourse. Refactor to return an error instead and let the caller
decide whether to escalate.

fix: replace the panic with ``return fmt.Errorf("...: %w", err)`` (or
a sentinel error). The error propagates through the call chain; the
``main`` package decides whether to log and exit, or recover and
continue.
next: re-run python3 scripts/checks/check_go_no_panic_outside_main.py
to confirm the gate goes green.
run: bash scripts/checks/run-all.sh

Pass example:
  // package internal/store
  func (s *Store) Open(path string) error {
      f, err := os.Open(path)
      if err != nil {
          return fmt.Errorf("open store %q: %w", path, err)
      }
      ...
  }

Forbidden example:
  // package internal/store
  func (s *Store) Open(path string) error {
      f, err := os.Open(path)
      if err != nil {
          panic(err)  // kills the process; caller can't recover
      }
      ...
  }

Exemptions: ``package main`` files in ``services/*/cmd/<name>/`` may
use ``panic`` for unrecoverable startup conditions. Test files
(``*_test.go``) are exempt — ``t.Fatal`` semantics. Net-new library
violations block.
"""


def collect_violations(services_root: Path = SERVICES_DIR) -> set[Path]:
    """Walk services/**/*.go (excluding tests + main); flag files that ``panic``."""
    violations: set[Path] = set()
    if not services_root.is_dir():
        return violations
    for go_file in services_root.rglob("*.go"):
        if go_file.name.endswith("_test.go"):
            continue
        try:
            if _file_violates(go_file):
                violations.add(go_file.relative_to(REPO_ROOT))
        except OSError:
            continue
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("go-no-panic-outside-main", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
