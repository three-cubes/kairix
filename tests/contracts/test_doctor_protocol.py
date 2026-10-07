"""Protocol-shape contract for :mod:`kairix.agents.onboarding.doctor`
(PR 1.5 / #420).

Pins the structural promises of :class:`SurfaceHealth`,
:class:`AgentHealth`, :class:`DoctorReport`, :func:`doctor_check_all`,
and :func:`doctor_check_agent`. The ``kairix doctor agent`` CLI + the
``tool_doctor_*`` MCP tools both consume these shapes; this contract
freezes them so the public surface cannot regress silently.

Every body runs over BOTH the production doctor functions and the
canonical :class:`tests.fakes.FakeDoctor` (F43 behavioural parity): the
dataclass-shape assertions are made on the objects each implementation
actually returns for the same config, so a fake that drifted to a
different (mutable / reshaped) result type fails the same body.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kairix.agents.onboarding.doctor import (
    AgentHealth,
    DoctorReport,
    SurfaceHealth,
    doctor_check_agent,
    doctor_check_all,
)
from tests.fakes import FakeDoctor

pytestmark = pytest.mark.contract


def _real_doctor() -> Any:
    return SimpleNamespace(check_all=doctor_check_all, check_agent=doctor_check_agent)


_IMPLEMENTATIONS: list[tuple[str, Callable[[], Any]]] = [
    ("real", _real_doctor),
    ("fake", FakeDoctor),
]


def _config(tmp_path: Path) -> dict[str, object]:
    """One agent with one populated memory surface under ``tmp_path``."""
    surface = tmp_path / "memory" / "agent-alpha"
    surface.mkdir(parents=True)
    (surface / "note-0.md").write_text("# note 0\n", encoding="utf-8")
    return {
        "agents": {
            "agent-alpha": {
                "harness": "claude-code",
                "surfaces": [{"path": str(surface), "glob": "**/*.md", "label": "memory"}],
            },
        },
    }


def _surface(doctor: Any, tmp_path: Path) -> Any:
    health = doctor.check_agent("agent-alpha", config=_config(tmp_path))
    assert health.surfaces, "a configured surface must be reported"
    return health.surfaces[0]


# Sabotage-proof (executed): removed `frozen=True` from the @dataclass
# decorator on SurfaceHealth → the FrozenInstanceError block did not
# trip when mutating `.exists` (both cases); restored.
@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_surface_health_is_frozen_dataclass(name: str, factory: Callable[[], Any], tmp_path: Path) -> None:
    """``SurfaceHealth`` is a frozen dataclass — callers cache and
    compare doctor outcomes across runs and rely on immutability."""
    sample = _surface(factory(), tmp_path)
    assert isinstance(sample, SurfaceHealth), name
    assert dataclasses.is_dataclass(sample)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sample.exists = False  # type: ignore[misc]  # mutating frozen dc is the sabotage


# Sabotage-proof (executed): renamed `file_count` to `files` on
# SurfaceHealth → field-name set assertion failed; restored.
@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_surface_health_carries_expected_fields(name: str, factory: Callable[[], Any], tmp_path: Path) -> None:
    """The load-bearing fields callers read are present in name and in
    order — operators see them per-surface in the validation report.
    ``writable`` (PLA-259) carries the write-access probe so an unwritable
    surface surfaces in the same envelope as the disk-state fields."""
    sample = _surface(factory(), tmp_path)
    field_names = [f.name for f in dataclasses.fields(sample)]
    assert field_names == [
        "path",
        "label",
        "exists",
        "file_count",
        "most_recent_mtime",
        "issues",
        "writable",
    ], name
    assert sample.label == "memory", name
    assert sample.exists is True, name


# Sabotage-proof (executed): dropped `frozen=True` from AgentHealth →
# the FrozenInstanceError block failed; restored.
@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_agent_health_is_frozen_dataclass(name: str, factory: Callable[[], Any], tmp_path: Path) -> None:
    """``AgentHealth`` is a frozen dataclass — same caching + diff
    semantics as :class:`SurfaceHealth`."""
    sample = factory().check_agent("agent-alpha", config=_config(tmp_path))
    assert isinstance(sample, AgentHealth), name
    assert dataclasses.is_dataclass(sample)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sample.overall = "error"  # type: ignore[misc]  # mutating frozen dc is the sabotage


# Sabotage-proof (executed): reordered fields on AgentHealth → field-
# name list assertion failed; restored.
@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_agent_health_carries_expected_fields(name: str, factory: Callable[[], Any], tmp_path: Path) -> None:
    """``AgentHealth`` exposes name, harness, surfaces, overall, issues
    in declared order — CLI + MCP envelopes index by name."""
    sample = factory().check_agent("agent-alpha", config=_config(tmp_path))
    field_names = [f.name for f in dataclasses.fields(sample)]
    assert field_names == ["name", "harness", "surfaces", "overall", "issues"], name
    assert (sample.name, sample.harness) == ("agent-alpha", "claude-code"), name


# Sabotage-proof (executed): dropped `frozen=True` from DoctorReport →
# the FrozenInstanceError block failed; restored.
@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_doctor_report_is_frozen_dataclass(name: str, factory: Callable[[], Any], tmp_path: Path) -> None:
    """``DoctorReport`` is a frozen dataclass — bulk outcomes survive
    round-trips through envelopes."""
    sample = factory().check_all(config=_config(tmp_path))
    assert dataclasses.is_dataclass(sample)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sample.overall = "error"  # type: ignore[misc]  # mutating frozen dc is the sabotage


# Sabotage-proof (executed): renamed `summary_text` to `summary` on
# DoctorReport → field name assertion failed; restored.
@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_doctor_report_carries_expected_fields(name: str, factory: Callable[[], Any], tmp_path: Path) -> None:
    """``DoctorReport`` exposes agents, overall, summary_text — the
    operator-facing one-paragraph summary travels in summary_text."""
    sample = factory().check_all(config=_config(tmp_path))
    field_names = [f.name for f in dataclasses.fields(sample)]
    assert field_names == ["agents", "overall", "summary_text"], name
    assert [a.name for a in sample.agents] == ["agent-alpha"], name
    assert sample.summary_text, name


# Sabotage-proof (executed): changed doctor_check_all to return a
# list → isinstance assertion failed; restored.
@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_doctor_check_all_returns_doctor_report(name: str, factory: Callable[[], Any]) -> None:
    """The function returns ``DoctorReport`` — never a list, iterator,
    or None — so callers can rely on the dataclass surface. An empty
    config is an empty, ``ok`` report."""
    report = factory().check_all(config={})
    assert isinstance(report, DoctorReport), name
    assert report.agents == (), name
    assert report.overall == "ok", name


# Sabotage-proof (executed): renamed `config` kwarg to `cfg` on
# doctor_check_all → keyword bind assertion failed; restored.
@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_doctor_check_all_signature_pins_keyword_only_kwargs(name: str, factory: Callable[[], Any]) -> None:
    """``check_all`` accepts ``config`` as a keyword-only kwarg — the CLI
    + MCP adapters depend on the parameter name."""
    sig = inspect.signature(factory().check_all)
    assert "config" in sig.parameters, name
    assert sig.parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY, name


# Sabotage-proof (executed): made doctor_check_agent re-raise unknown-
# agent errors → the no-raise assertion failed because ValueError
# escaped; restored to the swallow-into-AgentHealth path.
@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_doctor_check_agent_never_raises_on_unknown(name: str, factory: Callable[[], Any]) -> None:
    """``check_agent`` returns an :class:`AgentHealth` even when the
    agent is unknown — callers branch on ``overall`` / ``issues``
    rather than catching exceptions."""
    health = factory().check_agent("ghost", config={})
    assert isinstance(health, AgentHealth), name
    assert health.name == "ghost", name
    assert health.overall == "error", name
    assert health.issues, f"{name}: unknown agent must carry an actionable issue"


# Sabotage-proof (executed): renamed the first positional param from
# `agent_name` to `name` → the inspect assertion failed; restored.
@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_doctor_check_agent_signature_pins_positional_name(name: str, factory: Callable[[], Any]) -> None:
    """``check_agent`` takes the agent name as the first positional
    argument and ``config`` as keyword-only — MCP tool arguments map
    onto these names."""
    sig = inspect.signature(factory().check_agent)
    first_name = next(iter(sig.parameters))
    assert first_name == "agent_name", name
    assert "config" in sig.parameters, name
