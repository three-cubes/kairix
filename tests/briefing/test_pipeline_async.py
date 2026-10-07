"""
Regression tests for the asyncio.gather brief pipeline (issue #397 W-C).

The pre-Workstream-C pipeline used ``ThreadPoolExecutor`` + ``as_completed``
with a single wall-clock timeout (25s) — any one slow source tripped a
``FuturesTimeoutError`` that bubbled up to the caller and killed the whole
brief. Workstream C moved to ``asyncio.gather`` with per-source budgets so
a slow source contributes an empty section and the remaining sources still
populate the brief.

These tests pin the new graceful-degradation contract:

  1. A source that exceeds its budget contributes None / empty section.
  2. The brief NEVER raises TimeoutError to the caller.
  3. ``hybrid_search`` gets the longer (15s) budget; the five cheap sources
     get the shorter (3s) budget.

Sabotage-proof: each test below was mutated against production to confirm
it fails when the contract is broken, then restored. See commit body.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kairix.agents.briefing.pipeline import (
    BriefingDeps,
    BriefingPipeline,
    generate_briefing,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_writer(out_dir: Path):
    """Return a writer that drops a briefing file into the given tmp_path."""
    from datetime import datetime, timezone

    def _write(agent: str, content: str, sources_count: int = 0, token_estimate: int = 0) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{agent}-latest.md"
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%d %H:%M UTC")
        date_str = now.strftime("%Y-%m-%d")
        header = (
            f"# Agent Briefing — {agent} — {date_str}\n"
            f"_Generated: {ts} | Sources: {sources_count} | Tokens: ~{token_estimate}_\n\n"
        )
        out_path.write_text(header + content, encoding="utf-8")
        return out_path

    return _write


def _fake_synthesise(_agent: str, context: dict[str, str], max_tokens: int = 800) -> str:
    """Echo the source keys that survived back to the caller.

    Lets the tests assert which sources populated vs. which were dropped.
    """
    keys = sorted(k for k, v in context.items() if v and not k.startswith("_"))
    return "sources=" + ",".join(keys) if keys else "sources="


def _quick_source(value: str):
    return lambda *_args, **_kwargs: value


def _slow_source(value: str, sleep_s: float):
    def _fn(*_args, **_kwargs):
        time.sleep(sleep_s)
        return value

    return _fn


def _all_quick_sources() -> dict:
    return {
        "memory_logs": _quick_source("memory logs"),
        "recent_memory": _quick_source("recent memory"),
        "entity_stub": _quick_source("entity stub"),
        "knowledge_rules": _quick_source("rules"),
        "recent_decisions": _quick_source("decisions"),
        "hybrid_search": _quick_source("search hits"),
    }


def _pipeline(sources: dict, tmp_path: Path) -> BriefingPipeline:
    return BriefingPipeline(
        sources=sources,
        deps=BriefingDeps(
            synthesise_fn=_fake_synthesise,
            write_fn=_make_fake_writer(tmp_path),
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_slow_source_does_not_kill_other_sources(tmp_path):
    """A source that blows its budget contributes None — brief still assembles.

    Sabotage-proof: replacing ``return name, None`` with ``raise`` inside
    ``_bounded_source`` makes this test fail with the budget-exceeded
    exception bubbling out of ``generate_briefing``. Restored after.
    """
    sources = _all_quick_sources()
    # 4s sleep against a 3s budget → forced timeout for this source.
    sources["memory_logs"] = _slow_source("never seen", sleep_s=4.0)

    result = generate_briefing(
        "builder",
        deps=BriefingDeps(
            synthesise_fn=_fake_synthesise,
            write_fn=_make_fake_writer(tmp_path),
        ),
        sources=sources,
    )

    # Five non-timeout sources populated; memory_logs absent.
    assert "sources=entity_stub,hybrid_search,knowledge_rules,recent_decisions,recent_memory" in result
    # memory_logs being absent is the deterministic proof the 3s budget cut
    # the 4s source off: had the pipeline waited for it, its value would
    # have populated the section. No wall-clock ceiling (F82).
    assert "memory_logs" not in result.split("sources=")[-1].split("\n")[0]


@pytest.mark.integration
def test_brief_never_raises_timeouterror_to_caller(tmp_path):
    """Multiple sources timing out + a raising source still produces a brief."""
    sources = {
        "memory_logs": _slow_source("late", sleep_s=4.0),  # timeout
        "recent_memory": _quick_source("recent memory"),
        "entity_stub": lambda *_a, **_k: (_ for _ in ()).throw(  # raises
            RuntimeError("entity stub blew up"),
        ),
        "knowledge_rules": _quick_source("rules"),
        "recent_decisions": _slow_source("late", sleep_s=4.0),  # timeout
        "hybrid_search": _quick_source("search hits"),
    }

    # The contract: no exception escapes. If a TimeoutError or any other
    # exception leaks out, this test fails on the call line.
    result = generate_briefing(
        "builder",
        deps=BriefingDeps(
            synthesise_fn=_fake_synthesise,
            write_fn=_make_fake_writer(tmp_path),
        ),
        sources=sources,
    )

    # Three surviving sources synthesise; the timed-out + raising ones are absent.
    assert "sources=hybrid_search,knowledge_rules,recent_memory" in result


@pytest.mark.integration
def test_all_sources_succeed_within_budget(tmp_path):
    """Steady-state path: every source returns promptly → six populated sections."""
    result = generate_briefing(
        "builder",
        deps=BriefingDeps(
            synthesise_fn=_fake_synthesise,
            write_fn=_make_fake_writer(tmp_path),
        ),
        sources=_all_quick_sources(),
    )

    assert "sources=entity_stub,hybrid_search,knowledge_rules,memory_logs,recent_decisions,recent_memory" in result


@pytest.mark.integration
def test_hybrid_search_gets_longer_budget(tmp_path):
    """``hybrid_search`` keeps its slot at 4s; an entity_stub at 4s does not.

    Sleep just past the 3s cheap-source budget. ``hybrid_search`` should
    finish within its 15s budget, ``entity_stub`` should be cancelled.
    The asymmetry proves the per-source budget map is wired correctly —
    a single uniform timeout would let both pass or both fail together.

    Sabotage-proof: setting all six budgets to 3s in ``_SOURCE_BUDGETS_S``
    makes this test fail because ``hybrid_search`` then also drops.
    Restored after.
    """
    sources = _all_quick_sources()
    sources["hybrid_search"] = _slow_source("late but valid", sleep_s=4.0)
    sources["entity_stub"] = _slow_source("late and dropped", sleep_s=4.0)

    result = generate_briefing(
        "builder",
        deps=BriefingDeps(
            synthesise_fn=_fake_synthesise,
            write_fn=_make_fake_writer(tmp_path),
        ),
        sources=sources,
    )

    # entity_stub dropped (3s budget); hybrid_search kept (15s budget).
    populated_line = result.split("sources=")[-1].split("\n")[0]
    assert "hybrid_search" in populated_line, f"hybrid_search should keep 15s budget; got: {populated_line}"
    assert "entity_stub" not in populated_line, f"entity_stub should hit 3s budget; got: {populated_line}"


@pytest.mark.integration
def test_pipeline_class_inherits_async_contract(tmp_path):
    """BriefingPipeline.generate (the composition entry point) preserves the contract."""
    sources = _all_quick_sources()
    sources["memory_logs"] = _slow_source("late", sleep_s=4.0)
    pipeline = _pipeline(sources, tmp_path)

    result = pipeline.generate("builder")

    assert isinstance(result, str)
    populated_line = result.split("sources=")[-1].split("\n")[0]
    assert "memory_logs" not in populated_line
    assert "hybrid_search" in populated_line
