"""Contract for exercising CI workflow changes through the Python quality gate."""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _change_patterns(output: str) -> list[str]:
    filter_step = next(step for step in _workflow()["jobs"]["changes"]["steps"] if step.get("id") == "filter")
    filters = yaml.safe_load(filter_step["with"]["filters"])
    return filters[output]


def _python_change_patterns() -> list[str]:
    return _change_patterns("python")


def test_ci_workflow_changes_run_the_python_quality_gate() -> None:
    """The reusable caller must execute when its own contract changes."""
    assert ".github/workflows/ci.yml" in _python_change_patterns()


@pytest.mark.parametrize("output", ["python", "e2e"])
def test_lockfile_only_changes_run_the_python_and_e2e_stages(output: str) -> None:
    """A uv.lock-only dependency bump changes what every tier runs against, so
    it must trigger the Python stages and the composed E2E stage — like
    pyproject.toml does. Sabotage: drop 'uv.lock' from either filter → fails."""
    assert "uv.lock" in _change_patterns(output)


@pytest.mark.parametrize("job", ["unit-and-type", "unit-py312", "integration", "e2e-composed-path"])
def test_test_stages_install_the_committed_lock(job: str) -> None:
    """The test-producing stages install uv.lock with --locked, not a fresh pip
    resolution of pyproject.toml — otherwise a lockfile PR's suite runs against
    versions other than the ones it changes. Sabotage: revert a job to
    ``pip install -e ".[...]"`` (or drop --locked) → fails."""
    steps = _workflow()["jobs"][job]["steps"]
    uv_steps = [st for st in steps if "setup-uv-cached" in str(st.get("uses", ""))]
    assert uv_steps, f"{job}: no setup-uv-cached step"
    assert "--locked" in uv_steps[0]["with"]["sync-args"], f"{job}: sync must enforce the lock"
    assert not any("pip install -e" in str(st.get("run", "")) for st in steps), f"{job}: re-resolves via pip"
