"""Contract tests for the fitness function catalogue.

Bidirectional consistency:

1. **Every entry references a real check.** Each
   :class:`RuleEntry.check` must resolve to a real
   ``scripts/checks/check_<name>.py`` file — or be the literal
   sentinel ``"(proposed)"`` for entries marked ``status="proposed"``.
2. **Every check has at least one entry.** Every
   ``scripts/checks/check_*.py`` file must be referenced by at least
   one entry — no orphan checks.
3. **Vocabularies are closed.** Categories and statuses come from the
   well-known sets. (There is no per-file baseline to reference — every
   rule evaluates the full tree, PLA-472.)

The catalogue is the canonical source of truth; CLAUDE.md and
future tooling consume it. These tests prove the catalogue tracks
reality.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts" / "checks"))
from _rule_catalogue import (
    ALL_ENTRIES,
    CATALOGUE,
    Category,
    Status,
    by_category,
    by_status,
    categories_in_use,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_CHECKS = REPO_ROOT / "scripts" / "checks"


def _existing_check_basenames() -> set[str]:
    """Return ``{<basename>}`` for every ``scripts/checks/check_*.py``."""
    return {p.stem.removeprefix("check_") for p in SCRIPTS_CHECKS.glob("check_*.py")}


def test_every_entry_references_a_real_check_or_is_proposed() -> None:
    """A catalogue entry whose ``check`` field is not ``"(proposed)"``
    must resolve to EITHER a local ``scripts/checks/check_<check>.py``
    file OR an engine CORE module via the ``core:<module>`` namespace
    (``check="core:no_duplicate_string"`` →
    ``tc_fitness.core_checks.no_duplicate_string``). Entries marked
    ``status="proposed"`` are exempt — by definition the check doesn't
    exist yet.
    """
    from tc_fitness.catalogue import is_dispatchable  # noqa: F401  # import guard for engine availability
    from tc_fitness.core_checks import CORE_CHECKS

    existing = _existing_check_basenames()
    # CORE module names exposed by the engine (the part after ``core:``).
    core_modules = {key.split(":", 1)[1] for key in CORE_CHECKS}
    missing: list[tuple[str, str]] = []
    for entry in ALL_ENTRIES:
        if entry.status == "proposed":
            continue
        if entry.check == "(proposed)":
            continue
        if entry.check.startswith("core:"):
            module = entry.check.split(":", 1)[1]
            if module not in core_modules:
                missing.append((entry.id, entry.check))
            continue
        if entry.check not in existing:
            missing.append((entry.id, entry.check))
    assert not missing, (
        f"catalogue entries reference non-existent checks: {missing!r}. "
        f"Either land the check script, bind an engine CORE module via check='core:<module>', "
        f"or mark the entry status='proposed' with check='(proposed)'."
    )


def test_every_check_has_at_least_one_catalogue_entry() -> None:
    """Every ``scripts/checks/check_*.py`` file must be claimed by at
    least one catalogue entry. Orphans rot — they exist, they run in
    pre-commit, but nothing documents what they protect or how they
    relate to other rules.
    """
    referenced = {entry.check for entry in ALL_ENTRIES if entry.check != "(proposed)"}
    orphans = _existing_check_basenames() - referenced
    assert not orphans, (
        f"check scripts have no catalogue entry: {sorted(orphans)!r}. "
        f"Add an entry to scripts/checks/_rule_catalogue.py describing what each rule protects."
    )


def test_proposed_entries_use_sentinel_check_value() -> None:
    """Catalogue hygiene: ``status='proposed'`` entries must declare
    ``check='(proposed)'``. A proposed entry pointing at a real check
    is contradictory — either the check exists (status='shipped') or
    it doesn't (check='(proposed)').
    """
    for entry in by_status("proposed"):
        assert entry.check == "(proposed)", (
            f"entry {entry.id} has status='proposed' but check={entry.check!r}; "
            f"set check='(proposed)' or change status to 'shipped'/'vacuous'/'proxy'."
        )


def test_catalogue_keyed_by_gate_no_collisions_drop_silently() -> None:
    """The ``CATALOGUE`` dict is keyed by gate name. When two entries
    share a gate (legitimate — F12 + F13 both surface through
    ``bdd-no-implementation-leaks``), one shadows the other in the
    dict. The :data:`ALL_ENTRIES` tuple is the lossless source.

    This test pins the invariant that ``len(CATALOGUE) <=
    len(ALL_ENTRIES)`` — the dict can compress but never grow.
    """
    assert len(CATALOGUE) <= len(ALL_ENTRIES), (
        f"CATALOGUE has more keys ({len(CATALOGUE)}) than ALL_ENTRIES ({len(ALL_ENTRIES)}) — impossible by construction"
    )


def test_categories_are_well_known() -> None:
    """Every entry's category must come from the Literal type. Python
    doesn't enforce Literal at runtime — this test does. Catches
    typos (\"layring\" → \"layering\") before they fragment the
    taxonomy.
    """
    well_known: set[Category] = {
        "layering",
        "test-discipline",
        "plugin-contract",
        "production-safety",
        "schema-integrity",
        "feature-flag",
        "agent-affordance",
        "repo-hygiene",
        "observability",
        "go-discipline",
        "coverage",
        "process",
    }
    bad = [(entry.id, entry.category) for entry in ALL_ENTRIES if entry.category not in well_known]
    assert not bad, (
        f"entries reference unknown categories: {bad!r}. "
        f"Either fix the typo or extend the Category Literal in scripts/checks/_rule_catalogue.py."
    )


def test_statuses_are_well_known() -> None:
    """Same Literal-runtime check for status."""
    well_known: set[Status] = {"shipped", "vacuous", "proxy", "proposed", "superseded"}
    bad = [(entry.id, entry.status) for entry in ALL_ENTRIES if entry.status not in well_known]
    assert not bad, f"entries reference unknown statuses: {bad!r}."


def test_by_category_returns_matching_entries() -> None:
    """:func:`by_category` filters correctly."""
    layering = by_category("layering")
    assert all(entry.category == "layering" for entry in layering)
    assert any(entry.id == "F26" for entry in layering), "F26 must be in layering"


def test_by_status_returns_matching_entries() -> None:
    """:func:`by_status` filters correctly."""
    proposed = by_status("proposed")
    assert all(entry.status == "proposed" for entry in proposed)
    proposed_ids = {entry.id for entry in proposed}
    assert {"F78", "F79", "F80"}.issubset(proposed_ids), "ADR-026 blindspot trio must be marked proposed"


def test_categories_in_use_returns_unique_categories_in_declaration_order() -> None:
    """:func:`categories_in_use` deduplicates while preserving the
    first-occurrence order. Drives stable CLAUDE.md section ordering.
    """
    seen = categories_in_use()
    assert len(seen) == len(set(seen)), "categories_in_use must deduplicate"
    # The first entry in the catalogue is F26 (layering); layering must
    # therefore be the first category surfaced.
    assert seen[0] == "layering", f"first category should be 'layering' (F26's category, first entry); got {seen[0]!r}"
