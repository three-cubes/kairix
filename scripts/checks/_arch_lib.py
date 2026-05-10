"""Shared helpers for AST-based and XML-based architecture fitness checks.

Each check reports a list of offending file paths (relative to repo root)
and compares against ``.architecture/baseline/<name>-files.txt``. New
files (not in the baseline) trigger a non-zero exit; baseline files are
grandfathered with an informational message.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE_DIR = REPO_ROOT / ".architecture" / "baseline"

_RED = "\033[0;31m"
_GREEN = "\033[0;32m"
_YELLOW = "\033[0;33m"
_RESET = "\033[0m"


def gate(name: str, current: set[Path], remediation: str) -> int:
    """Compare current violations against baseline; print + return exit code.

    Args:
        name: short rule name (used in messages and baseline filename).
        current: set of repo-relative Paths with the violation.
        remediation: operator-actionable remediation hint.

    Returns:
        0 if no NEW violations (baseline matches or shrinks).
        1 if NEW violations introduced.
    """
    baseline_file = BASELINE_DIR / f"{name}-files.txt"
    if baseline_file.exists():
        baseline = {
            Path(line.strip())
            for line in baseline_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }
    else:
        baseline = set()

    current_rel = {p.relative_to(REPO_ROOT) if p.is_absolute() else p for p in current}
    new = sorted(current_rel - baseline)

    if new:
        print(f"{_RED}FAIL [arch:{name}]{_RESET} — new violation(s) introduced:")
        for p in new:
            print(f"  {p}")
        print()
        print(remediation)
        print()
        print(
            "If this is genuinely the only practical fix, document why in the\n"
            f"PR description and append the file to {baseline_file.relative_to(REPO_ROOT)}\n"
            "(but expect pushback at review time — adding to the baseline is rare)."
        )
        return 1

    remaining = len(baseline)
    if remaining > 0:
        print(
            f"{_YELLOW}ok [arch:{name}]{_RESET} — "
            f"{remaining} grandfathered file(s) still present in baseline."
        )
    else:
        print(f"{_GREEN}ok [arch:{name}]{_RESET} — clean.")
    return 0


def repo_relative(path: Path) -> Path:
    """Convert an absolute path under REPO_ROOT to a repo-relative Path."""
    return path.resolve().relative_to(REPO_ROOT)


def python_files(*roots: str) -> list[Path]:
    """Yield all .py files under the given relative roots, skipping __pycache__."""
    out: list[Path] = []
    for root in roots:
        root_path = REPO_ROOT / root
        if not root_path.exists():
            continue
        for p in root_path.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            out.append(p)
    return out


def main_entry(check_fn: object, name: str, remediation: str, *roots: str) -> int:
    """Convenience entry: scan the given roots, call ``check_fn(path)`` on each
    .py file, and gate on the union of returned violations.

    ``check_fn`` returns either ``True`` (file has a violation) or a falsy value.
    """
    violations: set[Path] = set()
    for path in python_files(*roots):
        if callable(check_fn) and check_fn(path):  # type: ignore[truthy-function]
            violations.add(repo_relative(path))
    return gate(name, violations, remediation)


if __name__ == "__main__":
    print("This module provides helpers for individual fitness-function checks.", file=sys.stderr)
    sys.exit(2)
