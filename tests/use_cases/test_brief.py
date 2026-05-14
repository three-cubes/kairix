"""Unit tests for ``kairix.use_cases.brief.run_brief``."""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.core.health import HealthDeps, KairixHealth
from kairix.use_cases.brief import (
    BriefDeps,
    BriefOutput,
    brief_output_to_envelope,
    run_brief,
)


def _healthy_health_deps() -> HealthDeps:
    """Inject probes that report all-green so brief proceeds to generate."""
    return HealthDeps(
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
        neo4j_available_fn=lambda: True,
    )


def _build_deps(
    *,
    content: str = "",
    raises: bool = False,
    out_dir: Path = Path("/tmp/brief"),
    health_deps: HealthDeps | None = None,
) -> tuple[BriefDeps, dict[str, list]]:
    captured: dict[str, list] = {"generate": [], "dir": []}

    def fake_generate(agent: str, **kwargs: object) -> str:
        captured["generate"].append((agent, kwargs))
        if raises:
            raise RuntimeError("boom")
        return content

    def fake_dir() -> Path:
        captured["dir"].append(True)
        return out_dir

    return (
        BriefDeps(
            generate_fn=fake_generate,
            briefing_dir_fn=fake_dir,
            health_deps=health_deps or _healthy_health_deps(),
        ),
        captured,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_returns_content_path_and_preview() -> None:
    deps, captured = _build_deps(
        content="\n".join([f"line {i}" for i in range(50)]),
        out_dir=Path("/var/lib/kairix/briefing"),
    )
    out = run_brief("builder", deps=deps)

    assert out.error == ""
    assert out.agent == "builder"
    assert out.content.startswith("line 0")
    assert out.path == "/var/lib/kairix/briefing/builder-latest.md"
    # Preview is first 30 lines, joined by newlines.
    assert out.preview == "\n".join([f"line {i}" for i in range(30)])
    assert captured["generate"][0][0] == "builder"


@pytest.mark.unit
def test_short_content_preview_equals_content() -> None:
    deps, _ = _build_deps(content="line 1\nline 2")
    out = run_brief("shape", deps=deps)
    assert out.preview == "line 1\nline 2"


@pytest.mark.unit
def test_agent_name_lowercased_and_stripped() -> None:
    deps, captured = _build_deps(content="x")
    out = run_brief("  Shape  ", deps=deps)
    assert out.agent == "shape"
    assert captured["generate"][0][0] == "shape"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_agent_returns_error_envelope() -> None:
    deps, captured = _build_deps()
    out = run_brief("rogue", deps=deps)
    assert out.error.startswith("InvalidAgent")
    assert captured["generate"] == []  # never reached the generator


@pytest.mark.unit
def test_empty_agent_string_is_invalid() -> None:
    deps, _ = _build_deps()
    out = run_brief("", deps=deps)
    assert "InvalidAgent" in out.error


@pytest.mark.parametrize("agent", ["builder", "shape", "growth", "consultant"])
@pytest.mark.unit
def test_each_documented_agent_is_accepted(agent: str) -> None:
    deps, _ = _build_deps(content="x")
    out = run_brief(agent, deps=deps)
    assert out.error == ""
    assert out.agent == agent


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generate_failure_yields_error_envelope() -> None:
    deps, _ = _build_deps(raises=True)
    out = run_brief("builder", deps=deps)
    assert out.error.startswith("RuntimeError:")
    assert out.content == ""


# ---------------------------------------------------------------------------
# Envelope projection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_envelope_carries_all_fields() -> None:
    out = BriefOutput(
        agent="builder",
        content="full content",
        path="/p",
        preview="preview",
        health=KairixHealth(),
    )
    env = brief_output_to_envelope(out)
    assert env["agent"] == "builder"
    assert env["content"] == "full content"
    assert env["path"] == "/p"
    assert env["preview"] == "preview"
    assert env["error"] == ""
    assert env["health"]["vector_search"] == "ok"
    assert env["health"]["next_action"] == ""


@pytest.mark.unit
def test_envelope_carries_error_field() -> None:
    out = BriefOutput(agent="x", error="InvalidAgent: 'x'. Must be ...")
    env = brief_output_to_envelope(out)
    assert env["error"].startswith("InvalidAgent")
    assert env["content"] == ""
    # Even an error envelope carries the health snapshot.
    assert "vector_search" in env["health"]


# ---------------------------------------------------------------------------
# W3: health envelope contract (#246)
# ---------------------------------------------------------------------------


def _chat_offline_health_deps() -> HealthDeps:
    return HealthDeps(
        secrets_loaded_fn=lambda: False,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
        neo4j_available_fn=lambda: True,
    )


@pytest.mark.unit
def test_healthy_state_brief_carries_clean_health_field() -> None:
    deps, _ = _build_deps(content="hello world")
    out = run_brief("builder", deps=deps)
    assert out.health.vector_search == "ok"
    assert out.health.chat == "ok"
    assert out.health.degraded_reason == ""
    assert out.health.next_action == ""


@pytest.mark.unit
def test_chat_offline_returns_empty_content_but_prescriptive_next_action() -> None:
    """W3 contract: when chat is offline brief returns an envelope with
    empty content (not a misleading partial success) and a directive
    that tells the agent to fall back to tool_search.

    Sabotage anchor: dropping the directive in ``brief_next_action``
    makes this test fail on the ``next_action`` assertion."""
    deps, captured = _build_deps(content="never seen", health_deps=_chat_offline_health_deps())
    out = run_brief("builder", deps=deps)

    # The brief was not generated (would have crashed in production).
    assert out.content == ""
    assert out.path == ""
    assert out.preview == ""
    # No exception surfaced; ``error`` stays empty — the affordance is on health.
    assert out.error == ""
    # Health surfaces the degradation.
    assert out.health.chat == "offline"
    assert out.health.degraded_reason != ""
    # Prescriptive directive points the agent at tool_search.
    assert out.health.next_action != ""
    assert "tool_search" in out.health.next_action
    assert "fall back" in out.health.next_action.lower()
    # Sabotage: the generator must not have been called when chat is offline.
    assert captured["generate"] == []


@pytest.mark.unit
def test_brief_envelope_includes_health_dict() -> None:
    out = BriefOutput(agent="builder", content="x", health=KairixHealth())
    env = brief_output_to_envelope(out)
    assert "health" in env
    assert env["health"]["chat"] == "ok"
    assert env["health"]["next_action"] == ""


@pytest.mark.unit
def test_brief_invalid_agent_still_carries_health_snapshot() -> None:
    deps, _ = _build_deps(health_deps=_chat_offline_health_deps())
    out = run_brief("rogue", deps=deps)
    assert out.error.startswith("InvalidAgent")
    # Even on validation failure the agent gets a health snapshot.
    assert out.health.chat == "offline"
    assert out.health.next_action != ""
