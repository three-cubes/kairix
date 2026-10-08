"""
Shared pytest fixtures for the kairix test suite.

Fixture hierarchy:
  _hermetic_data_dirs (session, autouse) — points HOME / XDG_* / KAIRIX_DOCUMENT_ROOT
    at clean session temp dirs so no test reads the dev's real dogfood data (PLA-285)
  no_azure_calls (autouse, all non-e2e tests) — blocks accidental Azure API calls
  _hermetic_user_config (autouse) — hides the developer's real ~/.config/kairix
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
    "tests.bdd.steps.onboard_scan_steps",
    "tests.bdd.steps.doctor_steps",
    "tests.bdd.steps.mcp_timeline_steps",
    "tests.bdd.steps.eval_tune_steps",
    "tests.bdd.steps.mcp_entity_steps",
    "tests.bdd.steps.eval_auto_gold_steps",
    "tests.bdd.steps.recall_steps",
    "tests.bdd.steps.benchmark_steps",
    "tests.bdd.steps.benchmark_install_corpus_steps",
    "tests.bdd.steps.mcp_search_steps",
    "tests.bdd.steps.mcp_prep_steps",
    "tests.bdd.steps.timeline_absolute_steps",
    "tests.bdd.steps.mcp_contradict_steps",
    "tests.bdd.steps.chunk_date_steps",
    "tests.bdd.steps.research_synthesis_steps",
    "tests.bdd.steps.search_dedup_steps",
    "tests.bdd.steps.agent_collections_steps",
    "tests.bdd.steps.eval_gate_steps",
    "tests.bdd.steps.collection_v2_default_in_scope_steps",
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
    "tests.bdd.steps.rrf_asymmetric_fusion_steps",
    "tests.bdd.steps.search_intent_gated_boosts_steps",
    "tests.bdd.steps.search_chunk_date_recency_steps",
    "tests.bdd.steps.search_collection_retrieval_overrides_steps",
    "tests.bdd.steps.search_cli_steps",
    "tests.bdd.steps.summarise_cli_steps",
    "tests.bdd.steps.kairix_cli_top_level_steps",
    "tests.bdd.steps.store_cli_steps",
    "tests.bdd.steps.brief_cli_steps",
    "tests.bdd.steps.agent_scope_callsites_steps",
    "tests.bdd.steps.setup_cli_steps",
    "tests.bdd.steps.wikilinks_cli_steps",
    "tests.bdd.steps.entity_cli_steps",
    "tests.bdd.steps.entity_audit_steps",
    "tests.bdd.steps.curator_cli_steps",
    "tests.bdd.steps.mcp_cli_steps",
    "tests.bdd.steps.cli_route_via_mcp_steps",
    "tests.bdd.steps.embed_cli_steps",
    "tests.bdd.steps.timeline_cli_steps",
    "tests.bdd.steps.soak_steps",
    "tests.bdd.steps.warm_steps",
    "tests.bdd.steps.sqlite_stats_steps",
    "tests.bdd.steps.mcp_maintenance_analyze_steps",
    "tests.bdd.steps.probe_steps",
    "tests.bdd.steps.probe_per_query_telemetry_steps",
    "tests.bdd.steps.worker_steps",
    # Worker preflight — persistence-integrity audit at boot / on demand.
    # See kairix/core/db/integrity.py for the IM-6 regression context.
    "tests.bdd.steps.worker_preflight_steps",
    "tests.bdd.steps.bootstrap_steps",
    "tests.bdd.steps.usage_guide_steps",
    "tests.bdd.steps.classify_steps",
    "tests.bdd.steps.classify_error_steps",
    "tests.bdd.steps.embed_pool_config_steps",
    "tests.bdd.steps.query_cache_steps",
    "tests.bdd.steps.enrich_cache_steps",
    "tests.bdd.steps.embed_cache_steps",
    "tests.bdd.steps.embedding_cache_steps",
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
    # Provider chat parameter-routing (gpt-5/o1/o3 max_completion_tokens translation).
    "tests.bdd.steps.provider_chat_max_completion_tokens_steps",
    # E2E provider journey step modules.
    "tests.bdd.steps.e2e_provider_chat_steps",
    "tests.bdd.steps.e2e_provider_embed_steps",
    "tests.bdd.steps.e2e_provider_health_steps",
    "tests.bdd.steps.e2e_provider_switch_steps",
    # probe-config health-check end-user CLI.
    "tests.bdd.steps.probe_config_health_steps",
    # Layered config loader — image-bundled base + sparse operator overlay.
    "tests.bdd.steps.config_layering_steps",
    # Plan B-parity Week 1 — conversation ingest CLI/use-case.
    "tests.bdd.steps.ingest_chat_steps",
    # Plan B-parity Week 2 — eval suite CLI/use-case.
    "tests.bdd.steps.eval_suite_steps",
    # Plan B-parity Week 4 Stream A — CI workflow extensions for eval gates.
    "tests.bdd.steps.eval_ci_gates_steps",
    # Plan B-parity Week 4 Stream C — soak BDD for fact-extractor pipeline.
    # Collected unconditionally; runtime-gated on KAIRIX_SOAK=1 inside the
    # step bodies so normal CI sees the scenarios but skips at first Given.
    "tests.bdd.steps.soak_fact_extractor_steps",
    # Plan B-parity Week 5 Stream A — MCP ingest + recall tools.
    "tests.bdd.steps.mcp_ingest_chat_steps",
    "tests.bdd.steps.mcp_facts_about_steps",
    # PLA-263 — kairix facts-about CLI (MCP/CLI parity for facts_about).
    "tests.bdd.steps.cli_facts_about_steps",
    # #472 — agent memory-write surfaces (kairix remember + memory_write MCP tool).
    "tests.bdd.steps.remember_cli_steps",
    "tests.bdd.steps.mcp_memory_write_steps",
    # Capability recommender (Spec A) — CLI + MCP recommend surfaces + flag.
    "tests.bdd.steps.recommend_cli_steps",
    "tests.bdd.steps.expand_cli_steps",
    "tests.bdd.steps.mcp_expand_steps",
    "tests.bdd.steps.mcp_recommend_steps",
    "tests.bdd.steps.feature_flag_recommender_steps",
    # P5 unified benchmark contract — quality + perf + stability lenses
    # wired through the canonical kairix benchmark run surface. Soak +
    # concurrent scenarios are tagged @pytest.mark.skip in the loader
    # until P3.b / P3.c land.
    "tests.bdd.steps.benchmark_unified_contract_steps",
    # Wave-2 IM-4 connector-framework extractors — passthrough +
    # markitdown plugins per
    # docs/architecture/connector-ingestion-architecture.md §2 + §3.
    "tests.bdd.steps.extractor_passthrough_steps",
    "tests.bdd.steps.extractor_markitdown_steps",
    # Wave-3 MM-1 connector-framework extractors — pdf_fallback plugin
    # (pdfplumber, MIT) per
    # docs/architecture/connector-ingestion-architecture.md §10 (Wave 3).
    "tests.bdd.steps.extractor_pdf_fallback_steps",
    # Wave-3 MM-2 OCR extractor — Tesseract default.
    "tests.bdd.steps.extractor_ocr_steps",
    "tests.bdd.steps.extractor_chain_escalation_steps",
    "tests.bdd.steps.connector_pipeline_failure_modes_steps",
    "tests.bdd.steps.silver_pathological_inputs_steps",
    # Wave-4 OF-1 slide-aware extractor — python-pptx-backed.
    "tests.bdd.steps.extractor_pptx_steps",
    # Wave-4 OF-2 docx extractor — python-docx, heading-hierarchy-aware.
    "tests.bdd.steps.extractor_docx_steps",
    # Wave-4 OF-3 xlsx extractor — openpyxl sheet-as-document.
    "tests.bdd.steps.extractor_xlsx_steps",
    # PR-3 gotenberg conversion tier — Office/ODF/Visio/RTF → PDF →
    # pdf_fallback re-entry.
    "tests.bdd.steps.extractor_gotenberg_steps",
    # Connector plugin BDD step modules — Wave 2 IM-5 lands the first
    # connector (obsidian). Future connectors (sharepoint, dex_crm, ...)
    # append a sibling entry per F36.
    "tests.bdd.steps.connector_obsidian_steps",
    # Bronze store framework BDD — write/replay/orphan-reap scenarios.
    # Phase 7 of streaming-bronze removed the FilesystemBronzeStore class
    # and the bronze_ttl_gc / orphan-reap maintenance stages. The
    # corresponding BDD features were deleted in the same commit; their
    # step modules are gone from this list.
    # PR-2 — feature-flag scaffold (kairix features status CLI + MCP tool).
    # See docs/architecture/feature-flag-architecture.md.
    "tests.bdd.steps.cli_features_steps",
    "tests.bdd.steps.mcp_features_status_steps",
    # Canonical-credential-naming CLI (kairix secrets verify).
    # See kairix/secrets/cli.py + ADR-031. The legacy alias migration
    # surface (migrate-list) was retired in #369 once operators
    # migrated to canonical KAIRIX_* env-var names.
    "tests.bdd.steps.secrets_cli_steps",
    "tests.bdd.steps.secrets_set_steps",
    # MCP tool_secrets_verify — agent-callable preflight envelope.
    "tests.bdd.steps.mcp_secrets_verify_steps",
    # Dead-letter triage surface (kairix dead-letter status CLI +
    # tool_dead_letter_status MCP). See GH #337 / #351.
    "tests.bdd.steps.cli_dead_letter_steps",
    # PR-5 — orphaned-source dead-letter drain verb (kairix dead-letter drain).
    "tests.bdd.steps.cli_dead_letter_drain_steps",
    "tests.bdd.steps.mcp_dead_letter_status_steps",
    # Wave 5 KP-1 — Dex CRM connector flag at introduce stage. F54.
    "tests.bdd.steps.feature_flag_connector_dex_crm_steps",
    "tests.bdd.steps.connector_dex_crm_steps",
    # F62 reference test — multi-tick cursor persistence invariants.
    "tests.bdd.steps.connector_cursor_persistence_steps",
    # GH #334 — Neo4j entity-graph drain (Curator-coupling boundary).
    "tests.bdd.steps.neo4j_drain_steps",
    # GH #336 / ADR-024 Bundle B — documents_media writer surfacing
    # per-extractor + per-document outcome.
    "tests.bdd.steps.documents_media_writer_steps",
    # GH #338 / ADR-024 F70 paydown — document_pages writer for paged
    # extractors (PDF / PPTX / DOCX); enables MM-3 citation paths.
    "tests.bdd.steps.document_pages_writer_steps",
    # ADR-025 Phase 1 — pipeline_status_emit flag both-branch coverage.
    "tests.bdd.steps.feature_flag_pipeline_status_emit_steps",
    "tests.bdd.steps.feature_flag_chunker_registry_dispatch_enabled_steps",
    "tests.bdd.steps.feature_flag_re_chunk_sweep_enabled_steps",
    # F64 reference test — SharePoint Graph 429 / Retry-After handling.
    "tests.bdd.steps.sharepoint_rate_limit_steps",
    # F63 reference test — maintenance scale-bound per-tick row cap.
    "tests.bdd.steps.maintenance_scale_bound_steps",
    # ADR-020 / F66 — connector per-tick budget + disk-watermark gate.
    "tests.bdd.steps.per_tick_budget_steps",
    # Wave 5 KP-2 — M365 email-headers connector (header-only per ADR-004).
    "tests.bdd.steps.connector_m365_email_headers_steps",
    "tests.bdd.steps.feature_flag_connector_m365_email_headers_steps",
    # Wave 5 KP-3 — M365 calendar connector + flag.
    "tests.bdd.steps.connector_m365_calendar_steps",
    "tests.bdd.steps.feature_flag_connector_m365_calendar_steps",
    # Wave 5 SharePoint — document-library connector + flag. Shares the
    # M365 AAD app registration (Sites.Read.All + Files.Read.All on the
    # same client-credentials triple).
    "tests.bdd.steps.connector_sharepoint_steps",
    "tests.bdd.steps.feature_flag_connector_sharepoint_steps",
    "tests.bdd.steps.connector_sharepoint_path_filtering_steps",
    # Wave E Notion — workspace-pages connector + flag. See
    # docs/architecture/connector-scope-topology/connector-design-specs/notion.md.
    "tests.bdd.steps.connector_notion_steps",
    "tests.bdd.steps.feature_flag_connector_notion_steps",
    # Wave E GitHub — greenfield Wave-E build per
    # docs/architecture/connector-scope-topology/connector-design-specs/github.md.
    # ``connector_github`` (introduce stage) gates the connector slot.
    # ``topology_github`` retired post-cutover (task #132).
    "tests.bdd.steps.connector_github_steps",
    "tests.bdd.steps.feature_flag_connector_github_steps",
    # Wave E Slack — workspace-channels connector + flag. See
    # docs/architecture/connector-scope-topology/connector-design-specs/slack.md.
    "tests.bdd.steps.connector_slack_steps",
    "tests.bdd.steps.feature_flag_connector_slack_steps",
    "tests.bdd.steps.connector_linear_steps",
    "tests.bdd.steps.feature_flag_connector_linear_steps",
    "tests.bdd.steps.connector_skills_steps",
    "tests.bdd.steps.feature_flag_connector_skills_steps",
    # IM-6 FTS-gap regression pin — connector-ingested chunks must be
    # findable via BM25 (the cutover surfaced 68,814 chunks in the
    # ``obsidian`` collection invisible to BM25 because the chunk-writer
    # skipped the FTS5 write).
    "tests.bdd.steps.connector_search_round_trip_steps",
    # Topology Wave D — operator config promotion (6 YAML blocks +
    # 5 cross-reference validators + kairix cc-pair CLI + topology
    # diagnostics in `kairix features status`). Wave A/B/C/D flag gates
    # retired post-cutover (task #132); CLI/MCP surfaces stay.
    "tests.bdd.steps.cli_cc_pair_steps",
    "tests.bdd.steps.mcp_cc_pair_steps",
    # Wave 5 Gmail — Google Workspace mailbox connector. Single-mailbox
    # per cc_pair (Onyx pattern); full-message body + envelope; History
    # API for change detection. ``connector_gmail`` (introduce stage)
    # gates the connector slot; ``topology_gmail`` retired post-cutover
    # (task #132).
    "tests.bdd.steps.connector_gmail_steps",
    "tests.bdd.steps.feature_flag_connector_gmail_steps",
    # Wave E Google Drive — workspace-files connector + flag. See
    # kairix/connectors/google_drive/README.md for the connector
    # capability surface and operator-side credential provisioning
    # (tracked under GH #356). ``topology_google_drive`` retired.
    "tests.bdd.steps.connector_google_drive_steps",
    # Apple iCloud CalDAV connector — ``topology_apple_caldav``
    # retired post-cutover (task #132).
    "tests.bdd.steps.connector_apple_caldav_steps",
    # Google Calendar connector — ``topology_google_calendar``
    # retired post-cutover (task #132). Ships OFF until Google Workspace
    # OAuth credentials are provisioned (GH #356).
    "tests.bdd.steps.connector_google_calendar_steps",
    # ADR-028 Wave G.1 — per-type chunkers (paged / structured formats).
    # Three chunker plugins shipping in one batch: SlideChunker (PPTX),
    # SheetRowChunker (XLSX / .xls / .xlsm), DocxHeadingChunker (DOCX).
    "tests.bdd.steps.chunker_slide_steps",
    "tests.bdd.steps.chunker_sheet_row_steps",
    "tests.bdd.steps.chunker_docx_heading_steps",
    # ADR-029 G.1 — agent-facing query queue + carry-along delivery.
    # tool_search-only spike behind the agent_query_queue flag (default OFF).
    "tests.bdd.steps.agent_query_queue_steps",
    "tests.bdd.steps.feature_flag_agent_query_queue_steps",
    # Issue #456 — F54 both-branch coverage for the
    # intent_confidence_gated_boosts feature flag (driven via the
    # intent_confidence_passes flag_reader DI seam).
    "tests.bdd.steps.feature_flag_intent_confidence_gated_boosts_steps",
    # Issue #429 Phase 2b — F54 both-branch coverage for the
    # entity_first_routing_enabled feature flag (driven via the
    # EntityFirstRoutingBoost flag_reader DI seam).
    "tests.bdd.steps.feature_flag_entity_first_routing_enabled_steps",
    # ADR-036 #459 Slice A — F54 both-branch coverage for the
    # entity_summary_indexing_enabled feature flag (worker-tick gate
    # for projecting Neo4j n.summary into the chunk store).
    "tests.bdd.steps.feature_flag_entity_summary_indexing_enabled_steps",
    # ADR-036 #461 Slice C — operator-facing BDD for entity-summary
    # indexing (composed-path scenarios via the production factory).
    "tests.bdd.steps.entity_summary_indexing_steps",
    # #432 deferred BDD — source-tier ranking via SourceTierBoost.
    "tests.bdd.steps.source_tier_ranking_steps",
    # #431 deferred BDD — canonical-entity seeding into Neo4j.
    "tests.bdd.steps.canonical_entity_seeding_steps",
    # Plan 1 task 10 — kairix self-installer BDD step impls.
    # Drives kairix init + uninstall via the real CLI subprocess surface
    # (F46-compliant) against an XDG-redirected tmp root. Runtime-gated:
    # scenarios needing root / a live user systemd bus skip with fix-style
    # affordances; the user-mode sibling scenarios cover the equivalent
    # code paths on every dev box.
    "tests.bdd.steps.install_steps",
    # Web setup wizard (#474) — composes build_mcp_app + FakeSetupService
    # through the public seams and drives the wizard with TestClient.
    "tests.bdd.steps.setup_wizard_steps",
    # Wizard OAuth source connect (#489) — same composition shape; the
    # source sign-in outcomes are scripted on FakeSetupService.
    "tests.bdd.steps.setup_wizard_source_steps",
    # Wizard browser-reachable remote access (#500) — tokened-URL → signed
    # cookie grant; non-loopback browser stand-in via TestClient client addr.
    "tests.bdd.steps.setup_wizard_remote_access_steps",
    # Perf & affordance SLO harness (PLA-256) — one command for cold/warm
    # latency + fact-recall quality + breadcrumb completeness.
    "tests.bdd.steps.slo_steps",
]


def pytest_sessionstart() -> None:
    """Purge orphaned staged-selection probes at true session start (SGO-199).

    ``tests/checks/test_staged_selection.py`` proves the staged-dispatch
    detectors by writing REAL ``zzz_staged_probe*`` files into the repo tree
    (``kairix/``, ``tests/``) and unlinking each in a ``finally``. The
    forbidden-import smoke stages ``kairix/core/zzz_staged_probe_f26.py`` for
    the duration of a multi-second full ``_dispatch_staged`` run. If a run is
    hard-killed inside that window (a loop timeout / ``Ctrl-C`` between the
    ``write_text`` and the ``finally``), the probe is orphaned in the tree.

    The NEXT run's early whole-tree gate tests then scan the tree and trip on
    the orphan as a net-new F26 (core→providers) violation — the
    ~50%-incidence flake SGO-199 root-caused:
      * tests/architecture/test_f26_core_import_boundary.py::test_real_repo_gate_is_green
      * tests/checks/test_catalogue_runner.py::test_in_process_verdict_matches_for_a_sample
    Both run near the START of the suite (``tests/architecture`` / early
    ``tests/checks``), long before ``test_staged_selection.py``'s own
    session-scoped sweep — which is scoped to that module and only fires once
    it is reached — can clean up. So that sweep never protected the victims.

    Running the purge here, from the ROOT conftest at ``pytest_sessionstart``
    (before collection and before any test), makes every invocation self-heal
    regardless of how a prior run died: no orphan survives into the run that
    scans for it. It also runs before collection, so an orphaned
    ``tests/zzz_staged_probe_f8.py`` can never be mis-collected as a test.
    Only ever removes uniquely-named ``zzz_staged_probe*`` paths, so it can
    never touch a real source file.
    """
    import contextlib
    import shutil
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    for path in sorted(
        repo_root.rglob("zzz_staged_probe*"),
        key=lambda probe: len(probe.parts),
        reverse=True,
    ):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


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

# Operator-override env vars a dev running kairix may have exported in their
# shell. Each one wins over the clean XDG/HOME defaults the session fixture
# installs, so they are cleared for the whole run — otherwise a dev's
# ``export KAIRIX_DATA_DIR=...`` would still route tests at the real dogfood
# store even after HOME/XDG were redirected. ``LOG_DIR`` is the non-KAIRIX_
# sibling ``KairixPaths.resolve`` reads for the log dir. Clearing
# ``KAIRIX_DOCUMENT_ROOT`` lets the document root fall through to its
# platform default ``$HOME/Documents`` — which the session fixture points at
# a clean dir — so no KAIRIX_* data-path value is SET for the run (F2).
_HERMETIC_DATA_ENV_OVERRIDES = (
    "KAIRIX_DATA_DIR",
    "KAIRIX_CACHE_DIR",
    "KAIRIX_DB_PATH",
    "KAIRIX_WORKSPACE_ROOT",
    "KAIRIX_LOG_DIR",
    "KAIRIX_DOCUMENT_ROOT",
    "LOG_DIR",
)

# Real operator credentials a dev may have exported. Cleared for the whole
# run so a test whose code path reaches ``kairix.secrets`` can never call the
# developer's Azure account. A live-credential run opts back in explicitly
# with ``KAIRIX_E2E=1`` (the e2e credential gate — see pyproject's ``e2e``
# marker), in which case the credentials are left in place.
_OPERATOR_CREDENTIAL_ENV = (
    "KAIRIX_AZURE_API_KEY",
    "KAIRIX_LLM_API_KEY",
    # Canonical names the SecretsLoader resolves (``kairix.secrets.loader``).
    "KAIRIX_PROVIDER_LLM_API_KEY",
    "KAIRIX_PROVIDER_EMBED_API_KEY",
    # Graph credential — an ambient value would let a unit test reach the
    # developer's live Neo4j instead of taking the "backend unavailable" path.
    "KAIRIX_NEO4J_PASSWORD",
)


def _ambient_env_to_clear() -> tuple[str, ...]:
    """The operator env vars the hermetic session baseline removes."""
    if _os.environ.get("KAIRIX_E2E") == "1":
        return _HERMETIC_DATA_ENV_OVERRIDES
    return _HERMETIC_DATA_ENV_OVERRIDES + _OPERATOR_CREDENTIAL_ENV


@pytest.fixture(scope="session", autouse=True)
def _hermetic_data_dirs(tmp_path_factory):
    """Redirect every default-data env kairix reads at clean session temp dirs (PLA-285).

    Root-cause fix for a whole class of local-vs-CI test divergence. Any dev
    running kairix (all six dogfood agents do) has real data under ``$HOME``,
    ``$XDG_DATA_HOME`` and ``KAIRIX_DOCUMENT_ROOT`` — so tests that resolve a
    kairix default path (``kairix/paths.py``: ``data_dir`` / ``cache_dir`` /
    ``config_dir`` / ``document_root``, plus the ``~/.local/share/kairix``,
    ``~/.cache/kairix``, ``~/Documents`` fallbacks) read the dev's live
    dogfood store instead of an isolated dir. That makes ``safe-commit`` /
    local ``pytest`` diverge from CI's clean env (e.g.
    ``test_doctor.py::test_completely_unknown_agent_returns_error_agent_health``
    resolved a real ``04-Agent-Knowledge/ghost`` dir, and the PLA-284 MCP
    warm-state tests raced the real ``~/.local/share/kairix/index.sqlite``),
    and it makes concurrent worktree agents contend on the shared
    ``~/.local/share/kairix/vectors.usearch``.

    This session-scoped, autouse baseline replicates CI's clean env for the
    whole run: ``HOME``, ``XDG_DATA_HOME``, ``XDG_CACHE_HOME`` and
    ``XDG_CONFIG_HOME`` point at empty per-session temp dirs (so the
    document root's platform default ``$HOME/Documents`` is clean too), and
    the operator vars in :func:`_ambient_env_to_clear` (data-dir overrides +
    real credentials) are cleared so a dev's shell export can't shadow those
    defaults or reach a live account. The only ``KAIRIX_*`` value set is the
    ``KAIRIX_CONNECT_DISABLE_BROWSER`` safety kill-switch — F2 recognises
    this session-scoped autouse conftest fixture as the env baseline. It
    is only the BASELINE — a test that needs specific data injects it
    explicitly (``tmp_path`` / ``FakePaths`` / an ``env=`` mapping).

    These are env-boundary safety nets (like ``_hermetic_user_config``), not
    test-shaping hacks — the session ``pytest.MonkeyPatch`` is undone at
    session teardown so the real env is restored for the process.
    """
    # tmp_path_factory / the builtin ``monkeypatch`` fixture are function-
    # scoped; a session fixture builds its own MonkeyPatch and undoes it at
    # session teardown.
    monkeypatch = pytest.MonkeyPatch()
    base = tmp_path_factory.mktemp("hermetic-home")
    home = base / "home"
    xdg_data = base / "xdg-data"
    xdg_cache = base / "xdg-cache"
    xdg_config = base / "xdg-config"
    documents = home / "Documents"
    for clean_dir in (home, xdg_data, xdg_cache, xdg_config, documents):
        clean_dir.mkdir(parents=True, exist_ok=True)

    # Lay down the ``kairix/`` data + cache subdirs exactly as ``kairix init``
    # does (``kairix/install/dirs.py`` ``ensure_dirs``). The factory's read
    # path (``build_search_pipeline`` → the topology resolver's
    # ``sqlite3.connect``) opens the DB without creating its parent — the
    # resolver issues SELECT-only queries and assumes init already laid the
    # tree down — so the subdir must exist for the connect to create the DB
    # file. Both data resolvers are covered: ``kairix.paths`` resolves the
    # vector index under ``$XDG_DATA_HOME/kairix``, while
    # ``kairix.core.db.get_db_path`` resolves the SQLite DB under
    # ``$HOME/.local/share/kairix`` (it reads HOME, not XDG_DATA_HOME).
    for kairix_subdir in (
        xdg_data / "kairix",
        xdg_cache / "kairix",
        home / ".local" / "share" / "kairix",
    ):
        kairix_subdir.mkdir(parents=True, exist_ok=True)

    # ``Path.home()`` reads ``HOME`` on POSIX, so this redirects every
    # ``~/...`` fallback in kairix/paths.py — including the document root's
    # ``$HOME/Documents`` default; the XDG vars redirect the XDG-first branches.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    for name in _ambient_env_to_clear():
        monkeypatch.delenv(name, raising=False)
    # Hard kill-switch on the kairix.connect.oauth2.* default browser path.
    # 2026-06-01 incident: a stream of real Slack "client_id not valid"
    # approval popups appeared on the operator's desktop during agent test
    # runs — root cause was the per-flow ``_DefaultBrowser`` fallback firing
    # real ``webbrowser.open`` when a test path escaped the
    # FakeBrowserLauncher injection seam. Set here, in the session baseline
    # (which runs before any test), every test runs with the kill-switch ON
    # and session teardown removes it; production leaves it unset.
    # F4-clean: the kairix-side read lives in
    # kairix/paths.py::connect_browser_disabled. F2 recognises this
    # session-scoped autouse conftest fixture structurally as the one
    # process-env baseline.
    monkeypatch.setenv("KAIRIX_CONNECT_DISABLE_BROWSER", "1")

    # Drop any path resolution cached before the env was redirected so the
    # first resolve() in the run sees the clean dirs (mirrors the
    # clear_cache() the integration real_document_root fixture does).
    from kairix.paths import clear_cache

    clear_cache()
    yield
    clear_cache()
    monkeypatch.undo()


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path_factory):
    """Hide the developer's real ``~/.config/kairix`` from every test (#492).

    The config readers resolve ``$XDG_CONFIG_HOME/kairix/kairix.config.yaml``
    — the pip-install location ``kairix init`` writes — as their final
    fallback, so on a machine where the operator has run ``kairix init``
    the ambient user config would leak into tests that assert
    no-config defaults (provider-not-configured fallbacks, path
    defaults). Redirecting XDG_CONFIG_HOME to an empty per-session dir
    keeps the suite hermetic on any machine. CI is unaffected (clean
    HOME). Tests that need their own XDG location still win — their
    ``monkeypatch.setenv`` / explicit ``env=`` dicts apply on top.

    XDG_CONFIG_HOME is not a KAIRIX_* var (F2-clean); like
    ``no_azure_calls`` this is a deliberate env-boundary safety net,
    not a test-shaping hack.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.getbasetemp() / "hermetic-xdg-config"))


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


