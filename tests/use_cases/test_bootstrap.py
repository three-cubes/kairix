"""Unit tests for ``kairix.use_cases.bootstrap.run_bootstrap`` (#246 W1).

Sabotage-proof contract:
- "all healthy" returns a full envelope with non-empty board, memory, goals.
- vault-missing returns ``error`` populated and a remediation-bearing
  ``next_action``.
- secrets missing populates ``health.secrets_loaded=False`` and
  ``health.vector_search="offline"`` but **still returns** board + memory.
- ``max_memory_days=0`` returns empty ``recent_memory`` with no error.
- ``BootstrapDeps()`` default factory wires real callables — setting any
  default-factory field to None during sabotage breaks the contract.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kairix.use_cases.bootstrap import (
    BootstrapDeps,
    BootstrapHealth,
    BootstrapOutput,
    MemoryEntry,
    bootstrap_health_to_envelope,
    bootstrap_output_to_envelope,
    bootstrap_output_to_markdown,
    run_bootstrap,
)

# ---------------------------------------------------------------------------
# Vault scaffolding helper
# ---------------------------------------------------------------------------


def _seed_vault(
    root: Path,
    agent: str,
    *,
    board: str | None = "Sprint priorities:\n- Ship #246",
    goals: str | None = "# Goals\n- Land bootstrap\n- Rewrite descriptions",
    memory: dict[str, str] | None = None,
    role: str | None = "Builder — agentic infrastructure",
) -> Path:
    """Lay out an agent's vault subtree and return the agent dir."""
    agent_dir = root / "04-Agent-Knowledge" / agent
    (agent_dir / "memory").mkdir(parents=True, exist_ok=True)
    if board is not None:
        (agent_dir / "Board.md").write_text(board, encoding="utf-8")
    if goals is not None:
        (agent_dir / "Goals.md").write_text(goals, encoding="utf-8")
    if role is not None:
        (agent_dir / "profile.md").write_text(f"# {role}", encoding="utf-8")
    if memory:
        for date_str, content in memory.items():
            (agent_dir / "memory" / f"{date_str}.md").write_text(content, encoding="utf-8")
    return agent_dir


def _healthy_deps(root: Path) -> BootstrapDeps:
    """Construct deps where every probe returns the healthy answer."""
    return BootstrapDeps(
        document_root_fn=lambda: root,
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_healthy_returns_full_envelope(tmp_path: Path) -> None:
    _seed_vault(
        tmp_path,
        "alpha",
        memory={
            "2026-05-12": "## 12 May\nWorked on bootstrap design.",
            "2026-05-13": "## 13 May\nLanded W1 skeleton.",
            "2026-05-14": "## 14 May\nWired descriptions.",
        },
    )

    out = run_bootstrap("alpha", deps=_healthy_deps(tmp_path), max_memory_days=3)

    assert out.error == ""
    assert out.agent == "alpha"
    assert "Ship #246" in out.board
    assert out.active_goals == ["Land bootstrap", "Rewrite descriptions"]
    assert [e.date for e in out.recent_memory] == ["2026-05-14", "2026-05-13", "2026-05-12"]
    assert out.recent_memory[0].content.startswith("## 14 May")
    assert out.role == "Builder — agentic infrastructure"
    assert out.health.vector_search == "ok"
    assert out.health.bm25 == "ok"
    assert out.health.chat == "ok"
    assert out.health.secrets_loaded is True
    assert out.health.degraded_reason == ""
    assert "Read your Board" in out.next_action


# ---------------------------------------------------------------------------
# Vault-missing branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_vault_path_missing_returns_error_and_remediation(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    deps = BootstrapDeps(
        document_root_fn=lambda: missing,
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
    )

    out = run_bootstrap("alpha", deps=deps)

    assert out.error.startswith("DocumentRootMissing")
    assert "KAIRIX_DOCUMENT_ROOT" in out.next_action
    assert "ask your admin" in out.next_action
    # Sabotage check: the test must not pass if remediation drops the
    # 'kairix onboard check' prescriptive step.
    assert "kairix onboard check" in out.next_action


