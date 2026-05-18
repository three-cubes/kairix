"""F22: Path naming conventions per repo tree.

Kairix's CLAUDE.md "Naming" section pins the high-level rules:

  - Code: ``snake_case`` functions, ``PascalCase`` classes,
    ``UPPER_SNAKE_CASE`` constants
  - User-facing: grade 8 reading level, "knowledge store" not "vault"

This fitness function operationalises the *file path* counterpart of
those rules: each tracked file must live under a known tree and its
basename must satisfy that tree's naming regex. Convergence with a
sibling repo's ``path_naming.py`` (issue #258); kairix uses kairix's
own repo layout.

Trees enforced (first match wins; the order matters):

  ``kairix/``
      Importable Python package. ``__init__.py`` or ``snake_case.py``.
  ``tests/bdd/features/``
      Gherkin features. ``snake_case.feature``.
  ``tests/bdd/steps/``
      Step modules. ``snake_case.py`` or ``__init__.py``.
  ``tests/`` (excluding ``tests/bdd/``)
      Pytest test modules. ``test_<thing>.py``, ``conftest.py``,
      ``fakes.py``, ``__init__.py``, or snake_case helper modules.
  ``scripts/checks/``
      Fitness-function check scripts. ``check_<rule>.py``,
      ``check-<rule>.sh``, ``_arch_lib.py``, ``_lib.sh``,
      ``run-all.sh``, ``audit_baselines.py``, ``merge_coverage_xml.py``.
  ``docs/operations/runbooks/`` and ``docs/runbooks/``
      Runbook docs. ``<topic>-<scenario>.md`` (kebab-case) or
      ``INDEX.md``.
  ``.architecture/baseline/``
      Baseline ratchets. ``<rule-name>-files.txt``.

Files outside every tree-rule (top-level config, ``.github/``, etc.)
are not constrained by F22.

The detector walks ``git ls-files`` (tracked files only — generated
artefacts and the worktree's ignored cruft are out of scope) and for
each path picks the FIRST tree-rule whose prefix matches; if that
rule's regex rejects the basename, the path is flagged.

Baseline at ``.architecture/baseline/path-naming-files.txt`` lists
current offenders so the gate lands green; the baseline is expected
to shrink as files get renamed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Per-tree regex rules. The first matching prefix wins, so order
# matters: more specific prefixes (``tests/bdd/features/``) must
# precede broader ones (``tests/``).
_SNAKE_PY = re.compile(r"^(__init__|conftest|fakes|_?[a-z][a-z0-9_]*)\.py$")
_TEST_PY = re.compile(r"^(__init__|conftest|fakes|test_[a-z0-9_]+|_?[a-z][a-z0-9_]*)\.py$")
_SNAKE_FEATURE = re.compile(r"^[a-z][a-z0-9_]*\.feature$")
_CHECK_SCRIPT_PY = re.compile(r"^(check_[a-z0-9_]+|_arch_lib|audit_baselines|merge_coverage_xml)\.py$")
_CHECK_SCRIPT_SH = re.compile(r"^(check[-_][a-z0-9-]+|_lib|run-all)\.sh$")
_RUNBOOK_MD = re.compile(r"^(INDEX|README|[a-z][a-z0-9-]*)\.md$")
_BASELINE_TXT = re.compile(r"^[a-z][a-z0-9-]*-files\.txt$")

_TREE_RULES: tuple[tuple[str, re.Pattern[str], tuple[re.Pattern[str], ...]], ...] = (
    # Importable Python package — snake_case .py files only.
    ("kairix/", re.compile(r"\.py$"), (_SNAKE_PY,)),
    # BDD features — snake_case .feature files.
    ("tests/bdd/features/", re.compile(r"\.feature$"), (_SNAKE_FEATURE,)),
    # BDD step modules — snake_case .py.
    ("tests/bdd/steps/", re.compile(r"\.py$"), (_SNAKE_PY,)),
    # Pytest tests + helpers — test_<thing>.py or snake_case helpers.
    ("tests/", re.compile(r"\.py$"), (_TEST_PY,)),
    # Fitness-function check scripts — check_<rule>.py / check-<rule>.sh.
    ("scripts/checks/", re.compile(r"\.py$"), (_CHECK_SCRIPT_PY,)),
    ("scripts/checks/", re.compile(r"\.sh$"), (_CHECK_SCRIPT_SH,)),
    # Runbooks — kebab-case markdown (or INDEX).
    ("docs/operations/runbooks/", re.compile(r"\.md$"), (_RUNBOOK_MD,)),
    ("docs/runbooks/", re.compile(r"\.md$"), (_RUNBOOK_MD,)),
    # Architecture baseline lists — <rule-name>-files.txt.
    (".architecture/baseline/", re.compile(r"\.txt$"), (_BASELINE_TXT,)),
)

REMEDIATION = """Refactor the file path to satisfy its tree's naming convention.

fix: rename the file so its basename matches the regex for its tree
(see scripts/checks/check_path_naming.py for the rule table). The
common cases are:
  - kairix/**/*.py             → snake_case.py
  - tests/**/test_*.py         → test_<thing>.py
  - tests/bdd/features/*.feature → snake_case.feature
  - scripts/checks/check_*.py  → check_<rule>.py
  - docs/**/runbooks/*.md      → kebab-case.md
  - .architecture/baseline/*.txt → <rule-name>-files.txt
next: re-run python3 scripts/checks/check_path_naming.py to confirm
the gate goes green.
run: bash scripts/checks/run-all.sh

Pass example:
  kairix/core/search/pipeline.py             # snake_case importable module
  tests/search/test_pipeline.py              # test_<thing>.py
  tests/bdd/features/search_returns_hits.feature
  scripts/checks/check_path_naming.py
  docs/operations/runbooks/how-to-debug-search-ranking.md

Forbidden example:
  kairix/core/Search-Pipeline.py             # PascalCase + dashes — fails
  tests/search/PipelineTest.py               # not test_<thing>.py
  tests/bdd/features/SearchReturnsHits.feature  # not snake_case
  scripts/checks/CheckPathNaming.py          # not check_<rule>.py

Why: agents and humans cross-reference paths constantly (in CLAUDE.md,
runbooks, error messages). A consistent shape per tree means a path
mentioned in one place is greppable everywhere. Net-new violations
block; pre-existing violators are grandfathered in
.architecture/baseline/path-naming-files.txt until renamed."""


def _git_ls_files(repo_root: Path = REPO_ROOT) -> list[str]:
    """Return every tracked file (repo-relative POSIX paths)."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _matching_rule(
    path: str,
) -> tuple[re.Pattern[str], ...] | None:
    """Return the basename-regex tuple for the FIRST tree rule whose
    prefix + suffix-trigger match this path, or ``None`` if no rule
    applies (out-of-scope file).
    """
    for prefix, suffix_trigger, basename_regexes in _TREE_RULES:
        if path.startswith(prefix) and suffix_trigger.search(path):
            return basename_regexes
    return None


def file_violates(path: str) -> bool:
    """True if a tracked path falls under a tree rule whose basename
    regex rejects its name. Files outside every tree rule pass
    silently (out-of-scope, not a violation).
    """
    rules = _matching_rule(path)
    if rules is None:
        return False
    basename = Path(path).name
    return not any(regex.fullmatch(basename) for regex in rules)


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every tracked file; return repo-relative paths whose
    basename fails the tree-rule regex.
    """
    violations: set[Path] = set()
    for tracked in _git_ls_files(repo_root):
        if file_violates(tracked):
            violations.add(Path(tracked))
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("path-naming", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
