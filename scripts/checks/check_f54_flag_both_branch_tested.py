"""F54: every flag in REGISTRY has both-branch test coverage.

Without F54, a flag's OFF branch silently rots after introduction
because nothing exercises it — rollback becomes a fiction. F54 makes
the rollback affordance mechanically real.

For each flag in ``kairix.core.features.registry.REGISTRY``:

  1. ``tests/bdd/features/feature_flag_<name>.feature`` exists.
  2. The feature file has ≥2 ``Scenario:`` / ``Scenario Outline:`` lines
     (the OFF + ON pair).
  3. ``tests/integration/test_feature_flag_<name>.py`` exists.
  4. The integration test exercises both branches (heuristic:
     ``with_flag("<name>", False)`` AND ``with_flag("<name>", True)``
     calls appear in the file — the canonical pattern from
     ``FakeFeatureFlagResolver``).
  5. If the flag's ``related_spec`` references a top-level capability
     spec (e.g. ``connector-ingestion-architecture.md``), then
     ``tests/e2e/test_composed_<name>_path.py`` exists.

Violations are emitted as
``tests/bdd/features/feature_flag_<name>.feature:flag=<name>:<reason>``
so each missing artefact appears as a distinct violation line.

Defensive: vacuous-green when ``kairix.core.features`` is not
importable (PR-2 may not be landed yet) or REGISTRY is empty.

Per F21, REMEDIATION carries ``fix:`` / ``next:`` / ``run:`` markers.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

BDD_FEATURES_DIR = REPO_ROOT / "tests" / "bdd" / "features"
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"
E2E_DIR = REPO_ROOT / "tests" / "e2e"

# Top-level-capability spec paths whose flags require an E2E composed-path test.
_TOP_LEVEL_SPECS = frozenset(
    {
        "docs/architecture/connector-ingestion-architecture.md",
        "docs/architecture/provider-plugin-architecture.md",
        "docs/architecture/fact-layer.md",
        "docs/architecture/cli-mcp-feature-parity.md",
    }
)

_SCENARIO_RE = re.compile(r"^\s*(Scenario|Scenario Outline):", re.IGNORECASE)

REMEDIATION = """F54: flag <name> is missing both-branch test coverage.
fix: add tests/bdd/features/feature_flag_<name>.feature with OFF + ON
     scenarios; add tests/integration/test_feature_flag_<name>.py
     exercising both branches via FakeFeatureFlagResolver from
     tests/fakes.py. For top-level-capability flags, also add
     tests/e2e/test_composed_<name>_path.py per F48.
next: see docs/architecture/feature-flag-architecture.md §5
      (both-branch test coverage).
run: python3 scripts/checks/check_f54_flag_both_branch_tested.py

Pass example:
  # tests/integration/test_feature_flag_hybrid_ranker_v2.py
  @pytest.mark.integration
  def test_off_branch_uses_v1():
      with with_flag("hybrid_ranker_v2", False):
          assert pipeline.ranker.__class__.__name__ == "HybridRankerV1"
  @pytest.mark.integration
  def test_on_branch_uses_v2():
      with with_flag("hybrid_ranker_v2", True):
          assert pipeline.ranker.__class__.__name__ == "HybridRankerV2"

Forbidden example:
  # tests/integration/test_feature_flag_hybrid_ranker_v2.py
  # has only with_flag("hybrid_ranker_v2", True) — the OFF / rollback
  # branch is never exercised. When the flag is flipped back during
  # an incident the legacy code path has rotted silently."""


def _count_scenarios(feature_path: Path) -> int:
    """Return the number of Scenario / Scenario Outline lines in a feature file."""
    try:
        text = feature_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return 0
    return sum(1 for line in text.splitlines() if _SCENARIO_RE.match(line))


def _integration_exercises_both_branches(integration_path: Path, flag_name: str) -> bool:
    """Return True when both branches appear via ``with_flag(<name>, ...)``.

    Heuristic: text-level scan for the canonical ``FakeFeatureFlagResolver``
    pattern. Matches both bare-name and module-aliased forms; the
    boolean argument can be ``False`` / ``True`` (case-insensitive
    tolerated for robustness against future refactors).
    """
    try:
        text = integration_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    # Tolerate single or double quotes; tolerate whitespace.
    off_re = re.compile(
        rf"with_flag\(\s*['\"]{re.escape(flag_name)}['\"]\s*,\s*False\b",
    )
    on_re = re.compile(
        rf"with_flag\(\s*['\"]{re.escape(flag_name)}['\"]\s*,\s*True\b",
    )
    return bool(off_re.search(text) and on_re.search(text))


def _is_top_level_capability_flag(entry: object) -> bool:
    """Return True when the flag's ``related_spec`` matches a top-level spec path."""
    related = getattr(entry, "related_spec", None)
    if not isinstance(related, str):
        return False
    # Normalise to posix-style relative path before matching.
    normalised = related.replace("\\", "/").lstrip("./")
    return normalised in _TOP_LEVEL_SPECS


def _violation_lines(name: str, entry: object) -> list[str]:
    """Return a list of violation strings for flag ``name``. Empty = clean."""
    out: list[str] = []
    feature_path = BDD_FEATURES_DIR / f"feature_flag_{name}.feature"
    integration_path = INTEGRATION_DIR / f"test_feature_flag_{name}.py"
    e2e_path = E2E_DIR / f"test_composed_{name}_path.py"

    feature_rel = feature_path.relative_to(REPO_ROOT).as_posix()
    integration_rel = integration_path.relative_to(REPO_ROOT).as_posix()
    e2e_rel = e2e_path.relative_to(REPO_ROOT).as_posix()

    if not feature_path.exists():
        out.append(f"{feature_rel}:flag={name}:missing-feature-file")
    elif _count_scenarios(feature_path) < 2:
        out.append(f"{feature_rel}:flag={name}:fewer-than-two-scenarios")

    if not integration_path.exists():
        out.append(f"{integration_rel}:flag={name}:missing-integration-test")
    elif not _integration_exercises_both_branches(integration_path, name):
        out.append(f"{integration_rel}:flag={name}:missing-both-branches")

    if _is_top_level_capability_flag(entry) and not e2e_path.exists():
        out.append(f"{e2e_rel}:flag={name}:missing-e2e-composed-path")

    return out


def _load_registry() -> dict[str, object] | None:
    """Defensively import REGISTRY. Returns None if module is absent.

    PR-2 may not be landed yet; the gate stays vacuous-green when the
    module is absent.
    """
    try:
        from kairix.core.features.registry import REGISTRY
    except ImportError:
        return None
    return dict(REGISTRY)


def find_violations(registry: dict[str, object]) -> list[str]:
    """Return sorted list of violation strings across all flags."""
    violations: list[str] = []
    for name, entry in registry.items():
        violations.extend(_violation_lines(name, entry))
    return sorted(violations)


def main() -> int:
    """Return 0 when clean / vacuous-green; 1 when net-new violations exist."""
    registry = _load_registry()
    if registry is None:
        print("ok [arch:f54-flag-both-branch-tested] — kairix.core.features absent; vacuous-green.")
        return 0
    if not registry:
        print("ok [arch:f54-flag-both-branch-tested] — registry empty; vacuous-green.")
        return 0

    violations = find_violations(registry)
    if not violations:
        print("ok [arch:f54-flag-both-branch-tested] — clean.")
        return 0

    print("FAIL [arch:f54-flag-both-branch-tested] — flag(s) missing both-branch coverage:")
    for v in violations:
        print(f"  {v}")
    print()
    print(REMEDIATION)
    return 1


if __name__ == "__main__":
    sys.exit(main())
