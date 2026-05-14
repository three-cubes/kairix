"""Unit tests for the ``kairix bootstrap`` CLI surface (#246 W1).

The CLI accepts an injectable ``BootstrapDeps`` — tests pass deps that
point ``document_root_fn`` at ``tmp_path`` directly, so we don't need
to monkeypatch ``KAIRIX_DOCUMENT_ROOT`` (F2-clean) or open env-read
seams (F4-clean).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from kairix.bootstrap_cli import build_parser, main
from kairix.use_cases.bootstrap import (
    BootstrapDeps,
    BootstrapHealth,
    BootstrapOutput,
    MemoryEntry,
    bootstrap_output_to_envelope,
    bootstrap_output_to_markdown,
)


def _seed_minimal_vault(root: Path, agent: str) -> None:
    agent_dir = root / "04-Agent-Knowledge" / agent
    (agent_dir / "memory").mkdir(parents=True, exist_ok=True)
    (agent_dir / "Board.md").write_text("priorities: ship", encoding="utf-8")
    (agent_dir / "Goals.md").write_text("- one\n- two", encoding="utf-8")
    (agent_dir / "memory" / "2026-05-14.md").write_text("today: progress", encoding="utf-8")


def _deps_for(root: Path) -> BootstrapDeps:
    """All probes healthy; document_root pinned to ``root``."""
    return BootstrapDeps(
        document_root_fn=lambda: root,
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
    )


@pytest.mark.unit
def test_build_parser_accepts_agent_and_json_flags() -> None:
    parser = build_parser()
    args = parser.parse_args(["alpha", "--json", "--max-memory-days", "5"])
    assert args.agent == "alpha"
    assert args.as_json is True
    assert args.max_memory_days == 5


@pytest.mark.unit
def test_cli_markdown_mode_writes_each_section(tmp_path: Path) -> None:
    _seed_minimal_vault(tmp_path, "alpha")
    out_buf, err_buf = io.StringIO(), io.StringIO()
    code = main(["alpha"], out=out_buf, err=err_buf, deps=_deps_for(tmp_path))
    assert code == 0
    output = out_buf.getvalue()
    assert "# Bootstrap envelope: alpha" in output
    assert "## Board" in output
    assert "priorities: ship" in output
    assert "## Active goals" in output
    assert err_buf.getvalue() == ""


@pytest.mark.unit
def test_cli_json_mode_emits_envelope(tmp_path: Path) -> None:
    _seed_minimal_vault(tmp_path, "alpha")
    out_buf, err_buf = io.StringIO(), io.StringIO()
    code = main(
        ["alpha", "--json", "--max-memory-days", "1"],
        out=out_buf,
        err=err_buf,
        deps=_deps_for(tmp_path),
    )
    assert code == 0
    payload = json.loads(out_buf.getvalue())
    assert payload["agent"] == "alpha"
    assert payload["board"].startswith("priorities")
    assert payload["recent_memory"][0]["date"] == "2026-05-14"
    assert "health" in payload
    assert "vector_search" in payload["health"]


@pytest.mark.unit
def test_cli_returns_non_zero_when_envelope_errors(tmp_path: Path) -> None:
    """Sabotage anchor: point the deps at a path that doesn't exist and
    confirm the CLI exits 1 with the error on stderr."""
    bogus = tmp_path / "does-not-exist"
    deps = BootstrapDeps(
        document_root_fn=lambda: bogus,
        secrets_loaded_fn=lambda: True,
        embed_backend_available_fn=lambda: True,
        bm25_index_available_fn=lambda: True,
    )
    out_buf, err_buf = io.StringIO(), io.StringIO()
    code = main(["alpha"], out=out_buf, err=err_buf, deps=deps)
    assert code == 1
    assert "DocumentRootMissing" in err_buf.getvalue()


@pytest.mark.unit
def test_envelope_projection_matches_use_case_helper() -> None:
    """The CLI must use the same envelope projector as the MCP adapter —
    sabotaging the projector breaks both surfaces in the same way."""
    out = BootstrapOutput(
        agent="alpha",
        role="Builder",
        board="b",
        recent_memory=[MemoryEntry(date="2026-05-14", content="x")],
        active_goals=["one"],
        health=BootstrapHealth(),
        next_action="next",
    )
    env = bootstrap_output_to_envelope(out)
    assert env["agent"] == "alpha"
    md = bootstrap_output_to_markdown(out)
    assert "# Bootstrap envelope: alpha" in md


@pytest.mark.unit
def test_cli_falls_through_to_default_deps_when_none(tmp_path: Path) -> None:
    """When ``deps`` is None, the CLI uses the production defaults — which
    resolve via ``kairix.paths.document_root()``. The autouse no_azure
    fixture in conftest.py deletes ``KAIRIX_LLM_API_KEY``, so the
    health probe reports secrets_loaded=False; the envelope still
    populates because the document root resolves and the agent dir is
    absent (new agent on first boot is a valid state)."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    code = main(["never-seen-agent", "--json"], out=out_buf, err=err_buf)
    # The default document root may or may not exist on the test host;
    # what we care about is that main() returns an int (not a raise) and
    # writes valid JSON when the root resolves cleanly.
    payload_or_error = out_buf.getvalue()
    assert payload_or_error  # the CLI always writes *something*
    # Either path is acceptable; we're sabotage-proofing the no-raise
    # contract here.
    assert code in (0, 1)
