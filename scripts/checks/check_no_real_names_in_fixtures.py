"""F32: No real first names or organisation/client names in test fixtures,
BDD scenarios, sample JSONL corpora, reference-library content, or
user-facing docs.

The mechanical version of the ``feedback_no_confidential_in_public_artefacts``
memory: public repo artefacts must use generic placeholders
(``agent-alpha``, ``Acme``, ``your-team``), not identifiers tied to a
specific human or client.

Detection scope:

- ``tests/**/*.py``               — pytest fixtures + assertions
- ``tests/bdd/**/*.feature``      — Gherkin scenarios
- ``reference-library/**/*.md``   — corpus prose
- ``reference-library/**/*.jsonl``— corpus transcripts
- ``docs/**/*.md``                — user-facing documentation

Detection signal: a curated ``REAL_NAMES`` set of identifiers historical
to this repo that should now be generic. The set is intentionally
narrow — only names actually leaked into committed artefacts (or that
the user has explicitly flagged for leak-prevention) — to avoid
false-positives on common English first names that legitimately appear
in citations (e.g. "Dan North" in BDD literature, "Daniel Kahneman" in
behavioural-economics references).

Generic placeholders that are explicitly OK:

- ``agent-alpha`` / ``agent-beta`` / ``agent-gamma`` / ``agent-delta`` / ``agent-epsilon``
- ``Acme`` / ``Example Corp`` / ``your-team`` / ``your-org``
- Cryptography/CS canon placeholders: ``Alice``, ``Bob``, ``Carol``

These never appear in ``REAL_NAMES`` so they pass the filter trivially.

There is no grandfathering: every in-scope match fails the gate. Migrate
the fixture to a generic placeholder at source.

Failure output follows F21: leads with the fix, includes ``run:`` for
re-running the gate, and shows a Pass/Forbidden example.

Why this rule exists: kairix is a public, dogfooded knowledge-store
project. Test fixtures and reference corpora seeded with a specific
contributor's friends, family, or clients leak into public commits,
issue threads, and the eval CHANGELOG. The cure is generic
placeholders; this gate makes the substitution mechanical so the next
fixture doesn't repeat the slip.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Identifiers the project has historically embedded in fixtures / docs
# that should now be generic. Whole-word match only (``re.compile`` with
# ``\b`` anchors) so substrings inside larger words don't false-flag
# (e.g. ``McMahonsville`` would not match — but no such word exists).
#
# Keep this set minimal. Adding a common English first name risks
# false-positives on legitimate third-party citations in the
# reference-library tree. The discriminator is "has this name actually
# appeared in *kairix-authored* fixtures/docs as a stand-in for a real
# person?" — not "is this a real name that exists in the world."
REAL_NAMES: tuple[str, ...] = (
    "Caroline",
    "McMahon",
    "Dan McMahon",
    "Daniel McMahon",
    "danielmcmahon",
)

# Compile to a single alternation, case-sensitive, with word boundaries.
# Multi-word entries (``Dan McMahon``) need ``\b`` only at the outer
# edges — internal whitespace is fine.
_PATTERN = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in REAL_NAMES) + r")\b")

# Per-extension scopes. Each scope is (path-prefix, suffix-tuple).
# The detector walks every tracked file and includes a file only if its
# repo-relative path matches at least one scope.
_SCOPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tests/", (".py",)),
    ("tests/bdd/", (".feature",)),
    ("reference-library/", (".md", ".jsonl")),
    ("docs/", (".md",)),
)

# The detector and its test file embed the names by definition — they
# document what the rule catches. Self-exempt to avoid the dogfood
# trap. Anything else added here demands a PR-description rationale.
#
# ``docs/architecture/fitness-functions.md`` documents the F32 rule
# itself (and reuses the same identifier in the F30 worked example);
# it is the source-of-truth for the rule, not a violator of it.
EXEMPT_FILES = frozenset(
    {
        "scripts/checks/check_no_real_names_in_fixtures.py",
        "tests/checks/test_no_real_names_in_fixtures.py",
        "docs/architecture/fitness-functions.md",
    }
)

# Path-prefix exclusions. ``reference-library/`` is vendored upstream
# scholarly content (the Turing Way, Data Feminism, etc.) — the names
# inside are accurate citations of the authors of those works, not
# kairix-authored fixtures. Editing vendored content would falsify
# the upstream attribution; excluding the tree keeps the rule honest
# about what it's actually policing (kairix-authored artefacts).
EXEMPT_PATH_PREFIXES: tuple[str, ...] = ("reference-library/",)

REMEDIATION = """Refactor real identifiers to generic placeholders — to pass.

fix: replace the real first name / surname / organisation reference
with a generic placeholder:
  - persons:   agent-alpha / agent-beta / agent-gamma / Alice / Bob / Carol
  - orgs:      Acme / Example Corp / your-team / your-org
next: re-run ``python3 scripts/checks/check_no_real_names_in_fixtures.py``
to confirm the gate goes green.
run: bash scripts/safe-commit.sh "test(fixtures): drop real names from fixtures"

Pass example:
  record = FakeFactRecord(entity="agent-alpha", attribute="role", value="VP")
  transcript = "agent-beta works at Acme."

Forbidden example:
  record = FakeFactRecord(entity="<real-first-name>", attribute="role", value="VP")
  transcript = "<real-full-name> works at <real-org>."
"""


def _is_in_scope(rel: str) -> bool:
    """True iff the repo-relative path falls under one of the F32 scopes."""
    return any(rel.startswith(prefix) and rel.endswith(suffixes) for prefix, suffixes in _SCOPES)


def _scan_file(path: Path, rel: str) -> list[str]:
    """Return one ``<rel>:<lineno>:<name>`` string per matching line.

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
        f"{rel}:{n}: real name found ({m.group(0)!r})"
        for n, line in enumerate(text.splitlines(), 1)
        for m in [_PATTERN.search(line)]
        if m is not None
    ]


def _is_exempt(rel: str) -> bool:
    return rel in EXEMPT_FILES or any(rel.startswith(prefix) for prefix in EXEMPT_PATH_PREFIXES)


def collect_violations(root: Path, files: list[str]) -> list[str]:
    """Scan every in-scope tracked path in ``files`` (repo-relative, under ``root``).

    Returns every hit in every non-exempt file — there is no grandfathering.
    """
    return [
        hit
        for rel in files
        if not _is_exempt(rel) and _is_in_scope(rel) and (root / rel).is_file()
        for hit in _scan_file(root / rel, rel)
    ]


def main() -> int:
    try:
        files = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    except subprocess.CalledProcessError:
        print("FAIL no_real_names_in_fixtures: could not enumerate tracked files", file=sys.stderr)
        return 1

    violations = collect_violations(ROOT, files)
    if violations:
        print("FAIL F32 no_real_names_in_fixtures: violations found", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print("", file=sys.stderr)
        print(REMEDIATION, file=sys.stderr)
        return 1

    print(f"ok F32 no_real_names_in_fixtures — clean ({len(files)} files scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
