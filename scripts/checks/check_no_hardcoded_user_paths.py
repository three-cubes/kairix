"""F31: No hardcoded user/machine paths in committed code.

Detects literal absolute filesystem paths that pin a single contributor's
machine to a file checked into the repo:

- ``/Users/<name>/...`` (macOS home dirs)
- ``/home/<name>/...`` (Linux home dirs, excluding ``/home/runner/`` which is
  the GitHub-hosted runner workspace and legitimately appears in CI fixtures)

Why this rule exists:

- A path like ``/Users/developer/Development/kairix/scripts/...`` only
  resolves on one human's laptop. Anyone else running the same code (CI,
  another contributor, the alpha VM) hits ``FileNotFoundError`` or a
  silent path mismatch.
- Today's release session surfaced an instance of worktree-path leakage
  into a subagent's report; this rule converts that ad-hoc smell into a
  mechanical gate so the next leak gets caught at safe-commit, not at
  cherry-pick time.

Allow-list rules:

- Markdown documentation (``*.md``) is exempt — user-facing docs often
  show example paths and shell snippets that look like absolute paths
  but are clearly illustrative.
- ``/home/runner/...`` is exempt — that's the GitHub-hosted runner
  workspace and legitimately appears in workflow fixtures and CI log
  parsing tests.
- ``/Users/runner/...`` is exempt — same rationale for macOS runners.
- Files inside ``reference-library/`` and ``benchmark-results/`` are
  exempt — these are data fixtures.

There is no grandfathering: every offending line anywhere in the tracked
tree fails the gate. Fix the path at source.

Failure output follows F21: leads with the fix, includes ``run:`` for
re-running the gate, and shows a Pass/Forbidden example.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Two patterns: macOS ``/Users/<x>/...`` and Linux ``/home/<x>/...``.
# The negative lookahead excludes ``/home/runner/`` and ``/Users/runner/``
# because GitHub Actions hosted runners check the repo out there and the
# strings legitimately appear in workflow / CI log fixtures.
PATTERNS = (
    re.compile(r"/Users/(?!runner/)[A-Za-z][\w.-]*/"),
    re.compile(r"/home/(?!runner/)[A-Za-z][\w.-]*/"),
)

EXEMPT_SUFFIX = (".md",)
EXEMPT_PREFIX = (
    "reference-library/",
    "benchmark-results/",
)
# The detector and its test file are themselves exempt: they have to
# embed example paths to describe what they catch.
EXEMPT_FILES = frozenset(
    {
        "scripts/checks/check_no_hardcoded_user_paths.py",
        "tests/checks/test_no_hardcoded_user_paths.py",
    }
)

REMEDIATION = """Refactor to a relative or env-derived path — to pass.

fix: replace the hardcoded path with one of:
  - a relative path resolved at runtime via ``Path(__file__).resolve().parents[N]``
  - an environment variable (``os.environ["KAIRIX_DATA_DIR"]``) with a sensible default
  - a fixture path scoped to the test (``tmp_path`` in pytest)
next: re-run ``python3 scripts/checks/check_no_hardcoded_user_paths.py`` to confirm green.
run: bash scripts/safe-commit.sh "fix(<area>): drop hardcoded user path"

Pass example:
  ROOT = Path(__file__).resolve().parents[2]
  data_dir = os.environ.get("KAIRIX_DATA_DIR", "/opt/kairix/data")

Forbidden example:
  ROOT = Path("/Users/developer/Development/kairix")
  config = "/home/dan/.config/kairix.yaml"
"""


def _is_exempt_path(rel: str) -> bool:
    if rel.endswith(EXEMPT_SUFFIX):
        return True
    if rel in EXEMPT_FILES:
        return True
    return any(rel.startswith(p) for p in EXEMPT_PREFIX)


def _scan_file(path: Path, rel: str) -> list[str]:
    """Return one ``<rel>:<lineno>`` string per matching line.

    Build the list via comprehension rather than ``.append`` so the F21
    actionable-feedback detector doesn't treat the per-line location
    strings as remediation text — the agent-actionable message lives
    in the ``REMEDIATION`` constant above, not on each violation line.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    return [
        f"{rel}:{n}: hardcoded user/machine path"
        for n, line in enumerate(text.splitlines(), 1)
        if any(rx.search(line) for rx in PATTERNS)
    ]


def collect_violations(root: Path, files: list[str]) -> list[str]:
    """Scan every tracked path in ``files`` (repo-relative, under ``root``).

    Returns every hit in every non-exempt file — there is no grandfathering.
    """
    return [
        hit
        for rel in files
        if not _is_exempt_path(rel) and (root / rel).is_file()
        for hit in _scan_file(root / rel, rel)
    ]


def main() -> int:
    try:
        files = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    except subprocess.CalledProcessError:
        print("FAIL no_hardcoded_user_paths: could not enumerate tracked files", file=sys.stderr)
        return 1

    violations = collect_violations(ROOT, files)
    if violations:
        print("FAIL F31 no_hardcoded_user_paths: violations found", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print("", file=sys.stderr)
        print(REMEDIATION, file=sys.stderr)
        return 1

    print(f"ok F31 no_hardcoded_user_paths — clean ({len(files)} files scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