@pytest.mark.unit
def test_document_root_fn_raising_populates_error_not_an_exception(tmp_path: Path) -> None:
    def boom() -> Path:
        raise RuntimeError("kaboom")

    deps = BootstrapDeps(
        document_root_fn=boom,
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
    )

    out = run_bootstrap("alpha", deps=deps)
    assert out.error == "RuntimeError: kaboom"
    assert "Configure KAIRIX_DOCUMENT_ROOT" in out.next_action


# ---------------------------------------------------------------------------
# Degraded but still useful — the core W1 affordance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_secrets_missing_still_returns_board_and_memory(tmp_path: Path) -> None:
    _seed_vault(
        tmp_path,
        "alpha",
        board="Critical: ship the release",
        memory={"2026-05-14": "Today: fix the secrets path."},
    )
    deps = BootstrapDeps(
        document_root_fn=lambda: tmp_path,
        secrets_loaded_fn=lambda: False,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
    )

    out = run_bootstrap("alpha", deps=deps)

    # Affordance contract: degraded health does NOT empty the envelope.
    assert out.error == ""
    assert "ship the release" in out.board
    assert out.recent_memory[0].content == "Today: fix the secrets path."
    assert out.active_goals == ["Land bootstrap", "Rewrite descriptions"]

    # Health: chat offline, vector_search degraded (embed backend still
    # imports), bm25 still ok.
    assert out.health.secrets_loaded is False
    assert out.health.chat == "offline"
    assert out.health.vector_search == "degraded"
    assert out.health.bm25 == "ok"
    assert "KAIRIX_LLM_API_KEY" in out.health.degraded_reason
    # Prescriptive next-action surfaces BM25 fallback + admin escalation.
    assert "BM25" in out.next_action
    assert "surface" in out.next_action.lower()


@pytest.mark.unit
def test_both_vector_legs_offline_yields_offline_state(tmp_path: Path) -> None:
    _seed_vault(tmp_path, "alpha", memory={"2026-05-14": "x"})
    deps = BootstrapDeps(
        document_root_fn=lambda: tmp_path,
        secrets_loaded_fn=lambda: False,
        embed_backend_available_fn=lambda: False,
        bm25_index_available_fn=lambda: False,
    )

    out = run_bootstrap("alpha", deps=deps)

    assert out.health.vector_search == "offline"
    assert out.health.bm25 == "offline"
    assert out.health.chat == "offline"
    # Sabotage anchor: the directive must NOT silently soothe — it
    # explicitly says retrieval is unavailable.
    assert "unavailable" in out.next_action.lower()


@pytest.mark.unit
def test_bm25_offline_but_vector_ok_yields_rebuild_fts_directive(tmp_path: Path) -> None:
    _seed_vault(tmp_path, "alpha")
    deps = BootstrapDeps(
        document_root_fn=lambda: tmp_path,
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: False,
    )

    out = run_bootstrap("alpha", deps=deps)
    assert out.health.vector_search == "ok"
    assert out.health.bm25 == "offline"
    assert "rebuild-fts" in out.next_action


@pytest.mark.unit
def test_probe_callable_raising_is_swallowed_to_false(tmp_path: Path) -> None:
    _seed_vault(tmp_path, "alpha")

    def explosive() -> bool:
        raise OSError("probe is on fire")

    deps = BootstrapDeps(
        document_root_fn=lambda: tmp_path,
        secrets_loaded_fn=explosive,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
    )

    out = run_bootstrap("alpha", deps=deps)
    # The raising probe is treated as a False signal — chat goes offline.
    assert out.health.secrets_loaded is False
    assert out.health.chat == "offline"
    # And run_bootstrap still returns cleanly.
    assert out.error == ""


# ---------------------------------------------------------------------------
# Max memory days
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_max_memory_days_zero_returns_empty_recent_memory(tmp_path: Path) -> None:
    _seed_vault(
        tmp_path,
        "alpha",
        memory={"2026-05-14": "should not appear"},
    )
    deps = _healthy_deps(tmp_path)
    out = run_bootstrap("alpha", deps=deps, max_memory_days=0)
    assert out.recent_memory == []
    assert out.error == ""


