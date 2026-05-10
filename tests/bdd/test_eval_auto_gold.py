"""pytest-bdd test module for eval_auto_gold.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "eval_auto_gold.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Corpus profile counts document types correctly")
def test_corpus_profile_counts():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Empty corpus returns zero counts")
def test_empty_corpus():
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Generated queries are proportioned by corpus type")
def test_generated_queries_proportioned():
    """Body populated by @scenario from the .feature file."""
