"""Unit tests for ``kairix.use_cases.brief.run_brief``."""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.use_cases.brief import (
    BriefDeps,
    BriefOutput,
    brief_output_to_envelope,
    run_brief,
)


def _build_deps(
    *,
    content: str = "",
    raises: bool = False,
    out_dir: Path = Path("/tmp/brief"),
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

    return BriefDeps(generate_fn=fake_generate, briefing_dir_fn=fake_dir), captured


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
    )
    env = brief_output_to_envelope(out)
    assert env == {
        "agent": "builder",
        "content": "full content",
        "path": "/p",
        "preview": "preview",
        "error": "",
    }


@pytest.mark.unit
def test_envelope_carries_error_field() -> None:
    out = BriefOutput(agent="x", error="InvalidAgent: 'x'. Must be ...")
    env = brief_output_to_envelope(out)
    assert env["error"].startswith("InvalidAgent")
    assert env["content"] == ""
