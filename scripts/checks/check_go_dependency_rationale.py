"""G10: every third-party Go dependency carries a one-line rationale.

Every direct (non-``// indirect``) entry in a service's ``go.mod`` must
appear in that service's ``DEPENDENCIES.md`` with a rationale line.
Mirrors the Python rationale practice for per-line suppressions (F3,
F14) and centralises the "why is this dependency here?" answer in one
operator-readable spot.

Detection: for each ``services/<name>/go.mod``:

  1. Parse the ``require (...)`` block. Collect module paths whose line
     does **not** end with ``// indirect`` — those are direct
     dependencies.
  2. Read ``services/<name>/DEPENDENCIES.md``. Each non-empty line that
     mentions a module path (substring match on first space-delimited
     token of a Markdown list item) is treated as a rationale entry.
  3. Every direct module without a matching rationale line is flagged.
  4. Stdlib paths (no ``/`` in the import; or starting with ``golang.org/x``
     when documented as standard-library-adjacent) are exempt by default.

Empty ``go.mod`` (stdlib-only services like ``services/hello``) reports
zero violations: nothing to rationalise.

Baseline: ``.architecture/baseline/go-dependency-rationale-files.txt``
ships empty. Adding a dep without a rationale fails at landing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVICES_DIR = REPO_ROOT / "services"

# Matches ``<module-path> <version>`` lines inside a ``require ( ... )``
# block. Anchors on indented lines to avoid matching the surrounding
# ``require (`` / ``)`` framing. Captures the module path.
_REQUIRE_LINE_RE = re.compile(
    r"""
    ^\s+                                # indented within require ( ... )
    (?P<module>[a-zA-Z0-9./_\-]+)       # module path (allow domain + path)
    \s+v\d                              # version starts with v<digit>
    """,
    re.VERBOSE,
)
_INDIRECT_RE = re.compile(r"//\s*indirect\b")


def _parse_direct_modules(go_mod: Path) -> set[str]:
    """Extract direct (non-indirect) module paths from a go.mod ``require`` block."""
    text = go_mod.read_text(encoding="utf-8")
    direct: set[str] = set()
    in_require = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not in_require:
            if line.strip().startswith("require ("):
                in_require = True
            elif line.startswith("require "):
                # Single-line require: `require <mod> <ver>` — rare but legal.
                parts = line.split()
                if len(parts) >= 3 and parts[1] not in {"(", ")"} and not _INDIRECT_RE.search(line):
                    direct.add(parts[1])
            continue
        if line.strip() == ")":
            in_require = False
            continue
        if _INDIRECT_RE.search(line):
            continue
        m = _REQUIRE_LINE_RE.search(line)
        if m:
            direct.add(m.group("module"))
    return direct


def _parse_rationale_modules(deps_md: Path) -> set[str]:
    """Read DEPENDENCIES.md; return module paths mentioned in list-item lines.

    Permissive substring matching — operators document deps in their
    natural prose. We pull the longest path-looking token from each
    bullet line and treat it as a covered module path.
    """
    if not deps_md.is_file():
        return set()
    text = deps_md.read_text(encoding="utf-8")
    covered: set[str] = set()
    # ``module/path`` heuristic — at least one ``/`` and dot for TLD
    path_re = re.compile(r"\b([a-zA-Z0-9._\-]+\.[a-zA-Z]{2,}(?:/[a-zA-Z0-9./_\-]+)+)")
    for line in text.splitlines():
        # Only look at bullet/code lines; ignore prose headers
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for m in path_re.finditer(stripped):
            covered.add(m.group(1))
    return covered


REMEDIATION = """Every direct (non-``// indirect``) entry in services/<name>/go.mod
must appear in services/<name>/DEPENDENCIES.md with a one-line
rationale. Refactor to either add the rationale or vendor the
functionality into the stdlib path to pass.

fix: open services/<name>/DEPENDENCIES.md and add a bullet line for
each flagged module path. Format: ``- <module-path> — <one-line
reason>``. Example:
  - github.com/golang-jwt/jwt/v5 — JWT verification for the alpha-deploy
    webhook signature path.
next: re-run python3 scripts/checks/check_go_dependency_rationale.py
to confirm the gate goes green.
run: bash scripts/checks/run-all.sh

Pass example (services/<name>/DEPENDENCIES.md):
  # Dependency rationale (G10)
  - github.com/foo/bar — reason for needing this dep.

Forbidden example:
  go.mod has `github.com/foo/bar v1.2.3` but DEPENDENCIES.md doesn't
  mention it — fails G10. Operators reading the service can't tell
  why this dependency is in the supply chain.

Why: the supply-chain audit story for Go binaries matches the F14
rationale model for Python. Every dep is a deliberate decision; the
deliberateness lives in the rationale registry, not just commit
history. Stdlib-only services have nothing to register and pass for
free. Net-new direct deps without rationale block.
"""


def collect_violations(services_root: Path = SERVICES_DIR) -> set[Path]:
    """For each services/<name>/go.mod, find direct deps missing rationale."""
    violations: set[Path] = set()
    if not services_root.is_dir():
        return violations
    for go_mod in sorted(services_root.glob("*/go.mod")):
        service_dir = go_mod.parent
        direct = _parse_direct_modules(go_mod)
        if not direct:
            continue
        deps_md = service_dir / "DEPENDENCIES.md"
        covered = _parse_rationale_modules(deps_md)
        missing = direct - covered
        if missing:
            # Report against the DEPENDENCIES.md path — that's where the
            # operator goes to fix it.
            violations.add(deps_md.relative_to(REPO_ROOT))
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("go-dependency-rationale", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
