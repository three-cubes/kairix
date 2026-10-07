"""F92: the fitness catalogue is current — checks, entries, and docs agree.

Motivation (EPIC #499 Phase 2 — the catalogue-driven runner)
------------------------------------------------------------
Phase 2 makes ``scripts/checks/_rule_catalogue.py`` the single source of
truth: ``run-all.sh``, pre-commit, and the docs all DERIVE from it. That
only holds if the catalogue cannot silently fall out of sync with the
check scripts on disk or the generated doc regions. F92 is the
self-hosting guard — a rule ABOUT the catalogue, registered IN the
catalogue — that fails the build when any of three invariants break:

  (a) **No orphan check scripts.** Every ``scripts/checks/check_*.py``
      and every ``scripts/checks/check-*.sh`` either has a RuleEntry, or
      is a thin shell wrapper that delegates to a cataloged python check,
      or is named by another cataloged check's source (the
      fresh-install smoke that F81 guards). An orphan check runs in some
      pipeline but nothing documents what it protects.
  (b) **No dangling entries.** Every non-``proposed`` RuleEntry resolves
      to a check script that exists on disk (its ``script`` override, or
      the default ``check_<check>.py``).
  (c) **No doc drift.** The generated regions in
      ``docs/architecture/fitness-functions.md`` and ``CLAUDE.md`` match
      what ``generate_catalogue_docs.py`` would emit from the catalogue.

Invariant (b) overlaps with ``tests/checks/test_rule_catalogue.py`` by
design — the test is a unit-suite proof, F92 is the pre-commit / Stage 0
gate that fires on every commit, including doc and shell-script edits the
unit suite might not be re-run for.

A currency invariant is binary: either the catalogue is current or it
is not.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import generate_catalogue_docs
from _rule_catalogue import ALL_ENTRIES

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHECKS_DIR = REPO_ROOT / "scripts" / "checks"

_GREEN = "\033[0;32m"
_RED = "\033[0;31m"
_RESET = "\033[0m"

# Shell wrappers that delegate to a cataloged python check are covered
# transitively; this captures the `check_<x>.py` names they invoke.
_PY_DELEGATE_RE = re.compile(r"check_[a-z0-9_]+\.py")

REMEDIATION = """F92: the fitness catalogue is out of sync with reality. The catalogue
(scripts/checks/_rule_catalogue.py) is the single source of truth — when
a check script, a RuleEntry, or a generated doc region drifts from it,
the catalogue-driven runner silently mis-dispatches and the docs lie.

fix: per the failing invariant printed above —
  (a) orphan check script: add a RuleEntry for it in
      scripts/checks/_rule_catalogue.py (id, gate, check, category,
      scope, summary), OR — if it is a thin shell wrapper — make it
      delegate to an already-cataloged check_<x>.py.
  (b) dangling entry: land the check script the entry names
      (check_<check>.py, or the file in its `script` field), or mark the
      entry status='proposed' with check='(proposed)'.
  (c) doc drift: run `python3 scripts/checks/generate_catalogue_docs.py`
      to regenerate the F-CATALOGUE regions, then stage the docs.
next: re-run `python3 scripts/checks/check_catalogue_currency.py` to
confirm the gate goes green.
run: bash scripts/safe-commit.sh "chore(fitness): keep the catalogue current (#499 phase 2)"

Pass example: a new rule F86 added as ONE RuleEntry row + its
check_f86_<name>.py script, then
`generate_catalogue_docs.py` run to refresh the doc regions — every
invariant holds, F92 stays green.

