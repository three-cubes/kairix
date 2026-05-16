"""pytest-bdd test module for soak.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "soak.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Soak passes when the workload is deterministic across repeats")
def test_soak_passes_on_deterministic_workload():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Soak fires the memory_growth gate when RSS climbs past iter-0")
def test_soak_fires_memory_growth_gate():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Soak fires the signature_mismatch gate when the workload drifts")
def test_soak_fires_signature_mismatch_gate():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Soak CLI failure output carries F21 affordance markers")
def test_soak_cli_failure_output_has_affordance_markers():
    """Body populated by @scenario from the .feature file."""
