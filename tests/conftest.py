"""
Shared pytest fixtures for the kairix test suite.

Fixture hierarchy:
  no_azure_calls (autouse, all non-e2e tests) — blocks accidental Azure API calls
  fake_llm_backend — FakeLLM satisfying LLMBackend Protocol
  neo4j_client — FakeNeo4jClient satisfying Neo4jClient interface
  search_db / seeded_search_db — BM25 search index fixtures

BDD step modules must be declared as pytest_plugins at the root conftest level
(pytest restriction: pytest_plugins in sub-conftest files is not supported).
"""

import pytest

# BDD step definition modules — registered here so pytest-bdd can discover them
# across the entire test run.
pytest_plugins = [
    "tests.bdd.steps.search_steps",
    "tests.bdd.steps.curator_steps",
    "tests.bdd.steps.reflib_steps",
    "tests.bdd.steps.normalisation_steps",
    "tests.bdd.steps.entity_steps",
    "tests.bdd.steps.onboard_steps",
    "tests.bdd.steps.mcp_timeline_steps",
    "tests.bdd.steps.eval_tune_steps",
    "tests.bdd.steps.mcp_entity_steps",
    "tests.bdd.steps.eval_auto_gold_steps",
    "tests.bdd.steps.recall_steps",
    "tests.bdd.steps.benchmark_steps",
    "tests.bdd.steps.mcp_search_steps",
    "tests.bdd.steps.mcp_prep_steps",
    "tests.bdd.steps.timeline_absolute_steps",
    "tests.bdd.steps.mcp_contradict_steps",
    "tests.bdd.steps.chunk_date_steps",
    "tests.bdd.steps.research_synthesis_steps",
    "tests.bdd.steps.search_dedup_steps",
    "tests.bdd.steps.agent_collections_steps",
    "tests.bdd.steps.eval_gate_steps",
    "tests.bdd.steps.configurable_default_scope_steps",
    "tests.bdd.steps.wikilinks_injection_steps",
    "tests.bdd.steps.eval_judge_steps",
    "tests.bdd.steps.eval_generate_steps",
    "tests.bdd.steps.eval_gold_builder_steps",
    "tests.bdd.steps.eval_monitor_steps",
    "tests.bdd.steps.embed_run_steps",
]

from tests.fixtures.embeddings import fake_embedding  # noqa: E402
from tests.fixtures.neo4j_mock import FakeNeo4jClient  # noqa: E402


@pytest.fixture(autouse=True)
def no_azure_calls(monkeypatch, request):
    """Block accidental Azure API calls in all tests except those marked e2e.

    Sets KAIRIX_EMBED_BACKEND=fake so any code that reads this env var
    will use the fake backend. Tests marked @pytest.mark.e2e must set
    KAIRIX_E2E=1 in the environment to confirm intent.
    """
    if "e2e" not in request.keywords:
        monkeypatch.setenv("KAIRIX_EMBED_BACKEND", "fake")
        monkeypatch.delenv("KAIRIX_AZURE_API_KEY", raising=False)
        monkeypatch.delenv("KAIRIX_LLM_API_KEY", raising=False)


@pytest.fixture
def neo4j_client():
    """FakeNeo4jClient with default test entities. No real Neo4j connection."""
    return FakeNeo4jClient()


@pytest.fixture
def neo4j_client_empty():
    """FakeNeo4jClient with no entities."""
    return FakeNeo4jClient(entities=[])


@pytest.fixture
def fake_llm_backend():
    """Fake LLMBackend satisfying the Protocol. No Azure calls."""
    import struct

    class FakeLLM:
        def chat(self, messages: list, max_tokens: int = 800) -> str:
            return "fake response"

        def embed(self, text: str) -> list[float]:
            return fake_embedding(seed=hash(text) % 1000)

        def embed_as_bytes(self, text: str) -> bytes | None:
            vec = self.embed(text)
            return struct.pack(f"{len(vec)}f", *vec)

        def dimension(self) -> int:
            return 1536

    return FakeLLM()
