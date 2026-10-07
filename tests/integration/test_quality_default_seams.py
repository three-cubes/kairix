"""F86: the probe / soak / eval production DI-default seams actually execute.

``run_probe_search`` / ``run_probe_burst`` bind ``_default_suite_loader``
and ``_default_search_fn`` when no ``suite_loader`` / ``searcher`` is
injected; ``run_soak`` binds ``_default_workload_runner``;
``LLMJudgeScorer`` binds ``_default_chat_backend``. Every other test of
those surfaces injects a fake, so before F86 none of the production
defaults ever ran (the escape-4 shape).

Each test here leaves exactly the seam under test UNINJECTED and asserts
on what the real implementation did:

* the suite loaders parse a real suite YAML from ``tmp_path`` (the search
  leg is faked so the assertion is on the loaded cases);
* the search defaults drive the real factory-built pipeline on an
  isolated, unconfigured platform — every query is counted as an error
  (no ``provider:``), never raised;
* the soak workload default resolves the suite through the bundled-suite
  lookup and reports the not-found error on the result envelope;
* the LLM-judge default refuses to build without a configured provider.

``XDG_CONFIG_HOME`` and the cwd point at ``tmp_path`` (F2-clean — no
``KAIRIX_*`` env is touched) so no shared or operator config leaks in.

Sabotage-proofs (executed; each restored):
  * runner ``_default_suite_loader`` → returned ``[]`` —
    ``test_probe_search_default_suite_loader_reads_the_suite_file`` failed.
  * burst ``_default_search_fn`` → returned ``None`` instead of searching —
    ``test_probe_burst_default_search_fn_drives_the_real_pipeline`` failed
    (0 errors).
  * soak ``_default_workload_runner`` → returned ``{}`` without resolving
    the suite — ``test_soak_default_workload_runner_resolves_the_suite``
    failed (``error`` empty).
  * scorers ``_default_chat_backend`` → returned ``None`` —
    ``test_llm_judge_default_chat_backend_requires_a_provider`` failed.
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from kairix.core.factory import reset_search_pipeline_cache
from kairix.core.search.config_loader import reset_config_cache
from kairix.paths import clear_cache
from kairix.quality.eval.scorers import LLMJudgeScorer
from kairix.quality.probe.burst import run_probe_burst
from kairix.quality.probe.runner import SampledQuery, run_probe_search
from kairix.quality.soak.runner import run_soak

pytestmark = pytest.mark.integration

_CONFIG_OVERRIDES = ("KAIRIX_CONFIG_PATH", "KAIRIX_CONFIG_OVERLAY_PATH", "KAIRIX_CONFIG_BASE_PATH")

_SUITE_YAML = textwrap.dedent(
    """\
    meta:
      agent: agent-alpha
      collections:
        - vault
      version: "1.0"
      created: "2026-03-23"

    cases:
      - id: R01
        category: recall
        query: "rollout kickoff decision"
        gold_path: "01-projects/rollout/kickoff.md"
        score_method: exact
      - id: C01
        category: conceptual
        query: "what is the memory architecture"
        gold_path: null
        score_method: llm
    """
)


@dataclass(frozen=True)
class _Case:
    """Minimal BenchmarkCase stand-in — the sampler reads id/category/query/agent."""

    id: str
    category: str
    query: str
    agent: str | None = None


@pytest.fixture
def unconfigured_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """No kairix config resolves: empty XDG config home + a clean cwd."""
    leaked = [name for name in _CONFIG_OVERRIDES if os.environ.get(name)]
    if leaked:
        # Skip rationale: a developer shell exported a config override (never
        # set in CI, where these seams are enforced); the "unconfigured
        # platform" premise of these assertions would not hold.
        pytest.skip(f"config override exported in this shell: {', '.join(leaked)}")
    (tmp_path / "xdg-config").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.chdir(tmp_path)
    clear_cache()
    reset_config_cache()
    reset_search_pipeline_cache()
    yield tmp_path
    clear_cache()
    reset_config_cache()
    reset_search_pipeline_cache()


def _suite_file(root: Path) -> Path:
    path = root / "probe-suite.yaml"
    path.write_text(_SUITE_YAML, encoding="utf-8")
    return path


def _recording_searcher(seen: list[str]) -> Callable[[SampledQuery], dict[str, str]]:
    def search(q: SampledQuery) -> dict[str, str]:
        seen.append(q.case_id)
        return {"results": "fake"}

    return search


def _one_case_loader(_suite: str) -> list[_Case]:
    return [_Case(id="R01", category="recall", query="rollout kickoff decision")]


# ── suite loaders (search leg faked) ──────────────────────────────────────


def test_probe_search_default_suite_loader_reads_the_suite_file(tmp_path: Path) -> None:
    seen: list[str] = []

    result = run_probe_search(str(_suite_file(tmp_path)), queries=4, searcher=_recording_searcher(seen), warmup=False)

    assert result.queries == 4
    assert result.errors == 0
    assert set(seen) <= {"R01", "C01"} and seen


def test_probe_burst_default_suite_loader_reads_the_suite_file(tmp_path: Path) -> None:
    seen: list[str] = []

    result = run_probe_burst(str(_suite_file(tmp_path)), total_queries=3, searcher=_recording_searcher(seen))

    assert result.total_queries == 3
    assert result.errors == 0
    assert set(seen) <= {"R01", "C01"} and len(seen) == 3


# ── search defaults (real factory-built pipeline, unconfigured) ───────────


@pytest.mark.usefixtures("unconfigured_platform")
def test_probe_search_default_search_fn_drives_the_real_pipeline() -> None:
    result = run_probe_search("ignored", queries=2, suite_loader=_one_case_loader, warmup=False)

    # No provider configured → the real factory refuses to build; the
    # executor records each query as an error instead of raising.
    assert result.queries == 2
    assert result.errors == 2
    assert result.passed is False


@pytest.mark.usefixtures("unconfigured_platform")
def test_probe_burst_default_search_fn_drives_the_real_pipeline() -> None:
    result = run_probe_burst("ignored", total_queries=2, suite_loader=_one_case_loader)

    assert result.total_queries == 2
    assert result.errors == 2
    assert result.passed is False


# ── soak workload default ─────────────────────────────────────────────────


def test_soak_default_workload_runner_resolves_the_suite() -> None:
    result = run_soak("f86-no-such-suite", repeat=2)

    assert result.passed is False
    assert result.iterations == []
    assert result.error.startswith("FileNotFoundError: Suite 'f86-no-such-suite' not found")


# ── LLM judge chat backend default ────────────────────────────────────────


@pytest.mark.usefixtures("unconfigured_platform")
def test_llm_judge_default_chat_backend_requires_a_provider() -> None:
    with pytest.raises(ValueError, match="missing the required 'provider:' field"):
        LLMJudgeScorer()