Forbidden example: dropping scripts/checks/check_f86_<name>.py into the
tree with no RuleEntry (it runs in pre-commit but nothing says what it
protects), or editing a generated <!-- F-CATALOGUE --> region by hand so
it no longer matches the catalogue."""


def _cataloged_py_checks() -> set[str]:
    """``{check_<check>.py}`` for every non-proposed, non-CORE RuleEntry.

    A ``core:<module>`` row resolves to an engine CORE module
    (``tc_fitness.core_checks.<module>``), not a local ``check_*.py`` file —
    so it names no local script and is excluded here."""
    return {f"check_{e.check}.py" for e in ALL_ENTRIES if e.check != "(proposed)" and not e.check.startswith("core:")}


def _cataloged_scripts() -> set[str]:
    """Every script named by a RuleEntry — the default ``check_<check>.py``
    AND any explicit ``script`` override."""
    out = _cataloged_py_checks()
    out.update(e.script for e in ALL_ENTRIES if e.script)
    return out


def _smoke_scripts_referenced() -> set[str]:
    """``check-*.sh`` filenames referenced by a cataloged check's SOURCE —
    e.g. the fresh-install smoke that ``check_f81_fresh_install_smoke.py``
    guards by path. These are covered transitively (the cataloged check
    proves they stay wired)."""
    referenced: set[str] = set()
    cataloged_py = _cataloged_py_checks()
    for name in cataloged_py:
        path = CHECKS_DIR / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        referenced.update(re.findall(r"check-[a-z0-9-]+\.sh", text))
    return referenced


def _orphan_scripts() -> list[str]:
    """Check scripts on disk with no catalogue coverage (invariant a)."""
    cataloged = _cataloged_scripts()
    smoke_refs = _smoke_scripts_referenced()
    orphans: list[str] = []
    for path in sorted(CHECKS_DIR.glob("check_*.py")) + sorted(CHECKS_DIR.glob("check-*.sh")):
        name = path.name
        if name in cataloged:
            continue
        if name.endswith(".sh"):
            # Shell wrapper delegating to a cataloged python check → covered.
            delegates = set(_PY_DELEGATE_RE.findall(path.read_text(encoding="utf-8")))
            if delegates & cataloged:
                continue
            # Smoke / sidecar script referenced by a cataloged check → covered.
            if name in smoke_refs:
                continue
        orphans.append(name)
    return orphans


def _dangling_entries() -> list[tuple[str, str]]:
    """RuleEntries naming a script that does not exist (invariant b).

    Returns ``[(entry_id, missing_script), ...]``."""
    missing: list[tuple[str, str]] = []
    for entry in ALL_ENTRIES:
        if entry.check == "(proposed)" or entry.status == "proposed":
            continue
        # A core:<module> row resolves to an engine CORE module, never a local
        # check_<check>.py — the engine guarantees the module exists (the
        # catalogue-consistency test pins core: resolution separately).
        if entry.check.startswith("core:"):
            continue
        script = entry.script if entry.script else f"check_{entry.check}.py"
        if not (CHECKS_DIR / script).exists():
            missing.append((entry.id, script))
    return missing


def _doc_drift() -> int:
    """0 iff the generated doc regions match the catalogue (invariant c)."""
    return generate_catalogue_docs.check()


def main() -> int:
    """Run all three invariants; print failures; return 0 iff all hold."""
    failed_invariants: list[str] = []

    orphans = _orphan_scripts()
    if orphans:
        failed_invariants.append("orphan check scripts (invariant a)")
        print(f"{_RED}FAIL [arch:f92]{_RESET} — check scripts with no catalogue entry:")
        for name in orphans:
            print(f"  scripts/checks/{name}")

    dangling = _dangling_entries()
    if dangling:
        failed_invariants.append("dangling catalogue entries (invariant b)")
        print(f"{_RED}FAIL [arch:f92]{_RESET} — catalogue entries naming a missing check:")
        for entry_id, script in dangling:
            print(f"  {entry_id} -> scripts/checks/{script}")

    if _doc_drift() != 0:
        # generate_catalogue_docs.check() already printed which files drift.
        failed_invariants.append("generated doc regions drift from the catalogue (invariant c)")

    if failed_invariants:
        print()
        print(REMEDIATION)
        return 1

    print(f"{_GREEN}ok [arch:f92]{_RESET} — catalogue current: checks, entries, and docs agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
