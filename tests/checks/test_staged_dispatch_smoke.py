"""Whole-gate staged-dispatch smokes (integration tier).

These are the only tests that drive the real ``run_checks._dispatch_staged``
end to end, paying the full ~40-rule staged gate (~11s locally, much more on a
loaded runner). They lived in ``test_staged_selection.py`` under the unit
marker, where the whole-gate cost raced the 60s per-test timeout on loaded
Stage 2 shards (the recurring ``test_file_local_f26_forbidden_import_caught``
flake). A whole-gate dispatch is an integration of every rule, so it runs in
CI Stage 3 (``-m integration``); the fast single-rule and selection proofs
stay in the unit tier.
"""

from __future__ import annotations

import pytest

from tests.fixtures.staged_probe import (
    failed_rule_ids,
    probe_file,
    ran_rule_ids,
    run_one_narrowed,
    run_staged,
    sweep_staged_probes,  # noqa: F401 — session-scoped autouse fixture
)

# Whole-gate cost is inherent (every staged rule runs) and scales with host load:
# ~11s idle, 36-58s measured at load average ~35. The default 60s per-test
# timeout is a ceiling for unit-sized work, not a full gate dispatch, so these
# two smokes carry their own budget instead of racing it.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


def test_file_local_f26_forbidden_import_caught() -> None:
    """A staged kairix/core file importing kairix.providers → staged mode runs
    and FAILS F26 (file-local class).

    This is the ONE retained end-to-end full ``_dispatch_staged`` smoke — it
    proves the real staged-dispatch wiring (selection → narrowing → in-process
    ledger) is intact end-to-end on the per-commit path, AND it carries the
    cluster's only clean-arm control through the full gate (the sabotage arm
    below stages a clean probe and asserts F26 clears). The other file-local
    proofs (F8) use the cheaper single-rule :func:`run_one_narrowed`; only this
    one pays the whole-gate cost, by design, and only ONCE (the clean arm uses
    the narrowed single-rule path).

    Robustness (#506): the assertions are scoped to **F26's own verdict in the
    ledger**, never the aggregate exit code. ``run_staged`` drives the FULL
    ~40-rule staged gate, several of whose rules (F94 system-path writes,
    F92 catalogue-currency, F22 path-naming, the token scanners) read
    whole-tree / git state — so a stray probe or ``__pycache__`` another test
    left in the tree could flip the aggregate ``exit_code`` to 1 even when this
    probe's own change is clean, failing a ``code2 == 0`` assertion for reasons
    that have nothing to do with F26 (the flake this test hit 3x on a pin bump).
    Asserting F26 specifically RAN and FAILED (violation arm) / RAN and did NOT
    fail (clean arm) keeps the full e2e dispatch as the subject while making the
    verdict immune to unrelated whole-tree rule state. The
    ``sweep_staged_probes`` session fixture (tests/fixtures/staged_probe.py) scrubs the most common
    debris; this scoping is the structural complement that removes the
    dependency on a globally-clean tree entirely."""
    with probe_file(
        "kairix/core/zzz_staged_probe_f26.py",
        "from kairix.providers import something  # forbidden core→providers import\n",
    ) as rel:
        _code, out = run_staged([rel])
    # Violation arm: F26 must have RUN (dispatch wiring intact) and FAILED on
    # the forbidden import. Scoped to F26 — independent of any other rule's
    # whole-tree verdict, so unrelated tree debris cannot make this flake.
    assert "F26" in ran_rule_ids(out), f"F26 must run end-to-end through the real dispatch; ledger:\n{out}"
    assert "F26" in failed_rule_ids(out), f"F26 must FAIL on the forbidden core→providers import; ledger:\n{out}"
    # Sabotage + clean-arm control (inline): the SAME probe without the import
    # must NOT fail F26 — the verdict flips on the one-line edit. Driven through
    # the single-rule narrowed path, not a second whole-gate dispatch: the full
    # dispatch above already proves the wiring, and paying the ~40-rule gate
    # twice pushed this test past the 60s per-test timeout on loaded CI shards.
    with probe_file("kairix/core/zzz_staged_probe_f26.py", "x = 1\n") as rel:
        rc2, out2 = run_one_narrowed("F26", [rel])
    assert rc2 == 0, f"removing the forbidden import must clear F26 (sabotage + clean-arm); output:\n{out2}"


def test_always_run_f92_appears_in_a_real_doc_only_dispatch() -> None:
    """F92 (catalogue currency, always-run) is in the ran-set of a real
    doc-only staged dispatch, not just selected by ``decide``."""
    _code, out = run_staged(["docs/architecture/ENGINEERING.md"])
    assert "F92" in ran_rule_ids(out), f"F92 must run on a doc-only staged change; ledger:\n{out}"
