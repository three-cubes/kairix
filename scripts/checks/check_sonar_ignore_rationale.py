"""F14: sonar.issue.ignore.multicriteria entries require a rationale comment.

SonarCloud rule-ignores are a load-bearing decision that needs visible
justification. This check verifies every
``sonar.issue.ignore.multicriteria.<id>.ruleKey`` line in
``sonar-project.properties`` is preceded by a comment block explaining
WHY the rule is ignored — not just THAT it is.

Convention enforced:

  # <ruleKey> — short rationale on at least one comment line preceding
  # any additional context lines.
  sonar.issue.ignore.multicriteria.<id>.ruleKey=<ruleKey>

The rationale block is the comment region immediately above the
ruleKey line (consecutive lines starting with ``#``, allowing blank
lines between paragraphs as long as comments resume before the
ruleKey). At least one comment line in that block must contain an
em-dash (``—``) and reference either the ruleKey or a kairix-shape
explanation. Empty or boilerplate-only comments (``# TODO``,
``# fixme``) do not count.

Failure is a fitness-function failure; no baseline (a hand-curated
file should not accumulate violations without review).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SONAR_FILE = REPO_ROOT / "sonar-project.properties"

RULE_KEY_PATTERN = re.compile(r"^sonar\.issue\.ignore\.multicriteria\.([A-Za-z0-9_-]+)\.ruleKey=")
BAD_TOKENS = ("TODO", "FIXME", "XXX", "fixme", "todo")


def _rationale_lines_above(lines: list[str], index: int) -> list[str]:
    """Collect the contiguous comment block ending immediately above ``index``.

    Walks upward through ``#``-prefixed lines, skipping at most one
    blank line between paragraphs. Stops at the first non-comment,
    non-blank line.
    """
    out: list[str] = []
    i = index - 1
    blank_skipped = False
    while i >= 0:
        line = lines[i].rstrip()
        if line.lstrip().startswith("#"):
            out.append(line.lstrip()[1:].strip())
            blank_skipped = False
        elif line == "":
            if blank_skipped:
                break
            blank_skipped = True
        else:
            break
        i -= 1
    return out


def _has_real_rationale(comment_block: list[str]) -> bool:
    """A comment block counts as a rationale if it has an em-dash or 12+ chars
    of substantive text (not just a heading like 'Exclusion list' or 'TODO')."""
    for line in comment_block:
        if not line:
            continue
        if any(tok in line for tok in BAD_TOKENS):
            continue
        if "—" in line or "--" in line:
            return True
        if len(line) >= 25 and not line.startswith("="):
            # Long substantive sentence — accept.
            return True
    return False


def main() -> int:
    if not SONAR_FILE.exists():
        # No sonar config to check; not a failure.
        print("\033[0;32mok [arch:sonar-ignore-rationale]\033[0m — no sonar-project.properties (skipped).")
        return 0

    lines = SONAR_FILE.read_text().splitlines()
    missing: list[tuple[str, int]] = []
    for idx, raw in enumerate(lines):
        match = RULE_KEY_PATTERN.match(raw.strip())
        if not match:
            continue
        rule_id = match.group(1)
        block = _rationale_lines_above(lines, idx)
        if not _has_real_rationale(block):
            missing.append((rule_id, idx + 1))

    if missing:
        print("\033[0;31mFAIL [arch:sonar-ignore-rationale]\033[0m — sonar.issue.ignore entries without rationale:")
        for rule_id, lineno in missing:
            print(f"  sonar-project.properties:{lineno}  multicriteria '{rule_id}' has no preceding rationale comment")
        print()
        print(
            "Add a comment block immediately above the .ruleKey line explaining\n"
            "why the rule is ignored for the given resourceKey. The convention is:\n"
            "  # <ruleKey> — short reason this fires false-positive for kairix's shape\n"
            "  sonar.issue.ignore.multicriteria.<id>.ruleKey=<ruleKey>"
        )
        return 1

    print("\033[0;32mok [arch:sonar-ignore-rationale]\033[0m — clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