@pytest.mark.unit
def test_max_memory_days_truncates_to_newest_n(tmp_path: Path) -> None:
    _seed_vault(
        tmp_path,
        "alpha",
        memory={
            "2026-05-10": "a",
            "2026-05-11": "b",
            "2026-05-12": "c",
            "2026-05-13": "d",
            "2026-05-14": "e",
        },
    )
    out = run_bootstrap("alpha", deps=_healthy_deps(tmp_path), max_memory_days=2)
    assert [e.date for e in out.recent_memory] == ["2026-05-14", "2026-05-13"]


@pytest.mark.unit
def test_missing_memory_dir_returns_empty_list_no_error(tmp_path: Path) -> None:
    _seed_vault(tmp_path, "alpha", memory=None)
    # The seed helper always creates memory/; remove it to drive this branch.
    (tmp_path / "04-Agent-Knowledge" / "alpha" / "memory").rmdir()

    out = run_bootstrap("alpha", deps=_healthy_deps(tmp_path))
    assert out.recent_memory == []
    assert out.error == ""


# ---------------------------------------------------------------------------
# Agent dir missing — new agent on first boot
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_agent_returns_scaffolding_directive(tmp_path: Path) -> None:
    out = run_bootstrap("never-seen", deps=_healthy_deps(tmp_path))
    assert out.error == ""
    assert out.board == ""
    assert out.recent_memory == []
    assert out.active_goals == []
    # Directive must tell the human to scaffold the directory.
    assert "scaffold" in out.next_action.lower()
    assert "04-Agent-Knowledge" in out.next_action


@pytest.mark.unit
def test_empty_board_still_yields_useful_next_action(tmp_path: Path) -> None:
    _seed_vault(tmp_path, "alpha", board="")
    out = run_bootstrap("alpha", deps=_healthy_deps(tmp_path))
    assert "Board is empty" in out.next_action


# ---------------------------------------------------------------------------
# Default-factory wiring (sabotage check)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_factory_wires_real_callables() -> None:
    """``BootstrapDeps()`` must populate every field with a real callable.

    Sabotage proof: if any default_factory regressed to ``None``, this
    test fails on the ``callable`` assertion below.
    """
    deps = BootstrapDeps()
    assert callable(deps.document_root_fn)
    assert callable(deps.secrets_loaded_fn)
    assert callable(deps.embed_backend_available_fn)
    assert callable(deps.bm25_index_available_fn)


@pytest.mark.unit
def test_default_factory_health_probes_dont_raise() -> None:
    """The default health probes must return booleans without raising,
    even when LLM creds and the FTS index are absent."""
    deps = BootstrapDeps()
    for fn in (
        deps.secrets_loaded_fn,
        deps.embed_backend_available_fn,
        deps.bm25_index_available_fn,
    ):
        value: object = fn()
        assert isinstance(value, bool)


@pytest.mark.unit
def test_run_bootstrap_with_default_deps_returns_envelope(tmp_path: Path) -> None:
    """The defaults integrate end-to-end — even with no document root
    seeded, the use case must produce a structured envelope and never
    raise. Drive through the document_root_fn override only; everything
    else uses the production defaults."""
    deps = replace(BootstrapDeps(), document_root_fn=lambda: tmp_path)
    out = run_bootstrap("alpha", deps=deps)
    assert isinstance(out, BootstrapOutput)
    # tmp_path exists but the agent dir doesn't.
    assert out.error == ""
    assert isinstance(out.health, BootstrapHealth)


# ---------------------------------------------------------------------------
# Goals loader edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_goals_supports_numbered_and_star_bullets(tmp_path: Path) -> None:
    _seed_vault(
        tmp_path,
        "alpha",
        goals="# Goals\n1. one\n* two\n- three",
    )
    out = run_bootstrap("alpha", deps=_healthy_deps(tmp_path))
    assert out.active_goals == ["one", "two", "three"]


@pytest.mark.unit
def test_goals_fallback_to_plain_lines_when_no_bullets(tmp_path: Path) -> None:
    _seed_vault(tmp_path, "alpha", goals="# Goals\nplain goal one\nplain goal two")
    out = run_bootstrap("alpha", deps=_healthy_deps(tmp_path))
    assert out.active_goals == ["plain goal one", "plain goal two"]


