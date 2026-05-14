"""Unit tests for ``kairix.core.health`` (#246 W3).

The shared health module is the single source of truth for "what's
working right now". Every test below drives the probe via
``HealthDeps`` injection — no @patch, no monkeypatch. Sabotage-proof
contract:

- Healthy probes → snapshot reports all-ok with empty
  ``degraded_reason`` and empty ``next_action``.
- One probe degraded → snapshot reports the offline leg, the cause,
  and a prescriptive ``next_action`` for the agent.
- A slow probe (mocked to sleep past the budget) is cancelled and
  treated as offline; the reason mentions the budget so the operator
  can debug.
- Removing the degradation case in ``_summarise_degradation`` makes
  the "every degraded snapshot carries a next_action" test fail
  (sabotage anchor).
"""

from __future__ import annotations

import time

import pytest

from kairix.core.health import (
    HEALTH_PROBE_BUDGET_S,
    HealthDeps,
    KairixHealth,
    brief_next_action,
    entity_next_action,
    health_to_envelope,
    probe_health,
    search_next_action,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Healthy path
# ---------------------------------------------------------------------------


def _healthy_deps() -> HealthDeps:
    return HealthDeps(
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
        neo4j_available_fn=lambda: True,
    )


def test_all_healthy_reports_all_ok_with_empty_directive() -> None:
    out = probe_health(_healthy_deps())
    assert out.vector_search == "ok"
    assert out.bm25 == "ok"
    assert out.chat == "ok"
    assert out.secrets_loaded is True
    assert out.degraded_reason == ""
    assert out.next_action == ""


def test_envelope_projection_is_stable_json_dict() -> None:
    snap = KairixHealth(
        vector_search="degraded",
        bm25="ok",
        chat="offline",
        secrets_loaded=False,
        degraded_reason="reason",
        next_action="do thing",
    )
    env = health_to_envelope(snap)
    assert env == {
        "vector_search": "degraded",
        "bm25": "ok",
        "chat": "offline",
        "secrets_loaded": False,
        "degraded_reason": "reason",
        "next_action": "do thing",
    }


# ---------------------------------------------------------------------------
# Single-leg degradation
# ---------------------------------------------------------------------------


def test_secrets_offline_marks_chat_offline_and_vector_degraded() -> None:
    out = probe_health(
        HealthDeps(
            secrets_loaded_fn=lambda: False,
            embed_backend_available_fn=lambda: True,
            bm25_index_available_fn=lambda: True,
            neo4j_available_fn=lambda: True,
        )
    )
    assert out.chat == "offline"
    assert out.vector_search == "degraded"
    assert out.bm25 == "ok"
    assert "KAIRIX_LLM_API_KEY" in out.degraded_reason
    assert out.next_action != ""
    assert "kairix onboard check" in out.next_action


def test_embed_backend_offline_marks_vector_degraded() -> None:
    out = probe_health(
        HealthDeps(
            secrets_loaded_fn=lambda: True,
            embed_backend_available_fn=lambda: False,
            bm25_index_available_fn=lambda: True,
            neo4j_available_fn=lambda: True,
        )
    )
    assert out.vector_search == "degraded"
    assert out.bm25 == "ok"
    assert "embed backend" in out.degraded_reason


def test_bm25_offline_with_vector_ok_yields_rebuild_directive() -> None:
    out = probe_health(
        HealthDeps(
            secrets_loaded_fn=lambda: True,
            embed_backend_available_fn=lambda: True,
            bm25_index_available_fn=lambda: False,
            neo4j_available_fn=lambda: True,
        )
    )
    assert out.bm25 == "offline"
    assert out.vector_search == "ok"
    assert "rebuild-fts" in out.next_action


def test_everything_offline_yields_unavailable_directive() -> None:
    out = probe_health(
        HealthDeps(
            secrets_loaded_fn=lambda: False,
            embed_backend_available_fn=lambda: False,
            bm25_index_available_fn=lambda: False,
            neo4j_available_fn=lambda: False,
        )
    )
    assert out.vector_search == "offline"
    assert out.bm25 == "offline"
    assert out.chat == "offline"
    # Sabotage anchor: directive must explicitly say retrieval is unavailable.
    assert "unavailable" in out.next_action.lower()


def test_probe_callable_raising_is_swallowed_to_false() -> None:
    def boom() -> bool:
        raise OSError("probe is on fire")

    out = probe_health(
        HealthDeps(
            secrets_loaded_fn=boom,
            embed_backend_available_fn=lambda: True,
            bm25_index_available_fn=lambda: True,
            neo4j_available_fn=lambda: True,
        )
    )
    # A raising probe becomes False; chat drops offline.
    assert out.secrets_loaded is False
    assert out.chat == "offline"
    assert out.degraded_reason != ""


def test_every_degraded_snapshot_carries_a_next_action() -> None:
    """Sabotage anchor: drop ``next_action`` in ``_summarise_degradation``
    and this test will fail. Walks the full Cartesian product of
    (secrets, embed, bm25) and asserts every non-all-ok combination has
    a populated directive."""
    for secrets in (True, False):
        for embed in (True, False):
            for bm25 in (True, False):
                snap = probe_health(
                    HealthDeps(
                        secrets_loaded_fn=lambda s=secrets: s,
                        embed_backend_available_fn=lambda e=embed: e,
                        bm25_index_available_fn=lambda b=bm25: b,
                        neo4j_available_fn=lambda: True,
                    )
                )
                if secrets and embed and bm25:
                    assert snap.next_action == ""
                    assert snap.degraded_reason == ""
                else:
                    assert snap.next_action != "", (
                        f"snapshot lost next_action for secrets={secrets} embed={embed} bm25={bm25}"
                    )
                    assert snap.degraded_reason != ""


# ---------------------------------------------------------------------------
# Budget enforcement — slow probes get cancelled
# ---------------------------------------------------------------------------


def test_probe_time_cap_marks_slow_probe_offline_and_returns_within_budget() -> None:
    """Sabotage anchor: remove the threading timeout in ``_run_with_timeout``
    and this test hangs for 5 seconds, exceeding the ``budget_s`` ceiling
    set below."""

    def slow_probe() -> bool:
        time.sleep(5.0)
        return True

    started = time.monotonic()
    out = probe_health(
        HealthDeps(
            secrets_loaded_fn=lambda: True,
            embed_backend_available_fn=slow_probe,
            bm25_index_available_fn=lambda: True,
            neo4j_available_fn=lambda: True,
        ),
        budget_s=0.5,
    )
    elapsed = time.monotonic() - started
    # The probe must NOT have waited for the slow callable. Slice is
    # budget_s/4 = 0.125s; even with thread-launch overhead we should
    # be well under the slow callable's 5s sleep.
    assert elapsed < 2.0, f"probe took {elapsed:.2f}s — slow probe was not cancelled"
    # The slow probe is treated as offline.
    assert out.vector_search == "degraded"
    assert "embed backend probe exceeded" in out.degraded_reason


def test_default_budget_constant_is_two_seconds() -> None:
    # The contract calls for a 2-second cap; this test pins the default
    # so a future refactor can't silently raise the cap.
    assert HEALTH_PROBE_BUDGET_S == 2.0


# ---------------------------------------------------------------------------
# Default factory wiring (sabotage check)
# ---------------------------------------------------------------------------


def test_default_factory_wires_real_callables() -> None:
    deps = HealthDeps()
    assert callable(deps.secrets_loaded_fn)
    assert callable(deps.embed_backend_available_fn)
    assert callable(deps.bm25_index_available_fn)
    assert callable(deps.neo4j_available_fn)


def test_default_factory_health_probes_return_booleans() -> None:
    """The default probes must return booleans without raising,
    even when LLM creds and the FTS index are absent."""
    deps = HealthDeps()
    for fn in (
        deps.secrets_loaded_fn,
        deps.embed_backend_available_fn,
        deps.bm25_index_available_fn,
        deps.neo4j_available_fn,
    ):
        value: object = fn()
        assert isinstance(value, bool)


def test_probe_with_default_deps_returns_kairix_health_instance() -> None:
    """End-to-end: ``probe_health()`` with no kwargs returns a frozen
    ``KairixHealth`` and never raises. Drives the production lazy
    imports."""
    out = probe_health()
    assert isinstance(out, KairixHealth)
    assert out.vector_search in {"ok", "degraded", "offline"}


# ---------------------------------------------------------------------------
# Tool-specific next_action overlays
# ---------------------------------------------------------------------------


def test_search_next_action_ok_when_healthy() -> None:
    assert search_next_action(KairixHealth()) == ""


def test_search_next_action_when_vector_degraded_points_at_bm25_only() -> None:
    snap = KairixHealth(vector_search="degraded", bm25="ok", chat="offline")
    msg = search_next_action(snap)
    assert "BM25-only" in msg
    assert "kairix onboard check" in msg


def test_search_next_action_when_bm25_offline_points_at_rebuild() -> None:
    snap = KairixHealth(vector_search="ok", bm25="offline")
    msg = search_next_action(snap)
    assert "rebuild-fts" in msg


def test_search_next_action_when_both_offline_surfaces_to_human() -> None:
    snap = KairixHealth(vector_search="offline", bm25="offline")
    msg = search_next_action(snap)
    assert "offline" in msg.lower()
    assert "surface" in msg.lower()


def test_brief_next_action_when_chat_offline_falls_back_to_search() -> None:
    snap = KairixHealth(chat="offline")
    msg = brief_next_action(snap)
    assert "tool_search" in msg
    assert "fall back" in msg.lower()


def test_brief_next_action_when_retrieval_degraded_surfaces_to_human() -> None:
    snap = KairixHealth(chat="ok", vector_search="degraded", bm25="ok")
    msg = brief_next_action(snap)
    assert "sparser" in msg or "surface" in msg.lower()


def test_brief_next_action_when_healthy_is_empty() -> None:
    assert brief_next_action(KairixHealth()) == ""


def test_entity_next_action_when_neo4j_offline_points_at_search() -> None:
    msg = entity_next_action(KairixHealth(), neo4j_available=False)
    assert "tool_search" in msg
    assert "graph offline" in msg.lower()


def test_entity_next_action_when_neo4j_ok_but_retrieval_degraded() -> None:
    snap = KairixHealth(vector_search="degraded")
    msg = entity_next_action(snap, neo4j_available=True)
    assert msg != ""
    assert "surface" in msg.lower()


def test_entity_next_action_when_everything_healthy_is_empty() -> None:
    assert entity_next_action(KairixHealth(), neo4j_available=True) == ""
