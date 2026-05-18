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

# Early numpy import — pre-loads the C extension before pytest-cov starts
# instrumenting test modules. Python 3.14 + numpy 2.4 + pytest-cov hit a
# "cannot load module more than once per process" ImportError when numpy
# is first imported AFTER coverage tracing has begun (#211). Loading it
# here ensures numpy is in ``sys.modules`` before the first test module
# loads, so subsequent ``import numpy`` calls are pure dict lookups.
import numpy  # noqa: F401 — pre-load only; see #211
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
    "tests.bdd.steps.search_logging_steps",
    "tests.bdd.steps.search_backends_steps",
    "tests.bdd.steps.search_boosts_steps",
    "tests.bdd.steps.search_config_validation_steps",
    "tests.bdd.steps.search_planner_steps",
    "tests.bdd.steps.search_rerank_steps",
    "tests.bdd.steps.search_intent_gated_boosts_steps",
    "tests.bdd.steps.search_chunk_date_recency_steps",
    "tests.bdd.steps.search_collection_retrieval_overrides_steps",
    "tests.bdd.steps.search_cli_steps",
    "tests.bdd.steps.summarise_cli_steps",
    "tests.bdd.steps.kairix_cli_top_level_steps",
    "tests.bdd.steps.store_cli_steps",
    "tests.bdd.steps.brief_cli_steps",
    "tests.bdd.steps.setup_cli_steps",
    "tests.bdd.steps.wikilinks_cli_steps",
    "tests.bdd.steps.entity_cli_steps",
    "tests.bdd.steps.entity_audit_steps",
    "tests.bdd.steps.curator_cli_steps",
    "tests.bdd.steps.mcp_cli_steps",
    "tests.bdd.steps.embed_cli_steps",
    "tests.bdd.steps.timeline_cli_steps",
    "tests.bdd.steps.soak_steps",
    "tests.bdd.steps.warm_steps",
    "tests.bdd.steps.probe_steps",
    "tests.bdd.steps.probe_per_query_telemetry_steps",
    "tests.bdd.steps.worker_steps",
    "tests.bdd.steps.bootstrap_steps",
    "tests.bdd.steps.usage_guide_steps",
    "tests.bdd.steps.classify_steps",
    "tests.bdd.steps.classify_error_steps",
    "tests.bdd.steps.embed_pool_config_steps",
    "tests.bdd.steps.query_cache_steps",
    "tests.bdd.steps.enrich_cache_steps",
    "tests.bdd.steps.embed_cache_steps",
    "tests.bdd.steps.embed_coalescer_steps",
    "tests.bdd.steps.vec_index_batched_metadata_steps",
    "tests.bdd.steps.transport_pool_steps",
    # transport_bdd_steps covers all four transport_(cache|coalesce|retry|timeout)
    # features in one module — shared step phrases would otherwise be
    # registered ambiguously across separate per-feature modules.
    "tests.bdd.steps.transport_bdd_steps",
    # Provider plugin BDD step modules. Five Wave-4 providers carry
    # skeleton skips until their implementations land.
    "tests.bdd.steps.provider_anthropic_steps",
    "tests.bdd.steps.provider_azure_foundry_steps",
    "tests.bdd.steps.provider_azure_legacy_steps",
    "tests.bdd.steps.provider_bedrock_steps",
    "tests.bdd.steps.provider_litellm_proxy_steps",
    "tests.bdd.steps.provider_ollama_steps",
    "tests.bdd.steps.provider_openai_steps",
    "tests.bdd.steps.provider_wire_common_steps",
    # E2E provider journey step modules.
    "tests.bdd.steps.e2e_provider_chat_steps",
    "tests.bdd.steps.e2e_provider_embed_steps",
    "tests.bdd.steps.e2e_provider_health_steps",
    "tests.bdd.steps.e2e_provider_switch_steps",
    # probe-config health-check end-user CLI.
    "tests.bdd.steps.probe_config_health_steps",
    # Layered config loader — image-bundled base + sparse operator overlay.
    "tests.bdd.steps.config_layering_steps",
]

