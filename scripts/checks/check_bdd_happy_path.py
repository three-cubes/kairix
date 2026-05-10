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
from _arch_lib import REPO_ROOT, gate, repo_relative  # noqa: E402

REMEDIATION = """Every feature file must declare at least one happy-path
scenario — a scenario that demonstrates what success looks like for
the capability the feature describes. A feature whose scenarios are
all @error / @negative / @failure is an error catalogue, not a
specification of stakeholder value.

Add a positive scenario:

  Feature: Benchmark suite execution
    Scenario: Operator runs a suite and sees scores
      Given a valid benchmark suite
      When the operator runs the benchmark
      Then the result shows category scores

References:
  - Gojko Adzic, "Specification by Example" — features describe
    capabilities, not exception cases.
  - Matt Wynne, "The Cucumber Book" — every feature has a
    must-work golden path.
  - Liz Keogh on BDD test-infection: scenarios that only test
    failures have lost the user-value framing."""


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
    violations = {
        repo_relative(p)
        for p in features_dir.rglob("*.feature")
        if file_has_violation(p)
    }
    return gate("bdd-happy-path", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