@pytest.fixture(autouse=True)
def _reset_vector_index_singleton():
    """Drop the process-shared usearch vector-index singleton between tests (#506 / #504-sibling).

    ``kairix.core.search.vec_index._VECTOR_INDEX`` is a process-global
    handle built lazily by :func:`get_vector_index`. The first call wins
    the cache and **every later call returns that same instance,
    regardless of the ``db_path`` argument** — so once any test populates
    it (e.g. a vec-index lifecycle/contract test loading a real on-disk
    index in its ``tmp_path``), a later test that builds a default
    ``build_search_pipeline`` inherits the *foreign* index instead of one
    scoped to its own ``FakePaths`` db. The stale index then queries the
    polluter's now-deleted metadata SQLite, the vector leg silently
    returns ``[]`` (``no such table: documents``), and the test is only
    green by luck of its BM25 leg — or flips outright when the stale rows
    contaminate the result set. Resetting on teardown (closing the
    metadata connection first, via ``reset_vector_index_singleton``)
    makes every test's vector-search state isolated, mirroring the
    pattern established by ``_reset_embed_coalescer`` /
    ``_reset_client_pool``. This is the missing sibling of those resets:
    ``_VECTOR_INDEX`` was the one process-shared search singleton with no
    autouse reset, so a populator with a missing/interrupted module-local
    reset could leak its index into an unrelated later test.
    """
    yield
    from kairix.core.search.vec_index import reset_vector_index_singleton

    reset_vector_index_singleton()


