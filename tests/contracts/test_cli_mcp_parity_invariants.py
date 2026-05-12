"""Phase 4 of #168 — cross-cutting parity invariants.

Every kairix feature exposed via both CLI and MCP must satisfy the
shape rules the per-feature parity tests already check individually.
This sweep catches regressions if a future use case is added without
the corresponding contract test, or if a use case stops following the
established pattern (returning a frozen dataclass, exposing a
``run_<op>`` callable, having an envelope projector helper).

The list below is the canonical set of use-case modules. Adding a new
use case should mean adding it here AND adding a per-feature parity
test in this directory.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import is_dataclass

import pytest

# Every use-case module + its (run_callable, output_class, envelope_helper) triple.
# When a Phase-3+ surface is added, append it here so the invariant sweep covers it.
_USE_CASES: list[tuple[str, str, str, str]] = [
    ("kairix.use_cases.timeline", "run_timeline", "TimelineResult", None),  # type: ignore[list-item]  # legacy: timeline result projection inlined in MCP adapter
    ("kairix.use_cases.search", "run_search", "SearchOutput", "search_output_to_envelope"),
    ("kairix.use_cases.contradict", "run_contradict", "ContradictOutput", "contradict_output_to_envelope"),
    ("kairix.use_cases.brief", "run_brief", "BriefOutput", "brief_output_to_envelope"),
    ("kairix.use_cases.entity", "run_entity_suggest", "EntitySuggestOutput", "entity_suggest_output_to_envelope"),
    ("kairix.use_cases.entity", "run_entity_validate", "EntityValidateOutput", "entity_validate_output_to_envelope"),
    ("kairix.use_cases.prep", "run_prep", "PrepOutput", "prep_output_to_envelope"),
    ("kairix.use_cases.research", "run_research_use_case", "ResearchOutput", "research_output_to_envelope"),
    ("kairix.use_cases.entity_get", "run_entity_get", "EntityGetOutput", "entity_get_output_to_envelope"),
    ("kairix.use_cases.usage_guide", "run_usage_guide", "UsageGuideOutput", "usage_guide_output_to_envelope"),
]


@pytest.mark.contract
@pytest.mark.parametrize("module_path,run_name,_output_name,_envelope_name", _USE_CASES)
def test_each_use_case_exposes_a_run_callable(
    module_path: str, run_name: str, _output_name: str, _envelope_name: str | None
) -> None:
    mod = importlib.import_module(module_path)
    fn = getattr(mod, run_name, None)
    assert callable(fn), f"{module_path}.{run_name} missing"
    # The use case must take a deps kwarg so adapters can inject test deps.
    sig = inspect.signature(fn)
    assert "deps" in sig.parameters, f"{module_path}.{run_name} must accept deps for testability"


@pytest.mark.contract
@pytest.mark.parametrize("module_path,_run_name,output_name,_envelope_name", _USE_CASES)
def test_each_use_case_returns_frozen_dataclass(
    module_path: str, _run_name: str, output_name: str, _envelope_name: str | None
) -> None:
    mod = importlib.import_module(module_path)
    cls = getattr(mod, output_name, None)
    assert cls is not None, f"{module_path}.{output_name} missing"
    assert is_dataclass(cls), f"{module_path}.{output_name} must be a dataclass"
    # frozen=True: dataclass __setattr__ is FrozenInstanceError; a sentinel attribute confirms.
    params = getattr(cls, "__dataclass_params__", None)
    assert params is not None and params.frozen, f"{module_path}.{output_name} must be frozen"


@pytest.mark.contract
@pytest.mark.parametrize("module_path,_run_name,_output_name,envelope_name", _USE_CASES)
def test_each_use_case_has_envelope_projector(
    module_path: str, _run_name: str, _output_name: str, envelope_name: str | None
) -> None:
    """Phase-2+ use cases share their envelope shape via a public projector
    function; the timeline use case (Phase 1) inlined the projection in its
    MCP adapter and is grandfathered.
    """
    if envelope_name is None:
        pytest.skip("Timeline use case (Phase 1) inlined the projection — grandfathered")
    mod = importlib.import_module(module_path)
    fn = getattr(mod, envelope_name, None)
    assert callable(fn), f"{module_path}.{envelope_name} missing"


@pytest.mark.contract
def test_use_case_dataclass_has_error_field() -> None:
    """Every use-case Output dataclass must include an ``error`` field —
    the never-raise contract requires callers to read errors from the result.
    """
    for module_path, _, output_name, _ in _USE_CASES:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, output_name)
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        assert "error" in fields, f"{module_path}.{output_name} missing 'error' field"


@pytest.mark.contract
def test_every_use_case_has_a_per_feature_parity_test() -> None:
    """Phase 4 sweep: every use case is paired with a contract test in
    this directory. Adds a guard against silently introducing new use
    cases without the matching parity test.
    """
    from pathlib import Path

    contracts_dir = Path(__file__).parent
    parity_tests = {p.stem for p in contracts_dir.glob("test_cli_mcp_parity_*.py")}

    expected_tests = {
        "test_cli_mcp_parity_timeline",
        "test_cli_mcp_parity_search",
        "test_cli_mcp_parity_contradict",
        "test_cli_mcp_parity_brief",
        "test_cli_mcp_parity_entity",
        "test_cli_mcp_parity_prep",
        "test_cli_mcp_parity_research",
        "test_cli_mcp_parity_entity_get",
        "test_cli_mcp_parity_usage_guide",
        "test_cli_mcp_parity_invariants",  # this file
    }
    missing = expected_tests - parity_tests
    assert not missing, f"missing per-feature parity tests: {missing}"
