"""G1: every Go binary exposes ``--version``.

The operator-visible version flag mirrors kairix's own ``--version``
(populated from setuptools-scm). For Go binaries the flag is populated
via ``-ldflags "-X main.version=<tag>"`` at build time. Without the
``--version`` surface, operators can't tell which release a container
is running without poking around in `docker inspect`.

Detection: walk every ``services/*/cmd/*/main.go``. The file must
reference both:

  1. A ``version`` package-level variable declaration (the build-time
     stamp target — searched as ``var version =`` or
     ``const version =``).
  2. A ``--version`` or ``-version`` flag registration (any of
     ``flag.Bool("version"``, ``flags.Bool("version"``,
     ``--version`` literal, ``"-version"`` literal).

Either signal missing → flagged. A binary that has the ``version`` var
but no flag registered to print it isn't honouring G1; same for a flag
registered without a build-time stamp target.

Baseline: ``.architecture/baseline/go-version-flag-files.txt`` ships
empty. New Go services land at zero violations by design.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVICES_DIR = REPO_ROOT / "services"

# var version = "..."  OR  const version = "..."
_VERSION_VAR_RE = re.compile(r'^\s*(?:var|const)\s+version\s*=\s*"', re.MULTILINE)
# flag.Bool("version", ...) / flag.String("version", ...) / fs.Bool("version", ...)
_VERSION_FLAG_RE = re.compile(r'\.(?:Bool|String)\s*\(\s*"version"', re.MULTILINE)

REMEDIATION = """Every Go binary under services/<name>/cmd/<name>/main.go
must expose --version. Refactor to add both a package-level ``var
version = "dev"`` and a flag registration like ``flag.Bool("version",
false, "print version and exit")`` to pass.

fix: add the version variable + flag to the listed main.go. The
variable is overridden at build time by:
  go build -ldflags "-X main.version=$(git describe --tags)" ./cmd/...
The flag prints the variable and exits 0 — exactly like kairix's own
--version (populated via setuptools-scm).
next: re-run python3 scripts/checks/check_go_version_flag.py to
confirm the gate goes green.
run: bash scripts/checks/run-all.sh

Pass example (services/hello/cmd/hello/main.go):
  var version = "dev"
  ...
  showVersion := fs.Bool("version", false, "print version and exit (G1)")
  if *showVersion {
      fmt.Fprintf(stdout, "hello %s\\n", version)
      return 0
  }

Forbidden example:
  // main.go has no version variable and no --version flag — fails G1.
  // Operators can't tell which release this binary is from output.

Why: operator triage starts with "what version is running here?". Every
Go binary deployed via the kairix release pipeline gets its version
stamped at build time and must expose it. Net-new violations block.
"""


def _collect_main_files(services_root: Path) -> list[Path]:
    """Find every ``services/<name>/cmd/<name>/main.go``.

    Strictly scoped — only the canonical entrypoint pattern is checked.
    Helper packages inside ``services/<name>/internal/`` are out of scope
    for G1; they don't represent shippable binaries.
    """
    if not services_root.is_dir():
        return []
    return sorted(services_root.glob("*/cmd/*/main.go"))


def collect_violations(services_root: Path = SERVICES_DIR) -> set[Path]:
    """Return repo-relative paths of main.go files missing G1 contract."""
    violations: set[Path] = set()
    for main_file in _collect_main_files(services_root):
        text = main_file.read_text(encoding="utf-8")
        if not _VERSION_VAR_RE.search(text):
            violations.add(main_file.relative_to(REPO_ROOT))
            continue
        if not _VERSION_FLAG_RE.search(text):
            violations.add(main_file.relative_to(REPO_ROOT))
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("go-version-flag", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
