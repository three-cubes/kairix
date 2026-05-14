"""F12: Every BDD feature must include at least one happy-path scenario.

The user-reported smell: "many of our BDD tests only document error
states." A feature whose scenarios are all `@error` / `@negative` /
`@failure` is not a specification of stakeholder value — it's an
error catalogue. Per Adzic (Specification by Example) and Wynne
(The Cucumber Book), a feature exists to document a *capability*;
the capability needs a positive scenario showing what success looks
like before we enumerate failure modes.

Rule: every ``*.feature`` file under ``tests/bdd/features/`` must
contain at least one scenario whose preceding tag line does NOT
include any of ``@error``, ``@negative``, ``@failure``, ``@unhappy``,
``@error-path``. Scenarios with no tag line at all count as
happy-path (untagged is the positive-flow default in our convention).

A feature with zero scenarios is also rejected — an empty feature is
either work-in-progress or accidentally truncated.

F12 does NOT (yet) catch:

  - Features missing for a given CLI subcommand (would require a
    public-surface enumeration step). See aspirational issue.
  - Three Amigos / abstraction-level review of scenarios.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import REPO_ROOT, gate, repo_relative

REMEDIATION = """Refactor to add at least one happy-path scenario per
feature file (a scenario WITHOUT any of the negative tags @error /
@negative / @failure / @unhappy / @error-path) to pass.

fix: add a happy-path Scenario to each listed *.feature file — one
positive scenario that demonstrates the capability's intended outcome,
not just failure modes.
next: re-run ``python3 scripts/checks/check_bdd_happy_path.py`` to
confirm the gate goes green.
run: bash scripts/safe-commit.sh "test(bdd): add happy-path scenario for <feature>"

Pass example:
  Feature: Benchmark suite execution
    Scenario: Operator runs a suite and sees scores
      Given a valid benchmark suite
      When the operator runs the benchmark
      Then the result shows category scores

    @error
    Scenario: Suite fails when config is invalid
      Given an invalid benchmark suite
      When the operator runs the benchmark
      Then an error is reported

Forbidden example:
  Feature: Benchmark suite execution
    @error
    Scenario: Suite fails when config is invalid
      ...
    @negative
    Scenario: Suite fails when target unreachable
      ...
  # — every scenario tagged negative → no happy-path → fails F12

A feature whose scenarios are all @error / @negative / @failure is an
error catalogue, not a specification of stakeholder value. Per Adzic
(Specification by Example), Wynne (The Cucumber Book), and Liz Keogh
on BDD test-infection — every feature exists to document a capability;
the capability needs a positive scenario showing what success looks like
before failure modes are enumerated."""


_NEGATIVE_TAGS = frozenset({"@error", "@negative", "@failure", "@unhappy", "@error-path"})

_SCENARIO_RE = re.compile(r"^\s*(Scenario|Scenario Outline):", re.IGNORECASE)
_TAG_LINE_RE = re.compile(r"^\s*@")
_BLANK_OR_COMMENT_RE = re.compile(r"^\s*(#.*)?$")


def _scenario_tags_at(lines: list[str], scenario_idx: int) -> set[str]:
    """Collect tags from the contiguous tag block immediately above the
    scenario at ``lines[scenario_idx]``. Stops at the first blank or
    non-tag line.
    """
    tags: set[str] = set()
    for offset in range(1, scenario_idx + 1):
        prev = lines[scenario_idx - offset]
        if _BLANK_OR_COMMENT_RE.match(prev):
            break
        if _TAG_LINE_RE.match(prev):
            for token in prev.strip().split():
                if token.startswith("@"):
                    tags.add(token.lower())
            continue
        break
    return tags


def file_has_violation(path: Path) -> bool:
    """A feature file fails F12 if it has zero scenarios, or if every
    scenario is tagged with one of the negative tags.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False

    lines = text.splitlines()
    scenario_indexes = [i for i, line in enumerate(lines) if _SCENARIO_RE.match(line)]
    if not scenario_indexes:
        return True

    has_happy_path = False
    for idx in scenario_indexes:
        tags = _scenario_tags_at(lines, idx)
        if not (tags & _NEGATIVE_TAGS):
            has_happy_path = True
            break
    return not has_happy_path


def main() -> int:
    features_dir = REPO_ROOT / "tests" / "bdd" / "features"
    if not features_dir.exists():
        return gate("bdd-happy-path", set(), REMEDIATION)
    violations = {repo_relative(p) for p in features_dir.rglob("*.feature") if file_has_violation(p)}
    return gate("bdd-happy-path", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
