"""pytest-bdd test module for eval_generate.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "eval_generate.feature")


@pytest.mark.bdd
@scenario(FEATURE, "QueryGenerator synthesises queries from a document")
def test_query_generator_synthesises_queries() -> None:
    pass


@pytest.mark.bdd
@scenario(FEATURE, "QueryGenerator returns no queries when the backend errors")
def test_query_generator_returns_empty_on_error() -> None:
    pass


@pytest.mark.bdd
@scenario(FEATURE, "SuiteGenerator runs the GPL pipeline against an indexed corpus")
def test_suite_generator_full_pipeline() -> None:
    pass


@pytest.mark.bdd
@scenario(FEATURE, "SuiteGenerator records calibration failure as an error in the result")
def test_suite_generator_calibration_failure() -> None:
    pass


@pytest.mark.bdd
@scenario(FEATURE, "Enrichment re-judges existing cases to produce graded gold_titles")
def test_enrichment_produces_graded_gold_titles() -> None:
    pass


@pytest.mark.bdd
@scenario(FEATURE, "Enrichment skips cases without a query")
def test_enrichment_skips_empty_query_cases() -> None:
    pass
