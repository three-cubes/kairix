"""End-to-end integration tests for the setup wizard.

Wires the production ``run_setup`` orchestrator through real templates,
real config writing, and a fake LLM connection probe. ``tmp_path`` is
the destination for both the config file and the document root — no
env-var monkeypatching (F4-clean).

Components that cooperate in each test:
  - ``run_setup`` (orchestrator)
  - ``SetupContext`` (real, non-interactive)
  - ``load_template`` (real, reads the bundled YAML)
  - ``_build_full_config`` / ``_write_config_yaml`` (real)
  - ``WizardDeps.connection_test`` (fake — boundary)

What's covered here that unit + BDD don't catch:
  - The full non-interactive happy-path lands a YAML config with the
    documented top-level keys (``paths``, ``retrieval``, optionally
    ``collections`` and ``graph``).
  - A second invocation against the SAME output path overwrites cleanly
    (re-running setup is idempotent on the destination file).
  - The structured failure shape: a missing document root produces
    ``run_setup -> False`` AND no config file is written.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from kairix.platform.setup.prompts import SetupContext
from kairix.platform.setup.wizard import WizardDeps, run_setup

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Boundary fakes
# ---------------------------------------------------------------------------


class _ProbeRecorder:
    """Records every ``connection_test`` call and returns the canned verdict."""

    def __init__(self, returns: bool) -> None:
        self._returns = returns
        self.calls: list[tuple[str, str, str, str]] = []

    def __call__(self, provider: str, endpoint: str, api_key: str, embed_model: str) -> bool:
        self.calls.append((provider, endpoint, api_key, embed_model))
        return self._returns


@pytest.fixture
def doc_root(tmp_path: Path) -> Iterator[Path]:
    """A populated documents directory under tmp_path."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "intro.md").write_text("# Intro\nHello world.", encoding="utf-8")
    (root / "guide.md").write_text("# Guide\nUseful content here.", encoding="utf-8")
    yield root


@pytest.fixture
def output_path(tmp_path: Path) -> Path:
    return tmp_path / "kairix.config.yaml"


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / ".setup-state.json"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_setup_happy_path_writes_well_formed_config(doc_root: Path, output_path: Path, state_path: Path) -> None:
    """Non-interactive happy path: valid doc root + a probe that returns
    True → ``run_setup`` returns True, the YAML config is on disk, parses
    cleanly, and carries the documented top-level keys.

    Sabotage: if ``_write_config_yaml`` stopped emitting the ``retrieval``
    section (e.g. the template-key wiring regressed), the
    ``"retrieval" in config`` assertion would fail. If the wizard stopped
    writing the file at all on success, ``output_path.exists()`` would
    be False.
    """
    probe = _ProbeRecorder(returns=True)
    ctx = SetupContext(interactive=False, json_mode=False, state_path=state_path)

    success = run_setup(
        output_path=str(output_path),
        ctx=ctx,
        preset="technical",
        document_path=str(doc_root),
        deps=WizardDeps(connection_test=probe),
    )

    assert success is True
    assert output_path.exists()

    config: dict[str, Any] = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    # Documented top-level keys (paths + retrieval are unconditional).
    assert "paths" in config
    assert "retrieval" in config
    # paths.document_root points at the tmp doc dir, not at a default.
    assert config["paths"]["document_root"] == str(doc_root)
    # The injected probe ran exactly once.
    assert len(probe.calls) == 1


def test_setup_rerun_against_same_output_overwrites_cleanly(
    doc_root: Path, output_path: Path, state_path: Path
) -> None:
    """Two successive setup runs against the same output path produce a
    single, well-formed config file on disk. Re-running setup is a
    common operator move (changing presets, switching doc roots); the
    second run must not corrupt the file or fail.

    Sabotage: if ``_write_config_yaml`` opened the file in append mode
    instead of overwrite, the second run would produce a non-YAML blob
    (two concatenated documents with shared keys) and ``yaml.safe_load``
    would either fail or return a non-dict.
    """
    probe = _ProbeRecorder(returns=True)
    ctx = SetupContext(interactive=False, json_mode=False, state_path=state_path)

    run_setup(
        output_path=str(output_path),
        ctx=ctx,
        preset="general",
        document_path=str(doc_root),
        deps=WizardDeps(connection_test=probe),
    )
    first_size = output_path.stat().st_size

    success_2 = run_setup(
        output_path=str(output_path),
        ctx=ctx,
        preset="technical",
        document_path=str(doc_root),
        deps=WizardDeps(connection_test=probe),
    )

    assert success_2 is True
    # File is still a single YAML mapping (not appended).
    config: dict[str, Any] = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    assert "paths" in config
    # And it wasn't doubled in size by accidental append.
    assert output_path.stat().st_size <= first_size * 2


def test_setup_rejects_missing_document_root_and_writes_no_config(
    tmp_path: Path, output_path: Path, state_path: Path
) -> None:
    """Structured-failure shape: a document_path that doesn't exist
    causes ``run_setup`` to return False AND skip the config write. The
    operator must fix the path before a config is persisted.

    Sabotage: if ``_resolve_document_root`` started defaulting to
    ``Path.home() / "Documents"`` on missing-dir input (instead of
    returning None), the wizard would continue, write the config, and
    this test's ``output_path.exists()`` assertion would fail.
    """
    probe = _ProbeRecorder(returns=True)
    ctx = SetupContext(interactive=False, json_mode=False, state_path=state_path)
    missing = tmp_path / "this-dir-does-not-exist"

    success = run_setup(
        output_path=str(output_path),
        ctx=ctx,
        preset="general",
        document_path=str(missing),
        deps=WizardDeps(connection_test=probe),
    )

    assert success is False
    assert not output_path.exists(), "Wizard must not persist a config when doc_root is invalid"
