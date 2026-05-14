"""F23: Every top-level directory has a README.md.

Each top-level directory under the repo root is a resolver: a human
or agent who follows a path mention (``docs/...``, ``tests/...``,
``kairix/...``) lands somewhere and needs a one-screen orientation —
what belongs here, what doesn't, where the canonical docs live.

This fitness function enforces that resolver-README invariant. It
mirrors tc-agent-zone's ``repo_ia.py`` IA1 check (issue #258).

Detection: ``REPO_ROOT.iterdir()`` for directories; subtract the
allow-list; require ``<dir>/README.md`` to exist on each remaining
directory.

Allow-list (intentionally narrow):

  - ``.git``, ``.github``, ``.pytest_cache``, ``.ruff_cache``,
    ``.architecture``, ``.claude``, ``.idea``, ``.vscode``,
    ``.venv``, ``__pycache__``, ``htmlcov``, ``logs``,
    ``node_modules``, ``coverage``, ``dist``, ``build``
  - any directory whose name starts with ``.`` (dotfiles in general).

Everything else needs a ``README.md``. Pre-existing bare directories
are grandfathered in ``.architecture/baseline/readme-coverage-files.txt``
(one ``<dir>/README.md`` path per line — i.e. the file that *should*
exist) so the rule lands green; the baseline shrinks as READMEs get
written.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Directories that don't need a README. Mostly machine-readable
# caches, .git internals, build artefacts, and dotfile configuration
# trees (.architecture holds baseline lists, not agent-readable docs).
_README_EXEMPT: frozenset[str] = frozenset(
    {
        ".git",
        ".github",
        ".pytest_cache",
        ".ruff_cache",
        ".architecture",
        ".claude",
        ".idea",
        ".vscode",
        ".venv",
        "__pycache__",
        "htmlcov",
        "logs",
        "node_modules",
        "coverage",
        "dist",
        "build",
    }
)

REMEDIATION = """Add a README.md to the listed top-level directory.

fix: create <dir>/README.md with a short orientation — what this
directory holds, what doesn't belong here, and links to the canonical
docs (under docs/). Keep it under one screen.
next: re-run python3 scripts/checks/check_readme_coverage.py to
confirm the gate goes green.
run: bash scripts/checks/run-all.sh

Pass example (tests/README.md):
  # tests/
  Pytest test suites organised by category — unit, contract, bdd,
  integration. Fakes in `tests/fakes.py`; fixtures in `tests/fixtures/`.
  See docs/architecture/ENGINEERING.md for the test pyramid.

Forbidden example:
  tests/                # no README.md — fails F23

Why: every directory mention in CLAUDE.md, docs/, or an error message
becomes a click. Landing in a bare directory wastes the click; the
resolver-README pattern (every top-level dir has one) means every
path mention lands somewhere oriented. Net-new violations block;
pre-existing bare directories are grandfathered in
.architecture/baseline/readme-coverage-files.txt until written."""


def _is_exempt(name: str) -> bool:
    """True if a top-level directory is allow-listed (caches,
    .git internals, machine-readable trees, generic dotfiles)."""
    if name in _README_EXEMPT:
        return True
    return name.startswith(".")


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every top-level directory under ``repo_root``; return
    repo-relative paths of the *missing* README.md files (i.e. the
    file that should exist but doesn't).
    """
    violations: set[Path] = set()
    for child in sorted(repo_root.iterdir()):
        if not child.is_dir():
            continue
        if _is_exempt(child.name):
            continue
        readme = child / "README.md"
        if not readme.is_file():
            violations.add(Path(child.name) / "README.md")
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("readme-coverage", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
