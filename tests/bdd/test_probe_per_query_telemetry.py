"""pytest-bdd test module for probe_per_query_telemetry.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "probe_per_query_telemetry.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Per-query stage records appear in the probe envelope")
def test_per_query_stage_records_in_envelope() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Vector stage is split into embed_http and vector_ann")
def test_vector_stage_split_into_embed_http_and_vector_ann() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Slow queries surface in per-query records")
def test_slow_queries_surface_in_per_query_records() -> None:
    """Body populated by @scenario from the .feature file."""
