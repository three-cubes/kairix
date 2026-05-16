"""End-to-end integration tests for ``kairix.platform.onboard.check``.

Unit tests cover each individual check function in isolation. These
integration tests assemble the full ``run_onboard_check`` cycle under
different failure shapes and assert the aggregate envelope behaves
correctly — failures don't short-circuit the rest of the run, the
JSON envelope stays well-formed, and remediation strings land.

Fakes:
  The check registry ``ALL_CHECKS`` is composed of free functions; we
  substitute it at the module symbol via ``monkeypatch.setattr`` so the
  cycle wires fake check callables that emit pre-shaped ``CheckResult``
  envelopes. No ``@patch`` on kairix internals (F1-clean) — direct
  attribute replacement on the module symbol is the documented pattern.
"""

from __future__ import annotations

import json

import pytest

from kairix.platform.onboard import check as check_mod
from kairix.platform.onboard.check import CheckResult, run_onboard_check

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers — build a check callable that returns a canned CheckResult
# ---------------------------------------------------------------------------


def _make_check(
    name: str,
    *,
    ok: bool,
    detail: str = "",
    fix: str | None = None,
) -> object:
    """Build a zero-arg check callable returning a canned ``CheckResult``."""

    def _check() -> CheckResult:
        return CheckResult(name=name, ok=ok, detail=detail or ("ok" if ok else "failed"), fix=fix)

    return _check


# ---------------------------------------------------------------------------
# Aggregate envelope shape — happy + degraded
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_aggregate_envelope_reports_fully_passed_when_all_checks_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All checks green → ``OnboardResult.fully_passed=True``, ``passed==total``,
    no failures.

    Sabotage: if ``run_onboard_check`` started counting passed checks
    incorrectly, ``passed != total`` and the invariant would break.
    """
    fakes = [
        _make_check("kairix_on_path", ok=True),
        _make_check("secrets_loaded", ok=True),
        _make_check("document_root_configured", ok=True),
        _make_check("vector_search_working", ok=True),
        _make_check("neo4j_reachable", ok=True),
    ]
    monkeypatch.setattr(check_mod, "ALL_CHECKS", fakes)

    result = run_onboard_check()

    assert result.fully_passed is True
    assert result.passed == result.total == len(fakes)
    assert result.failures == []


@pytest.mark.integration
def test_vector_search_failure_surfaces_non_empty_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single failing ``vector_search_working`` check produces one
    ``CheckFailure`` with a non-empty canonical remediation.

    Sabotage: if ``_remediation_for`` started returning ``""`` for a
    known check name, the assertion on the remediation string would fail.
    """
    fakes = [
        _make_check("kairix_on_path", ok=True),
        _make_check("secrets_loaded", ok=True),
        _make_check(
            "vector_search_working",
            ok=False,
            detail="search returned 0 results (vec=0, bm25=0)",
        ),
    ]
    monkeypatch.setattr(check_mod, "ALL_CHECKS", fakes)

    result = run_onboard_check()

    assert result.fully_passed is False
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.check == "vector_search_working"
    assert failure.detail.startswith("search returned 0")
    # Canonical remediation is non-empty and operator-actionable.
    assert failure.remediation
    assert "kairix embed" in failure.remediation.lower() or "docker logs" in failure.remediation.lower()


@pytest.mark.integration
def test_one_failure_does_not_short_circuit_rest_of_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``neo4j_reachable`` doesn't prevent the subsequent
    checks from running — the envelope still records every check
    result, passed or failed.

    Sabotage: if ``run_all_checks`` started ``break``-ing on first
    failure, ``total`` would be less than ``len(fakes)``.
    """
    fakes = [
        _make_check("kairix_on_path", ok=True),
        _make_check("secrets_loaded", ok=True),
        _make_check("document_root_configured", ok=True),
        _make_check("vector_search_working", ok=True),
        _make_check("neo4j_reachable", ok=False, detail="Neo4j unavailable"),
        _make_check("agent_knowledge_populated", ok=True),
        _make_check("chunk_date_populated", ok=True),
        _make_check("mcp_service", ok=True),
    ]
    monkeypatch.setattr(check_mod, "ALL_CHECKS", fakes)

    result = run_onboard_check()

    assert result.total == len(fakes)
    assert result.passed == len(fakes) - 1
    assert result.fully_passed is False
    # The failure was recorded, but every other check still ran (passed=total-1).
    assert len(result.failures) == 1
    assert result.failures[0].check == "neo4j_reachable"


@pytest.mark.integration
def test_multiple_failures_serialise_to_well_formed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aggregate envelope round-trips through ``json.dumps`` cleanly
    when multiple checks fail with multi-line ``fix`` strings.

    Sabotage: if a ``CheckFailure`` field became non-JSON-serialisable
    (e.g. a Path object instead of a string), ``json.dumps`` would raise.
    """
    from dataclasses import asdict

    fakes = [
        _make_check(
            "secrets_loaded",
            ok=False,
            detail="LLM credentials missing",
            fix="Add KAIRIX_LLM_API_KEY to /opt/kairix/service.env",
        ),
        _make_check(
            "vector_search_working",
            ok=False,
            detail="vec_failed=True, bm25-only fallback",
            fix=("Run: kairix onboard check\nCheck secrets_loaded result.\nThen: kairix embed --limit 20"),
        ),
        _make_check(
            "neo4j_reachable",
            ok=False,
            detail="connection refused",
            fix="bash scripts/install-neo4j.sh",
        ),
    ]
    monkeypatch.setattr(check_mod, "ALL_CHECKS", fakes)

    result = run_onboard_check()

    assert result.fully_passed is False
    assert len(result.failures) == 3

    # Build the JSON envelope shape the CLI emits — the structure
    # must serialise without raising and round-trip back to dict.
    payload = {
        "passed": result.passed,
        "total": result.total,
        "fully_passed": result.fully_passed,
        "failures": [asdict(f) for f in result.failures],
    }
    serialised = json.dumps(payload, indent=2)
    decoded = json.loads(serialised)

    assert decoded["passed"] == 0
    assert decoded["total"] == 3
    assert decoded["fully_passed"] is False
    assert len(decoded["failures"]) == 3
    # Each failure carries a non-empty remediation in the decoded envelope.
    for failure in decoded["failures"]:
        assert failure["check"]
        assert failure["detail"]
        assert failure["remediation"]
