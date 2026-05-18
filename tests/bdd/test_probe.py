"""pytest-bdd test module for probe.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "probe.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "probe search at low concurrency reports p50/p95/p99 stats")
def test_probe_search_passes_at_low_concurrency():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "probe search fires the bottleneck heuristic when p95 exceeds threshold")
def test_probe_search_fires_bottleneck_on_slow():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "probe search seed determinism reproduces the same sampled queries")
def test_probe_search_seed_is_deterministic():
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "probe burst surfaces queries-per-second buckets")
def test_probe_burst_buckets_qps():
    """Body populated by @scenario from the .feature file."""
