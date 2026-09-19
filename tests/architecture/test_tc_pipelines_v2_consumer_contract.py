"""Public caller contract for the tc-pipelines v2 migration.

The reusable workflows are separate repositories, so GitHub validates their
``workflow_call`` interface only after it schedules a caller.  Keep the
consumer-side shape explicit and executable locally: every shared pipeline
reference must use the reviewed immutable v2.2.0 carrier, the removed
``ci-requirements-path`` input must not be passed, and the two load-bearing
callers must retain their quality-gate and auto-merge semantics.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINES_V2_SHA = "07dad612b5f409a975c8356d556b07f3a855597c"  # pragma: allowlist secret -- public GitHub commit pin
FITNESS_VERSION = "v0.16.1"


def _workflow(name: str) -> dict[str, object]:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _tc_pipelines_uses() -> list[str]:
    return [
        line.strip().split("uses: ", 1)[1].split(" #", 1)[0]
        for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml")
        for line in path.read_text(encoding="utf-8").splitlines()
        if "uses: three-cubes/tc-pipelines/" in line
    ]


def test_all_shared_pipeline_references_use_the_v2_2_0_carrier() -> None:
    """Every consumer reference is immutable and comes from one reviewed release."""
    uses = _tc_pipelines_uses()
    assert uses
    assert all(reference.endswith(f"@{PIPELINES_V2_SHA}") for reference in uses)


def test_quality_gate_caller_uses_supported_v2_inputs_and_preserves_gate_behaviour() -> None:
    """The caller cannot retain an input removed from the reusable's v2 contract."""
    arch_fitness = _workflow("ci.yml")["jobs"]["arch-fitness"]
    assert arch_fitness["uses"].endswith(f"@{PIPELINES_V2_SHA}")
    assert "ci-requirements-path" not in arch_fitness["with"]
    assert arch_fitness["with"]["run-no-attribution"] is True
    assert arch_fitness["with"]["fetch-depth"] == 0
    assert arch_fitness["with"]["upload-coverage-artifact"] is False


def test_auto_merge_caller_preserves_the_quality_gate_fan_in_contract() -> None:
    """v2 still arms only on the required Quality gate fan-in result."""
    merge = _workflow("auto-merge.yml")["jobs"]["merge"]
    assert merge["uses"].endswith(f"@{PIPELINES_V2_SHA}")
    assert merge["with"]["fan-in-check-name"] == "Quality gate"
    assert merge["with"]["merge-method"] == "merge"
    assert merge["with"]["head-sha"] == "${{ github.event.workflow_run.head_sha }}"
    assert merge["permissions"]["id-token"] == "write"
    assert merge["permissions"]["pull-requests"] == "write"


def test_fitness_engine_is_pinned_to_the_v2_compatible_release() -> None:
    """The local gate and v2 reusable resolve the same current shared engine."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lockfile = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert f"tc-fitness.git@{FITNESS_VERSION}" in pyproject
    assert f"tc-fitness.git?rev={FITNESS_VERSION}" in lockfile
