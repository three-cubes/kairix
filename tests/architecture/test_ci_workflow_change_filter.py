"""Contract for exercising CI workflow changes through the Python quality gate."""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _python_change_patterns() -> list[str]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    filter_step = next(step for step in workflow["jobs"]["changes"]["steps"] if step.get("id") == "filter")
    filters = yaml.safe_load(filter_step["with"]["filters"])
    return filters["python"]


def test_ci_workflow_changes_run_the_python_quality_gate() -> None:
    """The reusable caller must execute when its own contract changes."""
    assert ".github/workflows/ci.yml" in _python_change_patterns()
