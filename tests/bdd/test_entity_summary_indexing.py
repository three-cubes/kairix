"""pytest-bdd test module for entity_summary_indexing.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "entity_summary_indexing.feature")

pytestmark = pytest.mark.bdd


@scenario(
    FEATURE,
    "Description-keyword query surfaces an enriched entity",
)
def test_description_keyword_query_surfaces_entity() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(
    FEATURE,
    "A first-party entity with no Wikidata id still surfaces by its description",
)
def test_first_party_entity_without_qid_surfaces_by_description() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(
    FEATURE,
    "Description-keyword query returns no entity row when the flag is off",
)
def test_description_keyword_query_returns_no_entity_when_flag_off() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(
    FEATURE,
    "Operator sees a Wikidata badge on entity rows in CLI output",
)
def test_operator_sees_wikidata_badge_on_entity_rows() -> None:
    """Body populated by @scenario from the .feature file."""


def test_scenario_search_pipeline_never_wires_the_network_bound_reranker(tmp_path: Path) -> None:
    """Regression (#493): the scenarios' search must stay hermetic.

    The flake root cause was the step's default-``deps`` factory build wiring
    the production cross-encoder reranker, whose first call imports torch and
    fetches the model from the Hugging Face hub (a real network call, ~30s
    cold) inside the 30s per-test timeout. The pipeline the ``When`` step
    searches through must carry no reranker, so no scenario can reach it.
    """
    import sqlite3

    from kairix.core.db.schema import create_schema
    from tests.bdd.steps.entity_summary_indexing_steps import (
        _Ctx,
        build_entity_summary_search_pipeline,
    )

    document_root = tmp_path / "vault"
    document_root.mkdir()
    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    db.close()
    ctx = _Ctx(tmp_path=tmp_path, db_path=db_path, document_root=document_root)

    pipeline = build_entity_summary_search_pipeline(ctx)

    assert pipeline.reranker is None, (
        "the entity-summary scenarios' pipeline wired the production cross-encoder "
        f"reranker ({pipeline.reranker!r}); its first call downloads a model over the network"
    )