@pytest.mark.unit
def test_role_from_role_md_when_profile_missing(tmp_path: Path) -> None:
    _seed_vault(tmp_path, "alpha", role=None)
    agent_dir = tmp_path / "04-Agent-Knowledge" / "alpha"
    (agent_dir / "Role.md").write_text("# Shape — design lead", encoding="utf-8")
    out = run_bootstrap("alpha", deps=_healthy_deps(tmp_path))
    assert out.role == "Shape — design lead"


# ---------------------------------------------------------------------------
# Envelope projection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_health_envelope_carries_all_fields() -> None:
    h = BootstrapHealth(
        vector_search="degraded",
        bm25="ok",
        chat="offline",
        secrets_loaded=False,
        degraded_reason="missing creds",
        next_action="Surface to your human.",
    )
    env = bootstrap_health_to_envelope(h)
    assert env == {
        "vector_search": "degraded",
        "bm25": "ok",
        "chat": "offline",
        "secrets_loaded": False,
        "degraded_reason": "missing creds",
        "next_action": "Surface to your human.",
    }


@pytest.mark.unit
def test_envelope_carries_all_fields_in_stable_shape() -> None:
    out = BootstrapOutput(
        agent="alpha",
        role="Builder",
        board="board body",
        recent_memory=[MemoryEntry(date="2026-05-14", content="x")],
        active_goals=["g1"],
        health=BootstrapHealth(),
        next_action="next",
    )
    env = bootstrap_output_to_envelope(out)
    assert env["agent"] == "alpha"
    assert env["role"] == "Builder"
    assert env["board"] == "board body"
    assert env["recent_memory"] == [{"date": "2026-05-14", "content": "x"}]
    assert env["active_goals"] == ["g1"]
    assert env["next_action"] == "next"
    assert env["error"] == ""
    assert env["health"]["vector_search"] == "ok"


@pytest.mark.unit
def test_markdown_render_includes_each_section() -> None:
    out = BootstrapOutput(
        agent="alpha",
        role="Builder",
        board="board body",
        recent_memory=[MemoryEntry(date="2026-05-14", content="memo")],
        active_goals=["g1"],
        health=BootstrapHealth(
            vector_search="degraded",
            bm25="ok",
            chat="offline",
            secrets_loaded=False,
            degraded_reason="missing creds",
            next_action="Surface to your human.",
        ),
        next_action="Surface to your human.",
    )
    md = bootstrap_output_to_markdown(out)
    assert "# Bootstrap envelope: alpha" in md
    assert "**Role:** Builder" in md
    assert "## Health" in md
    assert "vector_search: degraded" in md
    assert "## Board" in md
    assert "board body" in md
    assert "## Active goals" in md
    assert "g1" in md
    assert "## Recent memory" in md
    assert "### 2026-05-14" in md
    assert "memo" in md
    assert "degraded_reason: missing creds" in md


@pytest.mark.unit
def test_markdown_render_uses_placeholders_when_sections_empty() -> None:
    out = BootstrapOutput(agent="alpha", health=BootstrapHealth())
    md = bootstrap_output_to_markdown(out)
    assert "_(no Board.md found)_" in md
    assert "_(no Goals.md found)_" in md
    assert "_(no recent memory entries)_" in md


# ---------------------------------------------------------------------------
# Probe protocol — sabotage proof for type assumptions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_probe_returning_non_bool_is_coerced(tmp_path: Path) -> None:
    """A probe returning a truthy non-bool (e.g. an int) is still treated
    as "available". Sabotage by returning ``False`` flips the envelope."""
    _seed_vault(tmp_path, "alpha")

    def truthy_int() -> bool:
        # Deliberate non-bool to drive the coercion branch in
        # ``_safe_bool`` — proves the probe protocol tolerates truthy
        # ints from third-party adapters.
        return 1  # type: ignore[return-value]  # F3 rationale: deliberate non-bool to exercise _safe_bool coercion

    deps = BootstrapDeps(
        document_root_fn=lambda: tmp_path,
        secrets_loaded_fn=truthy_int,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
    )
    out = run_bootstrap("alpha", deps=deps)
    assert out.health.secrets_loaded is True
