"""pytest-bdd test module for benchmark_run.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "benchmark_run.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Benchmark produces category scores and gates")
def test_benchmark_produces_scores_and_gates():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Perfect mock scores pass all gates")
def test_perfect_mock_scores_pass_all_gates():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Zero-match suite fails phase1 gate")
def test_zero_match_suite_fails_phase1_gate():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "NDCG cases produce hit rate and MRR metrics")
def test_ndcg_cases_produce_metrics():
    """Body populated by @scenario from the .feature file."""
