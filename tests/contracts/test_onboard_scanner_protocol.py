"""Protocol-shape contract for :mod:`kairix.agents.onboarding.scanner`
(PR 1.4 / #420).

Pins the structural promises of :class:`ProposedScope`,
:func:`scan_for_agents`, and :func:`discover_single_agent`. The
``kairix onboard scan`` CLI + the ``tool_onboard_scan`` MCP tool both
consume these shapes; this contract freezes them so the public
surface cannot regress silently.

F43 parity: every test runs ONE body over the REAL scanner module
(``scan_for_agents`` / ``discover_single_agent`` with the production
detector registry) AND the canonical :class:`FakeOnboardScanner`.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kairix.agents.onboarding.scanner import (
    ProposedScope,
    discover_single_agent,
    scan_for_agents,
)
from tests.fakes import FakeOnboardScanner

pytestmark = pytest.mark.contract


@pytest.fixture(params=["real", "fake"])
def scanner(request: pytest.FixtureRequest) -> Any:
    """The real scanner module surface, or the canonical fake."""
    if request.param == "fake":
        return FakeOnboardScanner()
    return SimpleNamespace(scan_for_agents=scan_for_agents, discover_single_agent=discover_single_agent)


def _seed_agent(memory_root: Path, name: str = "agent-alpha") -> None:
    agent_dir = memory_root / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "Board.md").write_text("# board")


# Sabotage-proof (executed): removed `frozen=True` from the @dataclass
# decorator on ProposedScope (kairix/agents/onboarding/scanner.py) →
# `scope.file_count = 99` succeeded instead of raising
# FrozenInstanceError on the real leg; restored.
def test_proposed_scope_is_frozen_dataclass(scanner: Any, tmp_path: Path) -> None:
    """``ProposedScope`` is a frozen dataclass — callers depend on
    instances being hashable and immutable when they cache or compare
    proposals across runs."""
    _seed_agent(tmp_path)
    sample: Any = scanner.discover_single_agent("agent-alpha", memory_root=tmp_path)
    assert dataclasses.is_dataclass(type(sample))
    with pytest.raises(dataclasses.FrozenInstanceError):
        sample.file_count = 99


# Sabotage-proof (executed): renamed `file_count` to `files` on
# ProposedScope → the field-name list no longer matched on the real
# leg; restored.
def test_proposed_scope_carries_expected_fields(scanner: Any, tmp_path: Path) -> None:
    """The six load-bearing fields callers read are present in name and
    in order — operators paste the renderer output into yaml and the
    catalogue test in `tests/contracts/` reads field-by-field."""
    _seed_agent(tmp_path)
    (scope,) = scanner.scan_for_agents(memory_root=tmp_path)
    field_names = [f.name for f in dataclasses.fields(scope)]
    assert field_names == [
        "name",
        "surfaces",
        "harness",
        "confidence",
        "file_count",
        "most_recent_mtime",
    ]


# Sabotage-proof (executed): changed scan_for_agents to return a list
# (`return list(...)`) → `isinstance(scopes, tuple)` failed on the real
# leg; restored.
def test_scan_for_agents_returns_tuple_of_proposed_scope(scanner: Any, tmp_path: Path) -> None:
    """The function returns ``tuple[ProposedScope, ...]`` — never a list,
    iterator, or None — so callers can rely on length + indexing
    semantics without defensive coercion."""
    _seed_agent(tmp_path)
    scopes = scanner.scan_for_agents(memory_root=tmp_path)
    assert isinstance(scopes, tuple)
    assert scopes, "a seeded agent dir with .md content must yield a proposal"
    for scope in scopes:
        assert isinstance(scope, ProposedScope)


# Sabotage-proof (executed): renamed the `memory_root` kwarg to `root` on
# scan_for_agents → the sig.parameters check failed on the real leg;
# restored.
def test_scan_for_agents_signature_pins_keyword_only_kwargs(scanner: Any) -> None:
    """``scan_for_agents`` accepts ``memory_root`` (required),
    ``workspace_root``, and ``detectors`` as keyword-only — the CLI +
    MCP adapters depend on those parameter names."""
    params = inspect.signature(scanner.scan_for_agents).parameters
    for kw in ("memory_root", "workspace_root", "detectors"):
        assert kw in params
        assert params[kw].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["memory_root"].default is inspect.Parameter.empty


# Sabotage-proof (executed): renamed `discover_single_agent` first
# positional arg to `agent` (was `agent_name`) → the first-name check
# failed on the real leg; restored.
def test_discover_single_agent_signature_pins_positional_name(scanner: Any) -> None:
    """``discover_single_agent`` takes the agent name as the first
    positional argument and ``memory_root`` / ``workspace_root`` /
    ``harness`` / ``detectors`` as keyword-only kwargs — MCP tool
    arguments map onto these names."""
    sig = inspect.signature(scanner.discover_single_agent)
    first_name = next(iter(sig.parameters))
    assert first_name == "agent_name"
    for kw in ("memory_root", "workspace_root", "harness", "detectors"):
        assert kw in sig.parameters
        assert sig.parameters[kw].kind is inspect.Parameter.KEYWORD_ONLY


# Sabotage-proof (executed): made discover_single_agent swallow the
# "nothing detected" branch and return an empty-surfaces scope → the
# real leg's `pytest.raises(ValueError)` did not trip; restored.
def test_discover_single_agent_raises_when_nothing_found(scanner: Any, tmp_path: Path) -> None:
    """``discover_single_agent`` raises ``ValueError`` when no detector
    proposes any surface AND no .md files exist at the expected
    directory — callers MUST get a hard signal so they don't silently
    write an empty-surfaces scope into yaml."""
    with pytest.raises(ValueError, match="nonexistent"):
        scanner.discover_single_agent("nonexistent", memory_root=tmp_path)


# Sabotage-proof (executed): made discover_single_agent return a tuple
# instead of a single ProposedScope → `isinstance(result, ProposedScope)`
# failed on the real leg; restored.
def test_discover_single_agent_returns_single_proposed_scope(scanner: Any, tmp_path: Path) -> None:
    """The function returns one ``ProposedScope`` — not a tuple. Callers
    inject the result directly into the renderer's tuple-of-one branch."""
    _seed_agent(tmp_path)
    result = scanner.discover_single_agent("agent-alpha", memory_root=tmp_path)
    assert isinstance(result, ProposedScope)
    assert result.name == "agent-alpha"
    assert result.file_count >= 1
