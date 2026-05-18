"""Step definitions for bootstrap.feature.

Drives ``kairix.use_cases.bootstrap.run_bootstrap`` plus its envelope
projection through injectable ``BootstrapDeps``. Each scenario builds
a per-scenario state container and seeds an on-disk vault under
``tmp_path``; the steps never monkeypatch kairix internals (F1-clean)
and never read ``KAIRIX_*`` env vars (F2/F4-clean — deps are passed
directly to ``run_bootstrap``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, then, when

from kairix.use_cases.bootstrap import (
    BootstrapDeps,
    BootstrapOutput,
    bootstrap_output_to_envelope,
    run_bootstrap,
)

pytestmark = pytest.mark.bdd


# Step-phrase fragments lifted to constants where the same literal would
# otherwise repeat ≥3 times in this module (F17: no >=10-char string
# repeated >=3 times in a module).
_PHRASE_KNOWN_AGENT_SHAPE = 'a known agent named "shape"'
_PHRASE_CALL_BOOTSTRAP_FOR_AGENT = "the agent calls bootstrap for the agent"


@pytest.fixture
def _bootstrap_state(tmp_path: Path) -> dict[str, Any]:
    """Per-scenario fresh state container."""
    return {
        "document_root": tmp_path,
        "agent": "",
        "result": None,
        "exception": None,
    }


def _seed_minimal_vault(root: Path, agent: str) -> None:
    """Lay out a believable agent vault subtree under ``root``.

    Mirrors ``tests/test_bootstrap_cli.py::_seed_minimal_vault`` so the
    BDD scenarios test against the same vault shape the unit tests use.
    """
    agent_dir = root / "04-Agent-Knowledge" / agent
    (agent_dir / "memory").mkdir(parents=True, exist_ok=True)
    (agent_dir / "Board.md").write_text("priorities: ship\n- one\n- two\n", encoding="utf-8")
    (agent_dir / "Goals.md").write_text("- land bootstrap BDD\n- wire steps\n", encoding="utf-8")
    (agent_dir / "profile.md").write_text("# Shape — product/UX builder\n", encoding="utf-8")
    (agent_dir / "memory" / "2026-05-15.md").write_text("today: BDD coverage", encoding="utf-8")


def _healthy_deps_for(root: Path) -> BootstrapDeps:
    """Build a BootstrapDeps where every probe answers 'healthy'."""
    return BootstrapDeps(
        document_root_fn=lambda: root,
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
    )


# ---------------------------------------------------------------------------
# Given — seed the vault / pick the agent
# ---------------------------------------------------------------------------


@given(_PHRASE_KNOWN_AGENT_SHAPE)
def _given_known_agent(_bootstrap_state: dict[str, Any]) -> None:
    root: Path = _bootstrap_state["document_root"]
    _seed_minimal_vault(root, "shape")
    _bootstrap_state["agent"] = "shape"


@given("a document root that does not exist on disk")
def _given_missing_document_root(_bootstrap_state: dict[str, Any], tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    _bootstrap_state["document_root"] = missing
    _bootstrap_state["agent"] = "alpha"


# ---------------------------------------------------------------------------
# When — invoke run_bootstrap
# ---------------------------------------------------------------------------


@when(_PHRASE_CALL_BOOTSTRAP_FOR_AGENT)
@when("the agent calls bootstrap for any agent")
def _when_call_bootstrap(_bootstrap_state: dict[str, Any]) -> None:
    deps = _healthy_deps_for(_bootstrap_state["document_root"])
    try:
        _bootstrap_state["result"] = run_bootstrap(_bootstrap_state["agent"], deps=deps)
    except Exception as exc:  # pragma: no cover — run_bootstrap is contract-bound to never raise
        _bootstrap_state["exception"] = exc


# ---------------------------------------------------------------------------
# Then — assertions on the envelope
# ---------------------------------------------------------------------------


def _result(state: dict[str, Any]) -> BootstrapOutput:
    out = state["result"]
    assert out is not None, "run_bootstrap was not invoked or returned None"
    assert isinstance(out, BootstrapOutput)
    return out


@then("the envelope contains a role field")
def _then_has_role(_bootstrap_state: dict[str, Any]) -> None:
    out = _result(_bootstrap_state)
    # Sabotage: stop reading profile.md in _load_role and the role field
    # collapses to "" — this assertion catches that regression because
    # the seeded vault writes a non-empty profile.md.
    assert out.role, f"expected non-empty role; got {out.role!r}"
    assert "Shape" in out.role


@then("the envelope contains a board field")
def _then_has_board(_bootstrap_state: dict[str, Any]) -> None:
    out = _result(_bootstrap_state)
    # Sabotage: drop the _load_board call from run_bootstrap and the
    # board field stays "" even though Board.md exists, tripping this.
    assert out.board, f"expected non-empty board; got {out.board!r}"
    assert "priorities" in out.board


@then("the envelope contains a recent_memory section")
def _then_has_memory(_bootstrap_state: dict[str, Any]) -> None:
    out = _result(_bootstrap_state)
    # Sabotage: short-circuit _load_recent_memory to return [] and the
    # seeded 2026-05-15.md disappears from the envelope, failing here.
    assert out.recent_memory, f"expected ≥1 memory entry; got {out.recent_memory!r}"
    assert out.recent_memory[0].date == "2026-05-15"


@then("the envelope contains a goals section")
def _then_has_goals(_bootstrap_state: dict[str, Any]) -> None:
    out = _result(_bootstrap_state)
    # Sabotage: bypass _load_goals (return []) and the bullet list from
    # Goals.md is lost — assertion catches it.
    assert out.active_goals, f"expected ≥1 active goal; got {out.active_goals!r}"
    assert "land bootstrap BDD" in out.active_goals


@then("the envelope contains a health summary")
def _then_has_health(_bootstrap_state: dict[str, Any]) -> None:
    out = _result(_bootstrap_state)
    # Sabotage: replace _probe_health with a no-op returning a stub that
    # leaves vector_search/bm25/chat as empty strings and these checks
    # fail (the healthy deps should yield "ok" across the board).
    assert out.health.vector_search == "ok", out.health
    assert out.health.bm25 == "ok", out.health
    assert out.health.chat == "ok", out.health
    assert out.health.secrets_loaded is True


@then("the envelope carries a non-empty error field")
def _then_has_error(_bootstrap_state: dict[str, Any]) -> None:
    out = _result(_bootstrap_state)
    # Sabotage: silently swallow the DocumentRootMissing branch in
    # run_bootstrap (leave error="") and a missing vault would no longer
    # surface to the agent — this assertion forces the contract.
    assert out.error, f"expected non-empty error for missing root; got {out.error!r}"
    assert "DocumentRootMissing" in out.error or "does-not-exist" in out.error


@then("the envelope carries a remediation directive")
def _then_has_remediation(_bootstrap_state: dict[str, Any]) -> None:
    out = _result(_bootstrap_state)
    # Sabotage: blank-out next_action in the doc-root-missing branch and
    # the agent loses the affordance — assertion catches the regression.
    assert out.next_action, f"expected next_action for missing root; got {out.next_action!r}"
    assert "onboard check" in out.next_action.lower() or "document root" in out.next_action.lower()


@then("the envelope does not raise an exception")
def _then_no_exception(_bootstrap_state: dict[str, Any]) -> None:
    # Sabotage: let run_bootstrap raise on its first os error instead of
    # populating ``error``; this assertion (and the When step's try/except)
    # would observe the leak and fail here.
    assert _bootstrap_state["exception"] is None, _bootstrap_state["exception"]


@then("the envelope round-trips through json.dumps and json.loads cleanly")
def _then_json_round_trips(_bootstrap_state: dict[str, Any]) -> None:
    out = _result(_bootstrap_state)
    payload = bootstrap_output_to_envelope(out)
    # Sabotage: leak a Path/datetime into the envelope projection and
    # json.dumps raises TypeError — this assertion blocks that drift.
    text = json.dumps(payload)
    decoded = json.loads(text)
    assert decoded["agent"] == out.agent
    assert "health" in decoded
    assert "recent_memory" in decoded
