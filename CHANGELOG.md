# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: [Calendar Versioning (CalVer)](https://calver.org/) — `YYYY.MM.DD`, with `.N` suffix for same-day releases.
Git tags: `v2026.04.18`. Deploy by pinning to a tag: `pip install git+...@v2026.04.18`.

## [Unreleased]

## [2026.5.6] - 2026-05-06 — Schema, security, onboarding completion, doc hygiene

> **Upgrading?** Drop-in. No client config change, no transport change. The new `agent_owner` column is added via additive ALTER TABLE on container start; existing rows get NULL. The multi-path `AgentDef` schema parses old `collection: <name>` YAML for one release window — migrate at your own pace.

This release consolidates a sprint of work covering schema follow-on (#114, #115), security hardening (BLOCKER + MEDIUM + LOW SonarCloud findings, 51 in total), the trailing EmbedProvider wire (#43), bundled-suite hygiene (#103, #104), the onboarding flow's quality-gate stage 5 (KFEAT-013), and the first batch of public-shaped operator runbooks. Plus the previously-deployed hotfix for reference-library search pollution + MCP timeline result-shape (#119, UAT'd on `:develop` since 2026-05-05).

### Added

- **`agent_owner` column on `documents`** (#114) — first-class per-document agent provenance. Idempotent ALTER TABLE migration; existing rows get NULL. New `idx_documents_agent_owner` index. Embed scanner consults the `AgentRegistry` and tags rows with their owning agent at index time.
- **Multi-path `AgentDef` schema** (#115) — replaces the single-collection `AgentDef.collection: str` with `AgentDef.paths: list[str]`. Out-of-the-box agents now default to `/data/workspaces/{name}` instead of the historical TC-specific `04-Agent-Knowledge/{agent}` layout. New methods on `AgentDef`: `effective_paths`, `collection_names()`, `resolved_paths()`, `owns_path()`, `claims_write()`. Three deployment shapes supported (paths-omitted, single-path, multi-path TC pattern).
- **`kairix eval gate`** (KFEAT-013, stage 5) — quality-gate command that consumes a benchmark result + corpus hints and produces a go/hold verdict with concrete parameter-change recommendations. Closes the onboarding flow: `setup → entity seed → eval auto-gold → eval tune → eval gate`. Exits 0 PASS / 2 HOLD / 1 usage error so wrappers can chain.
- **Bundled-suite load-time validation** (#104) — every YAML in `suites/` is asserted to load cleanly via the test gate `test_all_bundled_suites_load_without_errors`. First-run quick-start can no longer break on a shipped suite.
- **Public-shaped operational runbooks** (OPS-009 batch 1) — three new runbooks in `docs/operations/runbooks/`:
  - `runbook-vector-search-failure.md` — `vec=0, vec_failed=True` diagnosis.
  - `runbook-embedding-lag.md` — new content not appearing in search.
  - `how-to-rebuild-entity-graph.md` — Neo4j entity graph rebuild via `kairix store crawl --full`.
  - `how-to-configure-pypi-trusted-publisher.md` (OPS-008) — one-time operator setup for OIDC-based PyPI publishing.
  All parameterised against `KAIRIX_*` env surface; no operator-specific paths or hostnames.

### Changed

- **`AgentRegistry.collection_for(name)`** is now a legacy single-collection accessor; **new code should call `collections_for(name)`** to handle multi-path agents correctly.
- **`AgentRegistry.all_collections()`** added — returns dedup-union of every agent's collection names, used by `Scope.ALL_AGENTS` / `EVERYTHING`.
- **`DocumentScanner`** accepts an optional `agent_owner_resolver` kwarg; default behaviour unchanged.
- **Wikilinks injector** now reads `document_root()` / `workspace_root()` lazily instead of capturing them at module import. Removes a long-standing `importlib.reload` requirement from the test fixture (#129).
- **`scripts/chunk-{crm,daily}-files.py`** default `--output-dir` changed from `/tmp/*` to `~/.cache/kairix/*` — safer default, no behaviour change for explicitly-passed paths.
- **`DefaultCollectionResolver._RESERVED_COLLECTIONS`** excludes `reference-library` from every default scope regardless of yaml. Reflib remains reachable via explicit `--collection reference-library`. (#119)
- **MCP `error envelope`** — uncaught exceptions inside tool handlers now return `{"error": "<ExceptionClass>: <message>"}` instead of being masked as JSON-RPC `-32602`. (carried forward from earlier work)

### Fixed

- **#103 — Benchmark crash on date-shaped gold titles.** PyYAML parses unquoted ISO dates as `datetime.date`; downstream `endswith()` raised. Coerced at suite-load boundary.
- **#104 — Bundled `reflib-gold-v1.yaml` unrunnable.** Removed (was a queries-only pre-grading snapshot); v3 is the canonical reflib gold.
- **OPS-007 (#43 trailing wire) — `recall_check._embed_query` raw HTTP.** Now goes through `EmbedProvider` for retry / rate-limit / backoff parity with the rest of the embed pipeline.
- **MCP `tool_timeline` returned empty placeholders for non-temporal queries** (#119) — `BudgetedResult` dereference fixed; real path/title/snippet/score now flow through.

### Security

- **2 BLOCKER VULN** (S2083 path-traversal) cleared with documented CLI trust-boundary rationale (#121).
- **23 MEDIUM hotspots** triaged with explicit per-finding NOSONAR rationale (#128) — 14 ReDoS (all bounded-input regexes), 5 weak-cryptography (non-security `random.*`), 4 permission (Dockerfile glob + root-by-design).
- **26 LOW hotspots** triaged (#129) — 4 signal-usage NOSONAR, 14 publicly-writable-dirs (split: 2 real script-default fixes, 12 NOSONAR for fixture-string paths), 8 GH Actions SHA-pinning intentionally left for a future supply-chain hardening sprint.

### Tests

- **2,133 unit/bdd/contract/integration tests pass** (was ~2,100 at v2026.5.3).
- New BDD coverage: `agent_collections.feature` (9 scenarios), `eval_gate.feature` (6 scenarios).
- New integration coverage: `test_multi_path_agents.py` (6 tests), `test_eval_gate_cli.py` (5 tests against real CLI dispatch).
- `tests/db/test_scanner.py` and `tests/integration/test_collections.py` refactored to use `create_schema()` from production — single source of truth with the live schema.

### Known incomplete (tracked, deferred)

- **OPS-012** — rebuild user-vault gold suite against current vault structure (#117). Runs against the live VM; deferred until the deploy window.
- **KFEAT-014** — reference-library storage isolation. Pipeline-level change; deferred to next release.
- **`kairix onboard` orchestrator** — chain stages 1–5 into one command. Worth a focused PR after this release lands.
- **GH Actions SHA-pinning** — supply-chain hardening sprint, separate workstream.
- **Vault runbook migration batch 2** — deployment-specific runbooks (binary-symlink, cron-deploy, KV setup, restart, secrets fetch) stay vault-private; needs operator-overlay design before public migration.

## [2026.5.3] - 2026-05-04 — MCP availability, agent bug closure, scope semantics

> **Upgrading? Read [`docs/upgrades/v2026.5.3.md`](docs/upgrades/v2026.5.3.md) first.** It tells your agents (or you) exactly what to change. The TL;DR is: **swap `/sse` to `/mcp` in your MCP client config.** No auth changes, no tunnels.

### Added
- **Streamable HTTP transport at `/mcp`** — every MCP tool call is now a normal HTTP request/response. Stateless per-request, no idle-connection failure mode. The legacy `/sse` endpoint is preserved on the same port for back-compat; clients can migrate at their own pace.
- **`/healthz` endpoint** — reflects readiness. Tool calls during cold-start return a structured `{"error": "kairix-initializing", "retry_after_ms": 1500}` instead of crashing.
- **Typed `Scope` parameter on every retrieval tool** — `search`, `prep`, `timeline`, and `contradict` accept five values: `shared`, `agent`, `shared+agent` (default), `all-agents`, `everything`. Cross-agent synthesis via `scope=all-agents` is now a first-class operation.
- **Agent registry** — `kairix.config.yaml` accepts an `agents:` section that declares which agents exist. `scope=all-agents` resolves to the union of their collections. Default per-agent path is `/data/workspaces/{agent}` when not declared explicitly.
- **`kairix config validate`** sub-command — catches missing collection names, duplicate agent definitions, overlapping write paths, unknown retrieval-override keys before they hit production.
- **`docs/operations/MCP-DEPLOYMENT.md`** — operator deployment guide.
- **`docs/operations/MCP-CLIENT-MIGRATION.md`** — client-side migration guide with per-client steps for Claude Desktop, Claude Code, OpenClaw, and custom Python/Node clients.
- **`docs/upgrades/v2026.5.3.md`** — version-specific upgrade guide. Drop into your agent's reading list for self-managed migrations.
- **Search log fields** — `agent`, `scope`, `collections_searched`, `vec_failed` added to the JSONL event schema.

### Changed
- **Container entrypoint** — `--transport http` (canonical) instead of `--transport sse` (deprecated alias).
- **Bundled `docker-compose.yml`** — host port now binds to `127.0.0.1` only by default. Kairix has no built-in auth; operators who want external access drop the prefix and put a gateway with auth in front.
- **`mcp` package floor** — `>=1.20,<2` (was `>=1.0,<2`) for streamable-HTTP transport stability. Other dependencies unchanged.
- **`contradict` default threshold** — `0.45` (was `0.6`) to match the new three-category composite scoring (direct + overstatement + status-mismatch). Result objects carry a `category` field. Saved invocations with explicit `--threshold 0.6` still work.
- **MCP error envelope** — uncaught exceptions inside tool handlers return `{"error": "<ExceptionClass>: <message>"}` instead of being masked as JSON-RPC `-32602 Invalid request parameters`. If you have retry logic on `-32602`, update it.
- **`tool_timeline` MCP behaviour** — falls through to search when the query has no temporal expression, matching CLI behaviour. Returns `is_temporal: false, fell_back: true`.

### Fixed
- **Research confidence always 0.0** — `mcp-kairix__research` now returns real confidence values. Previously `json.loads()` failed silently on prose responses; the new parser chain handles JSON and prose.
- **Briefing `--memory-root` path-doubling** — regression test guards against the failure mode and emits a warning if the override path already includes `/{agent}/memory`.
- **Entity suggest type errors** — role phrases are dropped, mistyped entities corrected via override sets, missing organisations promoted via configurable allowlist.
- **`-32602` masking real tool errors** — see Changed above.

### Architecture
- Eight new domain Protocols, each with a public Adapter, and a typed `Scope` enum closes Primitive Obsession. See `docs/architecture/ENGINEERING.md` §10 for the catalogue.
- Both pre-existing private-import test debts closed (`_collections_for`, `_parse_llm_response`).

### Tests
- **2,101 unit/contract/bdd tests**, **58 integration tests**. mypy strict clean across 167 source files. bandit clean on changed paths.

### Known incomplete (tracked)
- **#112** — kairix.config.yaml `retrieval:` section not loaded by the factory at runtime.
- **#114** — embed-side `agent_owner` chunk tagging.
- **#115** — multi-path agent collections schema (drops the hardcoded vault path; richer per-deployment customisation).
- **#116** — `prep` L0/L1 source non-determinism investigation.
- **#117** — user-vault gold-suite rebuild after document movement.

## [2026.4.27] - 2026-04-27 — Reference library gold suite, Docker-first deployment

### Added
- **160-query reference library gold suite** — curated benchmark covering all six query categories against the open-source reference library. Reproducible scores without a private knowledge store.
- **OpenAI SDK embed client** (#43) — `OpenAIEmbedProvider` using the `openai` SDK for direct OpenAI API embedding (non-Azure).
- **Multi-collection support** — `hybrid_search()` accepts multiple collection names; results fused across collections.
- **Port auto-detection** — `kairix mcp serve` and `kairix setup` auto-select an available port if the default is in use.

### Changed
- **Docker Compose is now the primary deployment method** — `docker compose up -d` replaces pip install as the recommended path. pip install remains as an alternative.
- **Benchmark scores updated** — weighted R10=0.8171, NDCG@10=0.8385, Hit@5=0.9629, MRR@10=0.7614 (160-query reference library suite).

### Tests
- **1,634 tests**, 86% coverage. Up from 1,222 at v2026.4.24a3.

## [2026.4.24a3] - 2026-04-24 — Researcher Agent, Embed SDK, security hardening

### Added
- **KFEAT-009: Self-contained storage** — removed QMD (Node.js) dependency entirely. Kairix now owns its own SQLite database, FTS5 full-text index, and sqlite-vec vector store. `pip install kairix` is the only install step.
- **BM25-primary fusion** — new default search strategy. BM25 results are ranked first; meaning-based (vector) results are appended for recall. 38-configuration sweep showed this outperforms standard RRF by +17% on weighted NDCG.
- **Configurable fusion strategy** — `RetrievalConfig.fusion_strategy` accepts `"bm25_primary"` (default) or `"rrf"`. Factory methods for common corpus types: `defaults()`, `for_semantic_corpus()`, `for_technical_documentation()`.
- **`kairix eval hybrid-sweep`** — grid search over fusion strategies, RRF constants, and boost parameters against a gold suite. Embedding cache for 60% faster iterations.
- **`kairix eval build-gold`** — TREC-style pooling + LLM judge to create unbiased relevance judgments from your own data.
- **`kairix eval sweep`** — BM25 column weight and query style optimisation.
- **KFEAT-010: MCP affordance** — budget auto-inference (entity lookups get smaller budgets, research queries get larger ones), entity-first hints in search results, plain-language tool descriptions.
- **KFEAT-004: Researcher Agent** — LangGraph state machine for iterative search. 6 nodes: classify_intent, retrieve, evaluate_sufficiency, refine_query, synthesise, give_up. Searches multiple times, refining the query until it finds a good answer or reports what's missing. Max 4 turns. New MCP tool: `tool_research()`.
- **EmbedProvider protocol** — `EmbedProvider` interface with `AzureEmbedProvider` and `OpenAIEmbedProvider` implementations using the `openai` SDK. Built-in retry, rate-limit handling, and exponential backoff. Factory: `get_embed_provider()`.
- **Public API surface** — `kairix.hybrid_search`, `kairix.SearchResult`, `kairix.RetrievalConfig`, `kairix.QueryIntent` exported from `kairix/__init__.py`.
- **`bm25_primary_fuse()`** in `rrf.py` — new fusion function for BM25-primary strategy.
- **Dependencies** — `langgraph>=0.2,<1` and `openai>=1.40,<2` added to core.

### Changed
- **README completely rewritten** — value-first messaging, plain language, cost comparison, agent platform integration context.
- **Benchmark scores updated** — weighted NDCG 0.818, NDCG@10 0.803, Hit@5 91.1% (293 queries, independent gold suite).
- **Vector default K** increased from 10 to 20 for better recall.
- **`RetrievalConfig`** now includes `fusion_strategy` and `rrf_k` fields.
- **Tool docstrings** rewritten for grade 8 reading level (plain language first, technical terms in brackets).
- **`CATEGORY_WEIGHTS`** centralised in `eval/constants.py` (was defined in 4 files with silent divergence).
- **`canonical_path()`** extracted to module level in `rrf.py` (was duplicated 3 times).
- **Multi-hop search** extracted from `search()` into `_run_multi_hop()` helper (reduces `search()` from 390 to ~320 lines).

### Fixed
- **Category alias bug** — sweep scoring now correctly maps `semantic→recall` and `keyword→conceptual`. Was dropping 40% of weighted score.
- **Cypher injection** — `GraphEdge` labels validated against `NodeLabel` enum via `__post_init__`.
- **Graph traversal DoS** — `max_hops` clamped to [1, 5].
- **MCP error leakage** — `str(exc)` no longer returned to callers; sanitised messages instead.
- **Secrets path leakage** — `OSError` messages no longer include internal file paths.
- **SSE transport** — MCP server defaults to `127.0.0.1` (was implicit `0.0.0.0`).
- **Lockfile** — moved from world-writable `/tmp` to `~/.cache/kairix/`.
- **Duplicate KV fetch** — `summaries/cli.py` now uses `kairix.secrets.get_secret()`.
- **Hardcoded legacy paths** — `benchmark/cli.py` QMD path replaced with `get_db_path()`.

### Removed
- **QMD dependency** — no more Node.js, npm, or external binary discovery.
- **`kairix/_qmd.py`** — QMD binary discovery module.
- **`qmd_azure_embed`** — backward-compatibility shim package.
- **`AnthropicBackend`** — stub that raised `NotImplementedError` on all methods (LSP violation).

### Security
- Dependency upper bounds added: `requests<3`, `httpx<1`, `pyyaml<7`.
- `SQLITE_VEC_PATH` no longer required; extension loaded via pip package.

### Tests
- **1,222 tests** (up from ~1,050 at v2026.4.18). 1,090 carry `@pytest.mark.unit`.
- New: 22 Researcher Agent tests, 25 MCP affordance tests, 8 EmbedProvider tests, 7 contract conformance tests, 5 e2e pipeline tests, 4 chunk-date enrichment tests.
- Dead QMD e2e test replaced with kairix pipeline e2e.

## [2026.4.18] - 2026-04-18 — kairix eval: automated evaluation suite generation

### Added
- **`kairix eval generate`** — GPL-inspired automated benchmark suite generation. Samples documents from the corpus, prompts gpt-4o-mini to write retrieval queries, runs hybrid search, judges retrieved documents with graded relevance (0/1/2), and outputs a suite YAML. Based on Generative Pseudo Labeling (Wang et al. 2022, NAACL).
- **`kairix eval enrich`** — converts an existing suite's `gold_path`-based cases to graded `gold_titles`. Runs hybrid search and LLM judge for each case. Preserves all other case fields.
- **`kairix eval monitor`** — canary regression detection with rolling JSONL log. Flags when weighted NDCG drops >5% vs the 7-day rolling average. Exit code 2 on regression (distinct from exit code 1 hard failure). Designed for integration after `kairix embed`.
- **`kairix eval report`** — generates a markdown trend report from the monitor log.
- **`kairix/eval/judge.py`** — per-document LLM relevance judge (gpt-4o-mini, 0/1/2 rubric, position-bias shuffle, 15-anchor calibration with `JudgeCalibrationError`).
- **`docs/evaluation/evaluation-methodology.md`** — methodology with research citations: Cranfield paradigm, GPL, TREC-DL, position bias (Arabzadeh et al. 2024), NDCG formula.
- **`docs/user-guide/eval-guide.md`** — user quickstart, command reference, monitoring setup, troubleshooting.

### Fixed
- Deployment process now uses tagged releases (`@v0.9.3`) rather than `@main` to make explicit which version is installed. `pip install git+...@main` silently skips reinstall when the version string is unchanged.

## [0.9.2] - 2026-04-15 — NDCG@10 in benchmark CLI output

### Changed
- **Benchmark CLI: NDCG@10 now shown in run summary** — `kairix benchmark run` now prints `NDCG@10`, `Hit@5`, and `MRR@10` directly below the weighted total when `ndcg`-scored cases are present in the suite. Previously these metrics were computed and stored in the result JSON but never displayed. NDCG@10 is the recommended metric for cross-run comparison; the weighted total continues to drive phase gate pass/fail logic.
- **Benchmark CLI: NDCG@10 delta in compare output** — `kairix benchmark compare A.json B.json` now shows a `NDCG@10 delta` row when both result files contain ndcg scores.
- `EVALUATION.md` — updated "Running the benchmark" section to show sample CLI output and clarify that NDCG@10 is the number to track across releases.

## [0.9.1] - 2026-04-15 — Apache 2.0, title-based qrels, Neo4j install script, deployment hardening

### Added
- **Benchmark: title-based document identity (TREC qrels pattern)** — `BenchmarkCase` now accepts `gold_title` (str) and `gold_titles` (list of `{title, relevance}` dicts) as the primary document identity for relevance judgments. Gold titles are stable note filename stems, decoupled from filesystem paths. A retrieved document matches if its filename stem normalises to the gold title, meaning benchmark scores are unaffected by vault reorganisation (files moved, folders renamed). New runner helpers: `_normalise_title()`, `_stem_from_path()`, `_title_in_retrieved()`, `_ndcg_score_by_title()`, `_hit_at_k_by_title()`, `_reciprocal_rank_by_title()`.
- **Benchmark: backwards compatibility** — existing suites using `gold_path`/`gold_paths` continue to work without modification. Path-based matching is retained as a fallback when `gold_titles`/`gold_title` are absent.
- **`kairix[neo4j]` optional dependency group** — `pip install "kairix[neo4j]"` installs the Neo4j Python driver (`neo4j>=5.0,<6.0`). Previously required a manual `pip install neo4j` step after deploy.
- **`check_secrets_loaded` two-tier check** — the deployment health check now probes the secrets file directly if env vars are absent. If the file exists and contains the required keys, the check returns OK with a note that credentials will activate on the next search call. This eliminates the false-negative on working deployments where secrets load lazily via `kairix._azure` import.
- **`scripts/install-neo4j.sh`** — Neo4j Community Edition install script. `--docker` (default): writes a minimal docker-compose.yml and starts `neo4j:5-community`. `--apt`: adds the Neo4j apt repository and installs via systemd. Both options print a GPL3 licence notice before proceeding, run `kairix onboard check` on completion.
- **`check_neo4j_reachable` improved fix hint** — now includes a `scripts/install-neo4j.sh` reference and a `docker run` one-liner for quick starts. Clarifies Neo4j is optional — entity boost and multi-hop are degraded without it.
- **`tests/onboard/test_check.py`** — deployment health check tests: Neo4j fix hint content assertions, secrets two-tier probe, vault root config, `run_all_checks` structural tests.

### Changed
- **Licence: MIT → Apache 2.0** — adds patent grant language. Better for commercial adoption and open-source ecosystem compatibility. `LICENSE` file replaced with full Apache 2.0 text. Copyright 2024-2026 quanyeomans contributors.
- `suites/example.yaml` — all cases migrated from `gold_paths` (path-based) to `gold_titles` (title-based). Documents are identified by their note slug, not their folder location.
- `EVALUATION.md` — methodology section rewritten to describe title-based qrels as the standard. Explains the TREC qrels convention, normalisation, and why title-based identity is correct for a living vault.
- `OPERATIONS.md` — cron section updated: replace inline `az keyvault secret show` with `source /run/secrets/kairix.env` (populated by `kairix-fetch-secrets.service`). Install instructions updated to `pip install kairix` / `pip install "kairix[neo4j]"`. New Neo4j section: optional dependency, install via `scripts/install-neo4j.sh`.
- `README.md` — install section updated to `pip install`; licence badge updated to Apache 2.0.
- `SECURITY.md` — rewritten to reflect current kairix architecture: tmpfs secrets via systemd oneshot unit, managed identity requirement, Neo4j GPL3 note, Apache 2.0 licence.

## [0.9.0] - 2026-04-14 — Neo4j-native entity system + Docker sidecar secrets

### Added
- **Curator health** (`kairix curator health`) rewritten to query Neo4j exclusively via Cypher. Reports entity counts, synthesis failures, missing vault_paths, and stale entities entirely from the graph — no SQLite dependency. `--no-neo4j` flag removed; client unavailability returns a graceful empty report.
- **entities.db retired**. `kairix/entities/` package deleted in full. Neo4j is the sole canonical entity store. `kairix entity` CLI subcommand removed. All product code (`mcp/server.py`, `briefing/sources.py`, `curator/`) updated to use Neo4j queries only.
- **Docker sidecar secrets via Azure Key Vault.** New `docker/vault-agent/` service: fetches five KV secrets at startup via `DefaultAzureCredential`, writes to tmpfs volume `/run/secrets/kairix.env` (chmod 600), signals readiness via `/run/secrets/.ready`. `kairix` service waits for `vault-agent: service_healthy` before starting.
- **`kairix/secrets.py`** — `load_secrets(path)` reads a `KEY=VALUE` file into env vars without overwriting existing values. Called at module import in `kairix/_azure.py` and `kairix/graph/client.py`. Priority: existing env vars > sidecar secrets > KV subprocess calls.
- **`docker/docker-compose.yml`** — three-service compose: vault-agent, kairix, neo4j:5-community. tmpfs secrets volume (`size=1m, mode=0700`) — secrets never written to disk.
- **`docker/.env.example`** — template for `KAIRIX_KV_NAME`, Azure service principal, path mounts, and Neo4j config.

### Removed
- `kairix/entities/` — entire package (\_\_init\_\_.py, cli.py, schema.py, graph.py, extract.py, pipeline.py, reconcile.py, resolver.py, stop\_entities.py, migrations/001\_initial.sql)
- `tests/entities/` — all entity unit and integration tests
- `KAIRIX_TEST_DB` env var from CI workflows (no longer needed)
- `kairix entity` CLI subcommand

### Changed
- `kairix curator health` now requires a live Neo4j connection; `--no-neo4j` flag no longer accepted
- `kairix/mcp/server.py` `tool_entity()`: entities.db fallback removed; Neo4j miss returns `{"error": "Entity not found: <name>"}` directly
- `kairix/briefing/sources.py` `fetch_recent_decisions()`: entities.db query block removed; decisions sourced from vault only

### Benchmark (v0.9.0, 95 curated queries)
- entity NDCG 0.811 → **0.714** (vault evolution — new content Apr 13–14 shifted gold ranks)
- keyword: 0.616 · procedural: 0.609 · temporal: 0.540 · multi_hop: 0.526 · semantic: 0.501
- **Overall NDCG@10: 0.587** · Hit@5: 0.821 · MRR@10: 0.679

---

## [0.8.1] - 2026-04-13 — Benchmark Infrastructure + Entity Enrichment

### Added
- **`kairix curator health`** — Curator agent health check CLI. Checks for synthesis failures (no summary), missing vault paths, and stale entities (configurable threshold, default 90 days). Reports Neo4j node counts when available. Output: vault-ready Markdown or JSON. Part of the Curator agent.
- **`kairix/llm/`** — `LLMBackend` protocol with `chat()`, `embed()`, `embed_as_bytes()` methods. `AzureOpenAIBackend` and `AnthropicBackend` (stub) implementations. `get_default_backend()` returns `AzureOpenAIBackend`. All product code now receives `LLMBackend` via dependency injection rather than importing backends directly.
- **Repo boundary** — all direct `kairix._azure` imports removed from product code. `hybrid.py` acquires embed via `_get_llm().embed_as_bytes()`. `search/planner.py` acquires chat via `_get_llm().chat()`. No module-level `kairix._azure` imports remain outside `kairix/llm/backends.py`.

### Fixed
- `vector_search_bytes()` now fetches `k × 4` candidates when a date filter is active. `VECTOR_DEFAULT_K=10` was too small for narrow date windows (e.g., "this week") — after force re-embed populated `chunk_date`, the top-10 candidates rarely included docs from a 7-day window, causing vec_count=0 for relative temporal queries.
- All intents now dispatch BM25 + vector in parallel. Previously keyword intent ran BM25-only, causing vector-only docs to miss entirely. Keyword NDCG: 0.48 → **0.62** (+0.110).

### Benchmark (v0.8.1, 95 curated queries)
- keyword NDCG: 0.48 → **0.616** (hybrid fix)
- entity: **0.811** · procedural: 0.609 · temporal: 0.540 · multi_hop: 0.526 · semantic: 0.501
- **Overall NDCG@10: 0.603** · Hit@5: 0.821 · MRR@10: 0.669

## [0.8.0] - 2026-04-11 — CRM Interaction Chunker + Temporal Benchmark Expansion

### Added
- Generic CRM interaction chunker. Processes JSON contact/interaction exports and writes one chunk file per interaction with injected frontmatter (date, contact, meeting_type). Enables CRM timelines to be embedded and searched with temporal filtering. 20 tests.
- Expanded temporal benchmark — 7 new cases (T02–T08) covering absolute date queries (T02–T05) and relative temporal expressions (T06–T08). Demonstrates correct behaviour: absolute date queries bypass date-range filter; relative expressions apply it.

### Notes
- The absolute-vs-relative temporal distinction (introduced in v0.7.0) is now validated with a broader case set.
- CRM interaction chunker is format-agnostic — adapt the provided script to your CRM's export schema.

## [0.7.0] - 2026-04-10 — Temporal Retrieval + Date Infrastructure

### Added
- `chunk_date` column in `content_vectors` — idempotent migration via `schema.py:ensure_vec_table`. Stores the date extracted from each chunk's source document.
- `kairix/embed/date_extract.py` — date extraction at embed time from (1) frontmatter `date`/`created`/`updated`/`created_at` fields (YYYY-MM-DD), (2) YYYY-MM year-month fields (mapped to first of month), (3) filename pattern `YYYY-MM-DD.md`. 24 tests.
- `get_date_filtered_paths(db, start, end)` in `embed/schema.py` — returns `frozenset[str]` of document paths with `chunk_date` in the given window. Used by `hybrid.py` for TEMPORAL intent date-range filtering.
- `is_relative_temporal(query)` in `temporal/rewriter.py` — returns `True` for relative temporal expressions (`last N days/weeks/months`, `recently`, `yesterday`, `today`, `this week/month`). Date filtering is only applied for relative expressions — absolute date references (`March 2026`, `2026-03-09`) query `about` a time period and must not be filtered by chunk_date.
- Date-filtered retrieval in `hybrid.py` — BM25 results post-filtered via `_path_from_file_uri()` + `date_filter_paths`; vector results post-filtered directly on `path`. Both fallback gracefully (no filter applied) when `date_filter_paths` is `None` or empty.
- `scripts/chunk-daily-files.py` — pre-processor for daily log files (`YYYY-MM-DD.md`). Splits on `##` headings, writes section chunks with injected frontmatter so each section inherits its parent document's date. 11 tests.
- `scripts/audit-date-formats.py` — scans vault `.md` frontmatter for date field coverage. Classifies values as ISO / YYYY-MM (year-month) / non-ISO / absent. 13 tests.
- YYYY-MM year-month frontmatter pattern in `date_extract.py` — maps `date: 2025-11` to `2025-11-01`. 6 additional tests.

### Fixed
- `kairix/embed/embed.py` — replaced hardcoded Key Vault name in error messages with `$KAIRIX_KV_NAME` env var reference.

### Benchmark (v0.7.0, 83 curated queries)
- temporal NDCG: 0.369 → **0.382** (date filtering for relative temporal expressions)
- entity: 0.751 · multi_hop: 0.549 · procedural: 0.564 · semantic: 0.519 · keyword: 0.439
- **Overall NDCG@10: 0.5569** · Hit@5: 0.84 · MRR: 0.67

## [0.6.0] - 2026-04-07 — Post-Refactor Benchmark + Relationship Enrichment

### Added
- `scripts/seed-entity-relations.py` — LLM-typed relationship enrichment via GPT-4o-mini batch classifier
- Nightly cron (`0 3 * * * AEST`) — entity extract + relationship seed, Azure KV secret fetch
- `cron-scripts/cron-registry.json` entry for `entity-relation-seed`
- `scripts/build-eval-gold.py` — rebuilds benchmark gold suite from live search + LLM judge
- `suites/v2-real-world.yaml` — fully rebuilt gold suite (263 cases; collection-relative path format)
- Benchmark results: NDCG@10 **0.7756** (entity 0.823, recall 0.788, multi_hop 0.728, temporal 0.810, conceptual 0.804, keyword 0.800, procedural 0.389)
- OPERATIONS.md: comprehensive deployment guide (Azure prerequisites, Key Vault secrets, first-run sequence, cron setup, monitoring, troubleshooting)

### Fixed
- Embed batch retry on dimension mismatch — `ensure_vec_table(db, actual_dims)` called per-batch on dimension error, retries once
- Hourly embed cron: now fetches `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` from Azure Key Vault at runtime (managed identity)
- Gold suite paths: rebuilt to collection-relative format (matching `kairix search` output) after vault refactor broke 196/554 paths

### Benchmark
- NDCG@10 **0.7756** on 263-case suite (vault refactor fully indexed, gold paths rebuilt)
- Entity graph: 1160 entities, 112 typed relationships seeded
- Next milestone: procedural NDCG ≥ 0.55 (current 0.389)

---

## [0.5.3] - 2026-03-28 — 1536-dim Gold Recalibration

### Added
- Recalibrated benchmark instrument after discovering 768-dim baseline was measuring a broken config (extension load order caused silent 0-dim writes)
- Confirmed 1536-dim as correct operational config; rebuilt 252-case gold suite at correct dimensionality
- `scripts/run-benchmark-v2.py`: NDCG@10 scoring engine replacing weighted-total runner

### Benchmark
- 768-dim true baseline: NDCG@10 0.7690 on 252-case suite
- 1536-dim operational: NDCG@10 0.7545 — keyword +0.114, entity +0.043 vs 768-dim

---

## [0.5.2] - 2026-03-26 — Real-World Eval Rebuild

### Added
- Replaced synthetic benchmark with real agent usage queries mined from server logs
- NDCG@10 scoring (was weighted category averages) — 134-case real-world suite
- Temporal routing fix — temporal queries routed to `kairix temporal query` before hybrid search
- Multi-hop pattern improvements — intermediate result reranking, entity bridging
- Suite expanded to 252 cases; multi-category NDCG scoring

### Benchmark
- Initial (instrument issues): NDCG@10 0.3203 on 134-case suite
- After instrument + temporal fix: NDCG@10 improved to 0.69+ range before recalibration

---

## [0.5.1] - 2026-04-06 — Entity Graph + Multi-Hop Planner

### Added
- Multi-hop QueryPlanner — GPT-4o-mini decomposes complex queries into sub-queries, parallel BM25+vector dispatch, result synthesis
- Entity graph seeded from vault-entities collection; entity boost wired into planner context injection
- `kairix entity extract --changed` incremental extraction pipeline
- `scripts/seed-entity-relations.py` (pattern-matching v1 — superseded by LLM classifier)

### Benchmark
- NDCG@10 0.7541 on 245-case suite — multi_hop 0.716 (+0.035 vs prior), entity 0.677

---

## [0.5.0] - 2026-03-23 — Temporal + Summaries + Wikilinks

### Added
- Temporal chunker + query rewriter + timeline CLI
- L0/L1 summaries generation (gpt-4o-mini) + tier router
- Wikilink injector + entity resolver + audit CLI
- Entity NER extraction pipeline + ontology reconciler
- Raw query logging: `KAIRIX_LOG_QUERIES=1` → queries.jsonl
- `scripts/analyze_queries.py`: real query distribution analysis
- Keyword zero-result fallback to vector search

### Fixed
- Vector index re-embedded at 1536-dim (was 768-dim — vectors never landed in vectors_vec)
- KV cold-start causing entity vector search failures (20-45% failure rate)
- Keyword queries returning 0 results when BM25 returns empty

## [0.4.0] - 2026-03-23 — Briefing + Classification

### Added
- `kairix brief <agent>` — 8-step concurrent briefing pipeline synthesises ~800-token session context from memory logs, entity stubs, rules, decisions, and hybrid search via GPT-4o-mini
- `kairix classify "<content>"` — two-stage auto-classification (rule-based first, LLM fallback) routes new writes to the correct vault file with confidence score
- `kairix/_azure.py`: `chat_completion()` for GPT-4o-mini synthesis calls
- `kairix/briefing/`: pipeline.py, sources.py, synthesiser.py, writer.py, cli.py — 48 tests
- `kairix/classify/`: rules.py, judge.py, router.py, cli.py — 83 tests
- Benchmark suite v1.1: CL01–CL04 classification cases; classification scoring in runner
- ENGINEERING.md: entity failure-mode patterns, benchmark suite maintenance rules, gold-path validity rules

### Fixed
- LLM judge KV secret name: `azure-openai-gpt4o-mini-deployment` (was `azure-openai-deployment` — silent 0.0 scoring on all LLM-judged benchmark cases)
- RRF path dedup: `_canonical_path()` strips collection prefix so BM25 and vector results for entity stubs now merge correctly in fused dict
- Entity benchmark gold paths: E01–E06 now have `gold_path` + `score_method: exact` (was `null`/`llm` — LLM judge had no ground truth, scored 0.2–0.4 on tangential docs)
- Entity stub content: jordan-blake.md, acme-corp.md, platform.md enriched to 650–750 words; project-x.md to 490 words

### Benchmark
- entity: 0.300 → 0.933 (gold-path fix + stub enrichment)
- classification: 1.000 (4/4 rule-based, deterministic)
- recall: 0.875 (stable)

---

## [0.3.0] - 2026-03-23 — Entity Benchmark Repair

### Added
- Entity stub enrichment: jordan-blake.md, acme-corp.md, platform.md, project-x.md enriched to ≥500 words
- Gold paths added to entity benchmark cases E01–E06

### Fixed
- Entity score collapse (0.733→0.300): root cause — benchmark gold_path: null + sparse stub content

## [0.2.0] - 2026-03-22

### Added
- Intent classifier (keyword/semantic/temporal/entity/procedural)
- BM25 wrapper (subprocess → structured results)
- Vector search wrapper (sqlite-vec CTE MATCH)
- RRF fusion + entity boost
- Token budget enforcer (L0/L1/L2 tiers)
- Hybrid orchestrator + parallel dispatch
- `kairix search` CLI
- Entity graph schema + migration system
- Entity graph (write, lookup, mentions, relationships)
- `kairix entity` CLI
- Benchmark CLI: YAML suite format, validate/run/compare/init commands
- Generalised benchmark framework SPEC.md
- CI: 4-stage pipeline, mypy strict, ruff, bandit, pip-audit, Dependabot
- ENGINEERING.md contributor guide

### Fixed
- sqlite-vec CTE pattern: MATCH must be primary table in inner CTE
- Collection scope: _SHARED_COLLECTIONS was missing vault (93% of content)
- Benchmark gold-pair validity: several benchmark gold pairs replaced with valid pairs

## [0.1.0] - 2026-03-22

### Added
- Azure OpenAI embedding pipeline (text-embedding-3-large, 1536-dim)
- Schema validation + sqlite-vec extension loading
- Staging table pattern for vec0 upserts
- Recall gate (5/5 known-doc queries post-embed)
- `kairix embed` CLI
- 50-query benchmark runner (BM25 baseline: 0.5054)