# PVT placeholder steps — catch-all ``pytest.skip`` until #284 harness ships.
# Gated on ``KAIRIX_PVT=1`` so the regex-catch-all parser doesn't intercept
# every Given/When/Then across the layer-2 BDD suite when PVT is off (the
# default). The tests/pvt/conftest.py autoskip is the primary defence — it
# skips PVT-marked items at collection time; this catch-all is the secondary
# defence that the PVT brief reserves for the ``KAIRIX_PVT=1`` mode where
# the autoskip is intentionally bypassed.
import os as _os  # noqa: E402 — keep pytest_plugins assembly above other imports

if _os.environ.get("KAIRIX_PVT") == "1":
    pytest_plugins.append("tests.pvt.steps.pvt_placeholder_steps")

from tests.fixtures.embeddings import fake_embedding  # noqa: E402
from tests.fixtures.neo4j_mock import FakeNeo4jClient  # noqa: E402


@pytest.fixture(autouse=True)
def no_azure_calls(monkeypatch, request):
    """Block accidental Azure API calls in all tests except those marked e2e.

    The ``delenv`` calls are the load-bearing protection — they remove
    real operator credentials (``KAIRIX_AZURE_API_KEY`` /
    ``KAIRIX_LLM_API_KEY``) from the per-test env so a test that hits a
    code path through ``kairix.secrets`` doesn't accidentally use the
    developer's Azure account. Tests marked ``@pytest.mark.e2e``
    bypass this and must set ``KAIRIX_E2E=1`` to confirm intent.

    This fixture is the reason ``tests/conftest.py`` stays baselined for
    F2 — the ``delenv`` operation is a deliberate safety net at the env
    boundary, not a test-shaping hack. F2 stays baselined here on
    purpose; promoting the fixture out of monkeypatch would lose the
    per-test isolation that prevents env leak between tests.
    """
    if "e2e" not in request.keywords:
        monkeypatch.delenv("KAIRIX_AZURE_API_KEY", raising=False)
        monkeypatch.delenv("KAIRIX_LLM_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _reset_embed_coalescer():
    """Drop the process-shared embed coalescer between tests (#288).

    The coalescer singleton owns a background dispatcher thread — if a
    test triggers construction (via ``embed_text`` without a ``client=``
    kwarg) the thread would survive into the next test and the next
    test's batch dispatcher closure would be stale. Resetting on teardown
    keeps each test's coalescer state isolated.
    """
    yield
    from kairix.transport.coalesce import reset_embed_coalescer

    reset_embed_coalescer()


@pytest.fixture(autouse=True)
def _reset_client_pool():
    """Drop the process-shared transport client between tests.

    The :mod:`kairix.transport.pool` singleton caches the built
    OpenAI-compatible client process-wide so coalescer batches reuse
    one ``httpx.Client`` connection pool. Tests that exercise that
    path through the production accessor (``_get_client``) would
    otherwise inherit a client from a previous test — including one
    built against now-deleted Azure credentials. Resetting on
    teardown keeps each test's pool state isolated, matching the
    pattern established by ``_reset_embed_coalescer``.
    """
    yield
    from kairix.transport.pool import reset_client_cache

    reset_client_cache()


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
    import hashlib
    import struct

    class FakeLLM:
        def chat(self, messages: list, max_tokens: int = 800) -> str:
            return "fake response"

        def embed(self, text: str) -> list[float]:
            # SHA-256 truncated to 32 bits — deterministic across runs (PYTHONHASHSEED
            # randomises hash()) and the 2^32 seed space makes collisions vanish (#240).
            seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")
            return fake_embedding(seed=seed)

        def embed_as_bytes(self, text: str) -> bytes | None:
            vec = self.embed(text)
            return struct.pack(f"{len(vec)}f", *vec)

        def dimension(self) -> int:
            return 1536

    return FakeLLM()