@pytest.fixture(autouse=True)
def _reset_workstream_b_caches():
    """Drop the process-shared brief + prep + source caches between tests (#396 W-B).

    ``kairix.use_cases.brief``, ``kairix.use_cases.prep``, and
    ``kairix.agents.briefing.sources`` each own a process-shared TTL LRU
    added by the MCP perf sprint. Without these resets, a test that
    populates the cache leaks state into the next test, breaking
    deterministic hit/miss assertions. Mirrors the pattern established
    by ``_reset_embed_coalescer``.
    """
    yield
    from kairix.agents.briefing.sources import reset_brief_source_cache
    from kairix.core.health import reset_health_probe_cache
    from kairix.use_cases.brief import reset_brief_output_cache
    from kairix.use_cases.prep import reset_prep_summary_cache

    reset_brief_output_cache()
    reset_prep_summary_cache()
    reset_brief_source_cache()
    reset_health_probe_cache()


@pytest.fixture
def neo4j_client():
    """FakeNeo4jClient with default test entities. No real Neo4j connection."""
    return FakeNeo4jClient()


@pytest.fixture
def neo4j_client_empty():
    """FakeNeo4jClient with no entities."""
    return FakeNeo4jClient(entities=[])


@pytest.fixture
def e2e_db(tmp_path):
    """One-line E2E setup: real schema in tmpdir; factory-ready.

    Builds a ``KairixPaths`` via ``FakePaths`` rooted at ``tmp_path``,
    creates the production SQLite schema (``create_schema``) in the
    target ``db_path``, and returns the paths object ready for
    ``factory.build_search_pipeline(paths=...)`` (or any other
    composed-production-path entry point).

    Used by F48 tests (``tests/e2e/test_composed_production_path.py``)
    and any other composed-path test that wants the canonical
    tmpdir+schema+factory wiring in one fixture rather than open-coding
    the four-line setup chain.

    See ``docs/architecture/test-discipline-hardening.md`` §4.4 for the
    affordance rationale.
    """
    import sqlite3

    from kairix.core.db.schema import create_schema
    from tests.fakes import FakePaths

    paths = FakePaths(
        document_root=tmp_path / "vault",
        db_path=tmp_path / "index.sqlite",
        log_dir=tmp_path / "logs",
        workspace_root=tmp_path / "workspaces",
    )
    paths.document_root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(paths.db_path), timeout=10.0)
    db.execute("PRAGMA journal_mode=WAL")
    create_schema(db)
    db.close()
    return paths


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
