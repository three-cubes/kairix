"""pytest-bdd test module for embed_run.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "embed_run.feature")


@pytest.mark.bdd
@scenario(FEATURE, "A clean run reports embedded count and zero failures")
def test_clean_run_reports_embedded_count() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "An empty corpus returns embedded zero without calling the backend")
def test_empty_corpus_skips_backend() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "A backend that raises records every chunk in the batch as failed")
def test_raising_backend_records_failures() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "Partial-response from the backend reports honest embedded and failed counts")
def test_partial_response_honest_counts() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "force=True clears existing vectors before re-embedding")
def test_force_clears_existing_vectors() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "limit caps the number of chunks processed below total available")
def test_limit_caps_chunks() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario(FEATURE, "A dimension mismatch from preflight aborts the run with SchemaVersionError")
def test_dim_mismatch_raises() -> None:
    """Body populated by @scenario from the .feature file."""
