# CLAUDE.md — Engineering Standards for kairix

Shared knowledge layer for human-agent teams. See [README.md](README.md) for product context.

## 🛑 Canonical standards — read before touching CI, gates, fitness functions, coverage, mutation, or governance

These already exist and are detailed. **Do NOT re-derive them.** Converge *up* to them; if something
is missing or weak, propose the change *into* the canonical home — never fork a parallel standard.

- **Canonical index:** [`tc-pipelines/governance/STANDARDS.md`](https://github.com/three-cubes/tc-pipelines/blob/main/governance/STANDARDS.md)
- **Requirements / OKRs / Waves:** Build & Release Health initiative (Linear) — incl. the `<60s` local loop
- **Fitness-function spec (F-series, tiered execution):** [kairix#499](https://github.com/three-cubes/kairix/issues/499)
- **Canonical homes:** `tc-fitness` (gate engine) · `tc-pipelines` (reusable CI + governance templates)

## How to commit

Use `bash scripts/safe-commit.sh "message"` for every commit. It runs lint, format, mypy, tests, security checks, and the Sonar per-file ratchet. Loop on failures until green. See [CONSTRAINTS.md](CONSTRAINTS.md) for what blocks a commit.

**Replay the EXACT CI gate locally before pushing (canonical [STANDARDS.md §5](https://github.com/three-cubes/tc-pipelines/blob/main/governance/STANDARDS.md)).** `uv sync --all-extras --all-groups`, then `uv run pre-commit run --all-files` and `uv run tc-fitness run`; never merge over a red gate; regenerate-and-stage generated artifacts. `safe-commit.sh` is the convenience wrapper; §5 is the rule of record.

**Link issues from the PR, not by hand.** When a PR fully resolves a GitHub issue, put `Closes #N` (one keyword per issue) in the PR body so GitHub auto-closes it AND records the "closed by PR" link on merge. Use `Refs #N` for a partial fix or a parent/epic that stays open (e.g. an EPIC awaiting later phases). **Agents creating a PR with `gh pr create --body "…"` bypass `.github/pull_request_template.md`, so they must include these lines explicitly.** Don't `gh issue close` by hand after merge — it reaches the closed state but loses the auto-link, and you can't retro-trigger auto-close on an already-merged PR. Never auto-close deferred or unresolved issues.

**Raise branches + PRs as the three-cubes-agent App, never under a human's account.** A local `gh` is usually authenticated as a person; pushing/`gh pr create` over it raises the PR under *them*, so they can't cleanly review/approve their own PR and it collides with the `/.github/ @three-cubes/maintainers` code-owner gate. Mint the App identity from the canonical shared tool (off-CI complement of the CI `github-app-token` action; needs an `az login` with reader access to the agent Key Vault — see [tc-pipelines `tools/`](https://github.com/three-cubes/tc-pipelines/tree/main/tools)) and use it for the push + PR:

```bash
export GH_TOKEN="$(uvx --from 'git+https://github.com/three-cubes/tc-pipelines@v1#subdirectory=tools' agent-token)"
git config user.name 'three-cubes-agent[bot]'
git config user.email '295831460+three-cubes-agent[bot]@users.noreply.github.com'
git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/three-cubes/kairix.git"  # reset to tokenless after pushing
GH_TOKEN="$GH_TOKEN" gh pr create ...   # PR author must be app/three-cubes-agent, not a person
```

**Local-first feedback loops.** Every blocking signal (lint, type, Sonar, coverage) must be reproducible locally in <60s. CI is the *confirmation* gate, not the *discovery* loop. `safe-commit.sh --check` (below) is the <60s inner loop that makes this true; when you hit a CI-flagged Sonar issue, query the full failing set ONCE (`python3 scripts/checks/check_sonar_new_code.py --all`), batch-fix locally, push once. See [`docs/architecture/local-first-feedback-loops.md`](docs/architecture/local-first-feedback-loops.md) for the Sonar-rule → local-fix recipe map.

**`safe-commit.sh --check` is the sub-45s warm inner loop.** Tighter than `--fast`, it runs four stages only — (1) scoped ruff lint + format on the STAGED files, (2) `dmypy` (warm-daemon mypy) instead of cold `mypy`, (3) staged-path fitness (`run_checks.py --staged` — only the rules whose scope intersects the staged files), and (4) the impacted tests touching the staged paths. The FIRST run is cold (the dmypy daemon spins up, ~30s); every run after is warm (~8s of gate work, sub-second mypy). It commits on green like `--fast`. This is the loop that makes the "<60s local feedback" promise true for kairix/ source edits. The FULL gate (default `safe-commit.sh`) REMAINS the merge bar — `--check` does NOT replace CI; it is purely the local inner loop. The dmypy daemon is left warm between runs; `.dmypy.json` is gitignored.

**`safe-commit.sh --fast` for CI-fix commits.** Default `safe-commit.sh` runs the full test suite + coverage (~5-10 min). For commits that genuinely can't affect the product test surface — workflow files (`.github/workflows/*.yml`), doc-only edits, `sonar-project.properties` tweaks, `Dockerfile` build-only changes — use `--fast` to run only lint + format + mypy + tests touching the staged paths. Full gate is still the merge bar; `--fast` is just the iteration loop. Don't use `--fast` for kairix/ source edits.

**Run `bash scripts/safe-commit.sh --pre-pr` before you push / open a PR / report done — safe-commit green is necessary but not sufficient.** The default (and `--fast`/`--check`) gates run only `pytest -m "unit or bdd or contract"` (CI Stage 2). CI Stage 3 runs `pytest tests/ -m integration` as a separate tier the inner loop does not replicate, so a change can be green locally and red in CI (PLA-281: a DI-seam change broke an integration-only fake, `run_search`'s broad `except` swallowed the mismatch into empty results, safe-commit was green, and CI Stage 3 went red an hour later). `--pre-pr` closes that gap: it replicates CI Stage 3 exactly — the same `-m integration --maxfail=3` marker and the same extras set (mirrors ci.yml Stage 3's `.[dev,agents,markitdown,pdf_fallback,ocr,pptx,docx,xlsx]`, synced into a dedicated `.venv-pre-pr` so the warm inner-loop venv stays intact). It is verify-only (commits nothing, needs nothing staged) and deliberately OUT of the inner loops so the <60s promise holds. Workflow: run the normal `safe-commit.sh "message"` to commit, then `bash scripts/safe-commit.sh --pre-pr` to confirm the integration tier is green, then push.

**Three classes of failure that only surface in Linux CI (test locally OR repro in an LXC container before iterating in CI):**
- `systemctl --user` integration — Linux runners need `loginctl enable-linger + systemctl start user@$(id -u)` AND `XDG_RUNTIME_DIR + DBUS_SESSION_BUS_ADDRESS` exported via `$GITHUB_ENV` for pytest subprocess inheritance. macOS lacks systemd entirely; tests must skip gracefully.
- `/run/secrets` mount — exists on Linux, doesn't on macOS. Tests creating stubs must handle `OSError [Errno 30]` (read-only fs) and skip rather than fail.
- Docker `linux/amd64` torch wheel — pip's two-step install pattern (`pip install torch --index-url cpu` then `pip install ".[extras]"`) re-resolves torch from default PyPI in the second pass, pulling 3.6 GB of NVIDIA CUDA libs. Always single-resolve with `--extra-index-url https://download.pytorch.org/whl/cpu`.

**Exercise reusable-workflow callers before merge.** Change-detection can gate a `uses:` reusable-workflow job OFF on a workflow-only PR, so a broken `workflow_call` contract (bad input name, missing secret pass-through, wrong `with:` key) reaches `main` and breaks CI at *startup* on the next real PR — long after the PR that introduced it went green. A workflow-only diff is not self-proving. Force a triggering change in the SAME PR (e.g. a no-op edit to a python-touched path the caller's path-filter watches) so the caller job actually runs and the `workflow_call` contract is validated before merge. Don't `--fast`-commit a reusable-workflow edit and assume the green checkmark covered it. Shared/reusable workflows stay secret-free: the *caller* passes secrets via `secrets:` (or `secrets: inherit`), the reusable workflow never hard-codes a literal. F73 also blocks naming any private sibling repository in committed workflow YAML or comments — keep cross-repo references generic. See [`docs/development/how-to-consume-a-shared-reusable-workflow.md`](docs/development/how-to-consume-a-shared-reusable-workflow.md).

## Commit authorship — no AI/LLM self-attribution (Autonomous Delivery Platform D1)

Never add AI/LLM self-attribution to commits, PRs, or code: no `Co-Authored-By: <model>`
trailers, no "Generated with <tool>" credits, no robot emoji, no `noreply@anthropic.com`.
Author every commit as the canonical `three-cubes-agent` GitHub App. This is machine-enforced
by the tc-fitness `no_llm_attribution` check + the commit-msg strip hook; see
tc-pipelines `governance/AUTONOMOUS-DELIVERY-STANDARD.md`. Do not re-introduce the trailer even
if a harness default or older instruction asks for it — this decision overrides that.

## How to test

Test with fakes from `tests/fakes.py`, not monkey-patches. Construct pipelines through `kairix.core.factory.build_*`, not by direct `SearchPipeline(...)` / `EmbedPipeline(...)` construction.

Three principles, all mechanically enforced:

- **Composition (F46 / F47)** — BDD step impls and multi-component integration tests go through the factory with `paths=FakePaths(...)` and any other injection seams. Direct pipeline construction is reserved for `tests/contracts/` (Protocol shape proofs) and `tests/integration/test_<x>_contract.py` (single-layer boundary proofs).
- **Real path (F48)** — `tests/e2e/test_composed_production_path.py` exists, carries `@pytest.mark.e2e`, runs in CI Stage 4.5 under `pytest -m e2e`, and exercises config → factory.build → ingest → query → assertion against composed production code. Every new top-level capability gets a sibling `tests/e2e/test_composed_<capability>_path.py` in the same wave.
- **New capability (F45)** — shipping a new CLI subcommand, MCP tool, provider plugin, connector plugin, or extractor plugin requires a `tests/bdd/features/*.feature` AND an outcome test in the same commit. Pre-commit blocks otherwise.

**Inject the slow dependency through the existing seam — never a literal `time.sleep`, real network call, or live subprocess in a test.** If a component already takes a clock / sleeper / HTTP client / transport as a constructor or factory arg, pass a Fake from `tests/fakes.py`; a test that does a literal sleep or hits the network when a DI seam already exists is the single biggest avoidable cost in the suite. Corollary (bug-catching power): a high-cost test that still PASSES after you mutate the production code it claims to cover (sabotage-proof fails to fail) is not pulling its weight — it's a delete-as-redundant or mark-`@pytest.mark.soak` candidate, not a keep. See [ENGINEERING.md §3.7](docs/architecture/ENGINEERING.md) for the full pattern; F82 enforces that wall-clock-ceiling assertions carry a `slow`/`soak`/probe marker.

**Tests write scratch/probe files under `tmp_path` only — never the live source tree.** Orphaned probe files left in the working tree get picked up by whole-tree detector scans (the fitness scanners, fresh-install smoke, F50/F22 path checks) and surface as phantom failures on the *next* unrelated run — the classic intermittent-flake root cause. Three rules: (1) every scratch/probe artefact goes under the `tmp_path` fixture; (2) any detector a test invokes is scoped to the staged set, not the whole tree; (3) a test that must touch real paths adds a teardown/sweep fixture that removes its debris even on failure.

CLI outcome tests use `subprocess.run([sys.executable, "-m", "kairix.cli", "<sub>", ..., "--document-root", str(tmp_path)])` — no `KAIRIX_*` env vars in the subprocess invocation. MCP tools test by direct handler call with `deps=...` injected. See `tests/contracts/test_protocols.py` for protocol compliance patterns; `tests/integration/test_vec_index_lifecycle.py` for canonical factory shape; `tests/e2e/test_composed_production_path.py` for the E2E exemplar; `docs/architecture/test-discipline-hardening.md` for the full specification.

## Architecture

Protocols define boundaries. Pipelines compose protocols. Factories build production pipelines. Repositories own data access. Strategies replace if/elif branches. See [docs/architecture/ENGINEERING.md](docs/architecture/ENGINEERING.md) for detail.

Key files:
- `kairix/core/protocols.py` — all domain boundary protocols
- `kairix/core/factory.py` — production pipeline construction
- `kairix/core/search/pipeline.py` — SearchPipeline orchestrator
- `tests/fakes.py` — fake implementations for testing

## Cutover patterns

Every change that swaps production behaviour goes through a feature flag. The pattern is mandatory for connector swaps, ranker swaps, schema migrations, ingest-pipeline changes, and any cutover that's reversible-until-validated.

See [`docs/architecture/feature-flag-architecture.md`](docs/architecture/feature-flag-architecture.md) for the canonical spec. Three principles:

- **Default-safe (§2.1)** — every flag defaults to the validated behaviour. Merging flag-gated code is structurally a no-op for operators; the cutover is a separate deliberate action.
- **Both-branch tested (F54)** — every flag has BDD scenarios for OFF and ON, integration tests exercising both branches, and (for top-level capability flags) an E2E composed-path test. F54 enforces this mechanically.
- **Mechanical retirement (F51)** — every flag has a `target_retire_in` version; F51 fires past that deadline unless explicitly extended with rationale. Stops "flag becomes permanent fixture".

Cutover protocol per flag flip: capture pre-flip baseline (state digest + eval scores + probe latency + sample-journey results) → flip the flag → soak (24h min) → capture post-flip same set → diff and gate on hard thresholds (state delta within ±2%, eval within ±2pp, latency within ±20%, sample-journey ≥80% parity) → promote stage or rollback.

Cutover tooling: `scripts/cutover/capture_baseline.py` + `scripts/cutover/diff_baseline.py`. Operator surface: `kairix features status` (CLI) + `tool_features_status` (MCP) — both required by F53.

## How to delegate work

Ralph pattern: fine-grained file-scoped work, parallel agents with embedded backpressure loops, `safe-commit.sh` in each loop. 10-15 loops/hour target. See [engineering hub](https://github.com/three-cubes/engineering-hub/tree/main/ralph).

**Default for batches (≥2 independent file-scoped tasks): parallel worktrees + cherry-pick.** Dispatch each agent with `isolation="worktree"`, all in parallel. Each agent commits to its own branch and reports SHA + path. From the main checkout, `git cherry-pick <sha>` each agent's commit. Resolve `tests/conftest.py` and `tests/fakes.py` conflicts by combining both sides, then push and clean up the worktree. (Repo is trunk-based on `main` — worktrees and the primary checkout share the same base, so the historical develop/main mismatch is gone.)

**Default for single tasks: sequential on the main checkout, no isolation.** One agent at a time, commits and pushes direct to main.

Every agent runs `safe-commit.sh` in its loop and only commits (and pushes, in non-worktree mode) when green. **Before reporting done (or before the orchestrator cherry-picks / pushes), run `bash scripts/safe-commit.sh --pre-pr` once** — safe-commit green covers only CI Stage 2 (`unit or bdd or contract`); `--pre-pr` runs the CI Stage 3 integration tier (`pytest tests/ -m integration --maxfail=3`) that the inner loop skips, so "green locally" actually means "green in CI". A subagent's task is not done until `--pre-pr` is green.

**Subagent worktree venv setup (lesson from v2026.7 session — burned ~30 min of agent time before being learned).** A fresh worktree starts with NO `.venv/`. `pip install -e ".[dev]"` is not enough — kairix has optional extras (`xlsx`, `docx`, `markitdown`, `pdf_fallback`, `ocr`, `agents`, `nlp`, `rerank`, `pptx`) that BDD step modules import at the top level. Without them, pytest can't even collect the test suite. Every subagent MUST run as its first command:

```bash
uv sync --all-extras --all-groups
```

before any `pytest` or `safe-commit.sh` invocation. Dispatch briefs should include this verbatim.

**Worktree isolation hygiene (#208, upstream anthropics/claude-code#59019).** Subagents dispatched with `isolation="worktree"` MUST stay inside their assigned worktree for all file writes. Do NOT `cd` to the primary checkout or to another worktree. Symptom of failed isolation: untracked files appear in the primary checkout that mirror paths the subagent claims to have written in its own worktree. Orchestrator-side defense: before each `git cherry-pick <subagent-sha>`, run `python3 scripts/checks/check_worktree_isolation.py` (use `--clean` to delete shadow copies in the primary). The subagent's commit is the canonical source; the primary's untracked copy is the stale shadow.

**Primary-agent review gate before every cherry-pick.** Mechanical gates (`safe-commit.sh`, pre-commit, CI) catch *correctness*. The primary agent is the gate for *intent* — that the subagent's diff matches the dispatch brief and the project's invariants. Before `git cherry-pick <subagent-sha>`, read the diff and apply this checklist, then document the pass in the cherry-pick body or post a short rationale on the PR:

- ☐ **Scope** — diff matches the dispatched task; no scope creep (renames, refactors, doc edits the brief didn't authorise)
- ☐ **Sabotage** — every new `test_*` has a sabotage-proof noted in the agent's report (mutate prod → confirm fail → restore); spot-check one
- ☐ **Baselines** — no F-rule baseline grew unless the commit body explicitly explains why
- ☐ **Worktree** — `python3 scripts/checks/check_worktree_isolation.py` reports clean (no shadow copies in primary)
- ☐ **Affordance** — any new pipeline-blocking message follows the "X found. Refactor to YYY to pass." template with Pass + Forbidden examples (F15 is the reference)

Failing any check: send the subagent back with a `SendMessage` correction or reject and re-dispatch with tighter brief. Don't paper over with manual edits at cherry-pick time.

**Human gate on releases.** Per `feedback_release_hitl` memory: don't cut release tags, deploy to shared infra, or run release workflows without explicit per-action authorisation. Routine commits go direct to `main` (trunk-based); release PRs are no longer the standard ritual since develop is gone — release notes now flow through the CHANGELOG entry that `release.yml` reads into the GitHub Release body. If a release-stabilisation PR is ever opened, draft the body locally and wait for green-light before `gh pr create`. kairix is now **autonomous-on-green**: a PR with a green gate merges with zero required review on routine work; a non-bypassable two-tier ruleset requires **code-owner review only on control-plane files** (`.github/`, `pyproject.toml`, `scripts/checks/`, per the two-tier CODEOWNERS). kairix is exempt from the org-baseline-main review rule. Agents author PRs as the **three-cubes-agent** GitHub App on `agent/*` branches (exempt from the branch_naming self-gate, which enforces on all other PRs). **Never open an agent PR from a human account** — GitHub forbids approving your own PR, so a user-authored PR deadlocks the control-plane review gate, and `--admin` cannot rescue it (the ruleset has zero bypass actors). To assume the App identity locally (off-CI), mint a short-lived installation token via the broker: `az login` with **Key Vault Secrets User** on the agents key vault, then `export GH_TOKEN="$(python3 agent-token.py)"` and set the git author to `three-cubes-agent[bot]`. The broker and full procedure — including the exact vault name — are canonical in **tc-pipelines** (`scripts/agent-token.py` + README Part 4 "Agent identity"); don't reinvent them here.

## Languages

**Python is the default.** All retrieval, agents, eval, MCP, and domain logic stays in Python. Hot paths are already native (SQLite FTS5, usearch, sentence-transformers, neo4j C driver, spaCy) — Python is the glue, which is exactly what Python is good at.

**Go is allowed only for operational binaries** that run outside the Python venv — webhook handlers, deploy wrappers, log shippers, health probes. Single-static-binary deploys with no `pip install` on the host. The default answer to "should this be Go?" is no. See [`docs/architecture/go-integration-plan.md`](docs/architecture/go-integration-plan.md) for the four-criterion decision matrix and the G1–G10 Go-side fitness functions.

**Repo layout**: Go binaries live at `services/<name>/cmd/<name>/main.go` with a per-service `go.mod`. CI workflow `Go quality` auto-discovers any `services/*/go.mod` and runs `gofmt -s`, `go vet`, `golangci-lint`, `go test -race -cover`, and cross-compile to linux/amd64+arm64 / darwin/amd64+arm64. The Python `1 · Quality gate` is untouched and independent.

**No Rust, no PyO3, no TypeScript** in scope. Adding a third language requires its own plan-of-record.

## Naming

- Code: `snake_case` functions, `PascalCase` classes, `UPPER_SNAKE_CASE` constants (Python); `gofmt -s` decides for Go.
- User-facing: grade 8 reading level, "knowledge store" not "vault"
- Test agents: generic names (agent-alpha, agent-beta)

## Soak tier (`@pytest.mark.soak`)

Production-scale soak tests live under `tests/soak/` (and Bundle E's `tests/integrity_invariants/*_soak` variants) and carry `pytestmark = pytest.mark.soak`. They seed N >= 10**4 rows through the canonical fakes + `kairix.core.factory.build_*`, then assert concrete observable outcomes (row counts, wall-clock budgets, monotonicity) at production scale. Excluded from Stage 2/3 per-commit CI; the [`soak-suite.yml`](.github/workflows/soak-suite.yml) workflow runs `pytest -m soak` nightly on `main` and on-demand via `gh workflow run soak-suite.yml`. Wall-clock target 20-60 min; this workflow is NOT a branch-protection check and does NOT block PR merge. See [ADR-024 §"Soak tier (new)"](docs/architecture/ADR-024-test-pyramid-redesign.md) for the canonical spec and the three seed soak tests (`bronze_coverage_parity_at_scale`, `vector_index_drift_at_scale`, `drain_progress_at_10k`).

Re-tiering gotcha: a per-function `@pytest.mark.soak` does NOT replace a module-level `pytestmark = pytest.mark.unit` (or any module-level tier marker) — pytest STACKS markers, so the test still carries `unit` and still runs on the per-commit path. To actually move a test off Stage 2/3 and onto the nightly soak cadence, relocate it into a dedicated soak module under `tests/soak/` (or a `*_soak` integrity-invariant module) whose only `pytestmark` is `pytest.mark.soak` — don't just decorate the function in place.

## Architecture fitness functions

Mechanical, blocking checks encode rejected patterns into automation. F-numbers are permanent shipping IDs (never renumbered, never reused); the catalogue at [`scripts/checks/_rule_catalogue.py`](scripts/checks/_rule_catalogue.py) holds full metadata (category, scope, ADR origin, status) and is the canonical query surface. The groupings below match the catalogue's category dimension.

The **runner is the shared `tc_fitness` engine** ([`three-cubes-fitness`](https://github.com/three-cubes/tc-fitness), pinned `@v0.6.1` in `pyproject.toml`) — EPIC #499 common-process convergence. kairix is a **pure consumer**: `scripts/checks/run_checks.py` dispatches through `tc_fitness.runner` (the shared `run`/`main_cli` engine), configuring the engine's declarative factories (`make_module_roots_resolver`, `make_binding_narrower`, `make_env_path_conditional_check`, `main_cli(extra_flags=, post_parse=)`) with kairix's own domain values, and `_rule_catalogue.py` imports `RuleEntry` from `tc_fitness.catalogue`. The model is **shared machinery, per-repo domain**: kairix keeps its own F-numbered catalogue rows + check implementations + baselines; the dispatch logic, `CheckContext` (parse-once), staged-selection, the ratchet, and the `RuleEntry` schema all live in the package. The schema is **id-agnostic** — kairix uses F-numbers, a sibling repo uses descriptive names; both run the same runner. (`_check_context.py` / `_staged_selection.py` were local once; they're now `tc_fitness.context` / `tc_fitness.staged`.)

<!-- BEGIN F-CATALOGUE (generated; edit _rule_catalogue.py) -->

**Layering**
- **F26** kairix/core/** may not import kairix/providers/** or kairix/transport/**. **F27** kairix/providers/<a>/ may not import another provider — plugins ship independently. **F34** kairix/core/connectors/** may not import kairix/connectors/** or kairix/extractors/**. **F35** kairix/connectors/<a>/ may not import another connector or any extractor. **F37** change-detection / sync code only under kairix/connectors or kairix/core/connectors. **F38** Silver processing (chunking + signal extraction) only in kairix/core/connectors/silver.py. **F44** engagement-scope code may not import firm-scope storage clients (psycopg etc.). **F61** bare _SqliteChunkWriter(db, collection=...) construction only under kairix/core/connectors/. **F80** engagement-scope code may not call firm-scope APIs at runtime — extends F44 from import to request _(proposed)_.

**Test discipline**
- **F1** no @patch / monkeypatch on kairix internals — inject Fake* through a seam. **F2** no monkeypatch.setenv("KAIRIX_*") — pass deps as kwargs instead. **F5** no internal-name imports in tests — use public surface only. **F6** no *_fn=None test-only kwargs in production. **F8** every test_* carries a category marker (unit/bdd/contract/integration/e2e/slow/soak/invariant). **F11** every pytest.mark.skip/skipif/xfail/importorskip has a rationale comment. **F12** every BDD feature has a happy-path scenario. **F13** BDD scenarios reject implementation symbols (Mock, kairix.<pkg>.<symbol>). **F45** every new CLI/MCP/provider/connector/extractor adds a BDD feature in the same commit. **F46** BDD step impls compose via CLI/MCP/factory — no direct *Pipeline(...) construction. **F47** integration tests construct multi-component pipelines via kairix.core.factory.build_*. **F48** tests/e2e/test_composed_production_path.py exists, runs in CI Stage 4.5. **F62** every stateful tick/run_batch component has a multi-tick advance/idempotency test. **F68** every Protocol method has a failure-injection contract test. **F69** every integration test with .fetchall()/list_changes has a ≥10K-row variant. **F72** every cross-layer integrity invariant has a fixture-scale AND soak-scale test. **F81** CI fresh-install smoke — clean dir → compose boot → healthz → MCP handshake → wizard 200 → wizard choreography (POST scan partial + key form→redirect) → BM25 search hit (scripts/checks/check-fresh-install-smoke.sh via .github/workflows/fresh-install-smoke.yml; per-commit leg checks the wiring). **F82** wall-clock ceiling assertions banned outside soak/probe tiers — elapsed-time vs numeric ceiling requires a slow/soak/load/pvt marker or # F82-allowed: rationale (#493 flake family). **F84** every production config-write site (write_config_updates / update_config_file / write_config_yaml / config-writer-named yaml.dump) has a composed write→read round-trip test through the canonical layered reader (#492 overlay split-brain class). **F88** every SetupService / KairixSetupService method documenting a concrete Raises: type is either handled (except, incl. superclass) in the wizard route that calls it or render-tested under tests/platform/setup (session-escape-5 raw-500 class). **F87** every registered persist/load pair (set_secret/load_secrets_file, FileTokenStore/secrets read, write_config_updates/load_merged_mapping, EmbeddingCache put_many/get_many) ships an adversarial round-trip corpus — multi-line + unicode + large (>=64KiB) + escape-lookalike (the GitHub-PEM consent-failure class). **F86** DI-default execution floor (static half) — every _default_* production seam in kairix/** stays visible to the coverage floor: no # pragma: no cover (escape-4 class). **F86-dynamic** DI-default execution floor (dynamic half) — every _default_* seam body has ≥1 executed line in the union coverage report; skips clean when no report (F9 stage). **F30** every CLI subcommand + every MCP tool has an outcome test (subprocess or direct handler). **F75** every CLI subcommand + MCP tool + connector appears in at least one eval-suite question _(proposed)_.

**Coverage**
- **F7** per-file coverage ≥ 90% (unit) — Stage 2 floor. **F9** per-file coverage ≥ 90% on union of unit + integration (Stage 5). **new_code_coverage** new-code coverage ≥ 80% on changed lines (Sonar new-code, local). **baseline-shrinking** F49: each release tag reduces F30/F46/F47 baselines by ≥1 (or keeps at zero). **sonar-new-code** SonarCloud per-file count ratchet — current per-file open-issue/hotspot counts may not exceed the committed baseline (.architecture/baseline/sonar-per-file*.json); deterministic, no live leak period, no skip flag.

**Feature flag**
- **F54** every flag has OFF + ON BDD scenarios, integration tests, and (for top-level) an E2E composed-path test. **F51** every FeatureFlag has target_retire_in ≤ current scm version + 6 months. **F52** every flag("<name>") call site references a name that exists in REGISTRY. **F53** kairix features status CLI subcommand + features_status MCP tool both exist.

**Plugin contract**
- **F28** every provider plugin has matching BDD feature + Examples-table row in E2E features. **F36** every connector + extractor plugin has matching BDD feature + Examples-table row. **F40** every Extractor plugin declares module-level version: str + make_extractor factory. **F41** every plugin tree has py.typed marker + no unjustified # type: ignore. **F42** Protocol methods return frozen-dc/tuple — never dict[str, Any] or bare Any. **F43** behavioural parity — every contract test runs ONE parametrized body over real + fake (≥2 impl fixtures), not separate real-only/fake-only assertions; plus the per-plugin contract-test presence limb. **F55** every Chunker plugin declares version + every Chunk(...) passes chunker_version= _(vacuous)_. **F56** every connector declares SourceConnector + at least one of {Poll, Checkpointed, Event}Connector. **F64** every plugin importing an HTTP client ships a rate-limit test (429/Retry-After). **F65** every connector implements metadata_for + propagation test for chunk_date/author.

**Production safety**
- **F15** no logging of secret-named variables in plaintext outside kairix/{secrets,credentials}.py. **F39** every Chunk(...) constructor call passes source_uri + source_modified_at + sensitivity explicitly. **F50** net-new files may not appear in any per-file F-rule baseline. **F63** every .fetchall() includes LIMIT in the query or carries a # F63-bounded: rationale. **F66** every connector + tick-driven component declares per_tick_max_items + disk_watermark_min_free_bytes. **F73** token-pattern scanner for private infra identifiers (externalised pattern source). **F89** every served file under a kairix/**/web/static/ tree has a sha256-pinned ASSETS.lock manifest row (upstream version + sha256 + url + rationale); the on-disk sha256 must match the row, so a swapped/outdated htmx.min.js or pico.css fails instead of shipping untraced. **F94** no runtime writes to system/OS paths (/etc, /opt, /usr, ...) — production code in kairix/** persists config + state through kairix.paths (the writable data dir) and the config overlay, never a hardcoded system path, so kairix runs least-privilege on hardened / read-only-root VMs (the wizard-save overlay class, #485/#492). **F91** wizard browser surface — HTML responses carry nosniff + frame-denial + a Content-Security-Policy (Limb A: contract test + render-path static check), and every inline <script> in the setup templates is F91-inline rationale-tagged, ≤20 lines, and not cross-template-duplicated (Limb B). **F76** no f-string interpolation of content-like vars (raw/body/payload/markdown/...) in log/exception/dead-letter strings. **F78** soak suite asserts RSS / peak memory ≤ budget — needs runtime instrumentation _(proposed)_. **F95** Neo4j write-mode selection lives at ONE chokepoint — only kairix/knowledge/graph/client.py may name `default_access_mode` / the `_is_write_query` derivation, so no caller re-derives read-vs-write and silently opens a READ session for a write query (the #628 silent-write class). **F96** embed-discovery completeness queries that LEFT JOIN content_vectors ... IS NULL must reference a state column (model / embedded_at), never presence alone — a presence-only join counts an un-promoted placeholder as 'done' (the chunk-0 #627 class, where seq=0 placeholders were never embedded).

**Schema integrity**
- **F57** every UPDATE topology_cc_pairs SET status=? lives next to a _ALLOWED_TRANSITIONS dispatch dict. **F58** HierarchyConnector impls have a parent-before-child contract test _(vacuous)_. **F67** every pushed_to_<sink> column has a matching UPDATE site flipping 0 → 1. **F70** every CREATE TABLE has at least one INSERT INTO site OR a # table-is-derived: rationale. **F71** every preflight _check_* counting external state has a count-equals-ground-truth contract test. **F77** sqlite3.connect call sites outside the allow-list (worker/factory/scripts/tests) are flagged _(proxy)_. **F79** every schema delta has a tested rollback path; destructive changes keep N-day grace _(proposed)_.

**Agent affordance**
- **F3** every # noqa / # NOSONAR / # pragma / # type: ignore / # nosec has rationale text. **F10** CI workflow silencers (continue-on-error, fail_ci_if_error: false) require rationale. **F14** every sonar.issue.ignore.multicriteria entry has a preceding rationale comment. **F16** cognitive complexity ≤ 15 per function (Sonar S3776). **F17** no string literal ≥10 chars duplicated ≥3 times in a module (Sonar S1192). **F18** no commented-out code (Sonar S125). **F19** unused function parameters must be _-prefixed (Sonar S1172). **F20** empty function bodies require docstring or # Intentionally empty — comment (Sonar S1186). **F21** every check_*.py failure-output carries fix:/next:/run: action markers. **F23** every top-level directory has a README.md resolver. **F83** gate-runner contract for shell gate scripts — no unguarded VAR=$(...) under set -e (#483 silent-death class), || true requires trailing rationale, shellcheck-clean at error severity, safe-commit.sh/run-all.sh stages emit named OK/FAIL verdicts, and quiet grep output probes cannot produce false failures under pipefail. **F85** registered cross-tier contract vocabularies single-sourced — a member of a DECLARED vocabulary (source-auth PHASE_* strings, the azure provider-name set; owned by kairix/platform/setup/service.py) re-declared as a constant/collection in another setup-tier module, or used as a raw string in a template instead of the env.globals symbol, fails (session-escape-8 phase-string class). **F90** setup-wizard template ↔ route referential integrity (F52 applied to the browser tier) — every hx-get/hx-post/href/action /setup URL resolves to a Route/Mount in routes.py, every hx-target/hx-include #id is defined in some template, every template is rendered or extended (dangling-button class: the tour replacing first-search left no dead control). **paydown-doc-currency** grandfathering paydown doc reflects current baseline state. **capability-affordance** agent-callable capabilities surface their affordances at the call boundary. **F97** every agent-facing result surface embeds or returns the shared SourceRef breadcrumb — each registered result-row dataclass (search / timeline / entity / prep / research / contradict) declares a SourceRef-typed field OR a source_ref() accessor, so the canonical resolvable source_uri is surfaced uniformly and the per-surface pointer drift can't re-accrue (PLA-274). **F98** every agent result-row surface exposes an expand-acceptable locator AND expand accepts a source_uri-only call — each registered surface (search / timeline / entity / prep / research / contradict / expand) exposes the resolvable source_uri (source_ref() / SourceRef field / source_uri field) an agent feeds to expand, and run_expand keeps its seq parameter optional, so a document/section-level (L2) hit whose seq is null never dead-ends at a guessed #0 (the anti-dead-end lock, PLA-297). **F99** bundled usage guide stays in sync with the tool registry — every capability in tool_capabilities() (each registered MCP tool + CLI subcommand) is discoverable in kairix/agents/usage_guide/data/agent-usage-guide.md via its CLI / bare <mcp_tool> wire-name token (the guide is generated from CAPABILITIES_CATALOG with the bare names), so an agent self-training from the guide can find every shipped tool (the drift class that let `expand` fall out); flag-gated-off surfaces are excluded (PLA-299 / PLA-321).

**Repo hygiene**
- **F4** no os.environ.get("KAIRIX_*") outside paths.py / secrets.py. **F22** repo paths follow per-tree naming conventions. **F24** no `from tests.*` / `import tests` inside kairix/**/*.py — tests not shipped in wheel. **F29** performance-measurement code only under kairix/quality/probe/. **F32** no real names in test fixtures (use agent-alpha etc. + reference library). **F33** shellcheck disable directives require rationale. **no-hardcoded-user-paths** F31: no hardcoded /Users/ or /home/<dev>/ paths. **SGO-156** no AI/LLM self-attribution residue in first-party source/docs — no model co-author trailer, no AI-vendor no-reply author identity, no robot emoji (the metadata agent tools stamp onto commits); agent work is authored by the canonical bot/human, never advertised as model-generated (decision D1).

**Observability**
- **F74** every Stage subclass is only invoked via a StageRunner — never direct .process() call _(vacuous)_. **F93** every ci.yml job is in the 'CI gate' aggregator's needs: closure (so its failure blocks the merge) or carries a # fan-in: informational marker — a green merge can't ship with an un-gated job failing.

**Process**
- **worktree-isolation** subagent worktree isolation — no shadow copies in primary checkout. **F92** catalogue currency — every check_*.{py,sh} has a RuleEntry, every RuleEntry maps to an existing check, and the generated doc regions match generate_catalogue_docs.py --check (the self-hosting guard for the catalogue-driven runner). **SGO-158** every commit author AND committer over the PR range (cutover..HEAD) carries an allow-listed identity — the canonical three-cubes-agent App, the named human maintainer, and the platform merge/bot committers — so an off-allowlist or marker-in-name identity can't slip in; guard-forward via cutover_ref (decision D2). **SGO-270** the root harness references the central engineering canon — as a product repo, kairix carries the full root harness set (CLAUDE.md, AGENTS.md, RESOLVER.md, ETHOS.md, SCORECARD.md, CONTRIBUTING.md) and at least one harness file carries BOTH the `Canonical standards` marker AND a link to tc-pipelines governance/STANDARDS, so agents route to canon instead of re-deriving a parallel standard (Golden Path convergence). **SGO-269** kairix's CI consumes the ONE shared quality gate — Stage-0 arch-fitness runs the `tc-fitness run` engine via the python-quality-gate reusable — rather than a hand-rolled fork, so the single standard is enforced mechanically (Golden Path convergence).

**Go side**
- **G1** every Go binary exposes --version. **G6** no panic outside main/init. **G8** logging via log/slog (no log.Println or fmt.Println in service code). **G9** every services/<name>/ has a README.md. **G10** dependency-rationale registry per services/<name>/DEPENDENCIES.md.

<!-- END F-CATALOGUE -->

Pre-existing violations are grandfathered in `.architecture/baseline/`; net-new violations block at pre-commit, `safe-commit.sh`, and CI Stage 0 (or Stage 5 for F9). Full detail per rule: [`scripts/checks/_rule_catalogue.py`](scripts/checks/_rule_catalogue.py) (kairix's catalogue rows — schema imported from `tc_fitness.catalogue`) + [`docs/architecture/fitness-functions.md`](docs/architecture/fitness-functions.md) (canonical reference). Read these before adding any silencer, skip, suppression, internal import, or BDD scenario — the gate rejects lazy bypasses.

## CI

Stages: arch-fitness (Stage 0, F1-F6+F8+F14) → pre-commit → contracts → unit+bdd+contract+mypy (Stage 2, includes F7 per-file 90% floor) → integration → security (incl. SonarCloud) → Docker. All must pass before merge.

Codecov surfaces:
- **Coverage**: `unit` flag (Stage 2) and `integration` flag (Stage 3) upload via `codecov/codecov-action@v5`. `codecov.yml` carryforwards both flags so the dashboard merges correctly when only one stage runs. Patch target = 85% (matches F7).
- **Test analytics**: JUnit XMLs from contracts / unit / integration upload via `codecov/test-results-action@v1` for flaky-test and slow-test tracking.
- **Bundles**: not applicable (Python-only project).

## Docs — agent-actionable resolver

Find the canonical doc for the task you're doing. Each row reads
"to do X → read Y / run Z". When multiple docs apply, the **bold** one
is the source-of-truth; the others fill in detail.

### 1. Project vision + roadmap

| To do this | Read |
|---|---|
| Understand why kairix exists + who it's for | **[`README.md`](README.md)** — pain → outcome framing for human-agent teams |
| See what's shipped + what's next | **[`docs/project/ROADMAP.md`](docs/project/ROADMAP.md)** — current state, near-term direction, capability matrix |
| Inspect a specific release's behaviour changes | [`CHANGELOG.md`](CHANGELOG.md) — per-version entry; pairs with [`docs/upgrades/`](docs/upgrades/) for upgrade steps |
| Trace a discussion / decision back to context | [`GitHub Discussions → Roadmap`](https://github.com/three-cubes/kairix/discussions) — priorities, RFCs, feature direction |

### 2. Architecture

| To do this | Read |
|---|---|
| Understand the layered architecture (Protocols / Pipelines / Factories / Repositories) | **[`docs/architecture/ENGINEERING.md`](docs/architecture/ENGINEERING.md)** — patterns, factory composition, repository pattern |
| Understand the deployment topology (Docker compose, VM, MCP transport) | [`docs/architecture/ADR-017-deployment-architecture.md`](docs/architecture/ADR-017-deployment-architecture.md) |
| Understand the provider plug-in surface (Azure Foundry, OpenAI, Bedrock, …) | [`docs/architecture/provider-plugin-architecture.md`](docs/architecture/provider-plugin-architecture.md) — three-layer split locked by F26/F27/F28 |
| Understand the connector / source ingestion framework (Obsidian, Dex, M365, SharePoint, Linear; planned Notion / Teams / Slack / GitHub / Drive) | [`docs/architecture/connector-ingestion-architecture.md`](docs/architecture/connector-ingestion-architecture.md) — Wave 0-5 framework, locked by F34-F44. Ingestion runs a pre-extract compatibility gate (`kairix/core/connectors/compat.py` — known-unsupported formats are recorded `skipped_unsupported` instead of dead-lettering; OOXML-as-octet-stream is disambiguated and indexed), an auto-drain of permanently-unprocessable dead-letters on sync (`kairix/core/connectors/deadletter_drain.py` — `corrupt_zip` + known-unsupported MIMEs only), and an optional `gotenberg` PDF-conversion extractor tier for legacy Office / Visio / ODF / Publisher / RTF. SharePoint drives **self-discover from a site** (`drives: [{site_id|site_url}]` — no hand-entered drive IDs; per-site fault isolation); **sync observability** in `kairix worker status` distinguishes a quiet connector from a silently-stalled one (cursor-freeze / low-disk / poison surface as WARNs); `kairix dead-letter drain [<source>]` + a periodic sweep clear any source's permanently-unprocessable backlog |
| Plan connector / collection / scope topology evolution (multi-instance connectors, cross-source collections, per-actor scope profiles, skill-driven retrieval) | **[`docs/architecture/connector-scope-topology/ADR.md`](docs/architecture/connector-scope-topology/ADR.md)** — proposed 5-layer model; `00-overview.md` for nav, `01-05` for source analysis / use cases / BDD / simulation / non-functionals |
| Add guided configuration for a connector (discover available sites/drives/channels/repos → pick from list → emit YAML → progress reporting during ingest) | [`docs/architecture/guided-configuration.md`](docs/architecture/guided-configuration.md) — KFEAT-022; SharePoint pilot deep-dive + the generalised pattern for Slack / GitHub / Notion |
| Understand the fact layer / conversational recall surface | [`docs/architecture/fact-layer.md`](docs/architecture/fact-layer.md) — ADR + Capability #1–#5 from v2026.5.18 |
| Understand the CLI ↔ MCP feature-parity contract | [`docs/architecture/cli-mcp-feature-parity.md`](docs/architecture/cli-mcp-feature-parity.md) |
| Decide whether new operational code should be Go or Python | [`docs/architecture/go-integration-plan.md`](docs/architecture/go-integration-plan.md) — four-criterion matrix + G1–G10 Go fitness functions |

### 3. Engineering practices

| To do this | Read / run |
|---|---|
| Write a test the right way (Protocol fakes, no monkey-patches) | **[`docs/architecture/ENGINEERING.md#testing`](docs/architecture/ENGINEERING.md)** + [`tests/fakes.py`](tests/fakes.py) + [`tests/contracts/test_protocols.py`](tests/contracts/test_protocols.py) |
| Run the same gates CI runs, locally | `bash scripts/safe-commit.sh "<message>"` — lint, format, mypy, pytest+coverage, arch-fitness, secrets, confidential-pattern, sonar per-file ratchet |
| Reproduce a CI-flagged Sonar / lint / type / coverage issue locally in one shot | **[`docs/architecture/local-first-feedback-loops.md`](docs/architecture/local-first-feedback-loops.md)** — Sonar-rule → local-fix recipe map; `python3 scripts/checks/check_sonar_new_code.py --all` pulls the full failing set so you batch-fix once instead of push-per-fix |
| Pay down a grandfathered baseline entry (resolve a `.architecture/baseline/<rule>-files.txt` line) | **[`docs/architecture/grandfathering-paydown.md`](docs/architecture/grandfathering-paydown.md)** — three resolution shapes (refactor / rule-exempt with rationale / structural change), per-baseline status + next-move, and the deprecation endgame |
| Onboard as a new contributor | [`CONTRIBUTING.md`](CONTRIBUTING.md) + [`docs/getting-started/quick-start.md`](docs/getting-started/quick-start.md) |
| Understand evaluation methodology + benchmark suites | [`docs/evaluation/EVALUATION.md`](docs/evaluation/EVALUATION.md) |
| Run a benchmark / interpret scores | [`docs/operations/runbooks/how-to-run-benchmark.md`](docs/operations/runbooks/how-to-run-benchmark.md) |

### 4. Guardrails + preferred patterns

| To do this | Read |
|---|---|
| See what blocks a commit (the mechanical contract) | **[`CONSTRAINTS.md`](CONSTRAINTS.md)** — short list of hard blocks |
| Understand the architecture fitness functions F1–F54 + G1–G10 | **[`docs/architecture/fitness-functions.md`](docs/architecture/fitness-functions.md)** — canonical reference; read before adding any silencer, skip, suppression, or internal import |
| Land a new top-level capability with its discipline carrying | **[`docs/architecture/test-discipline-hardening.md`](docs/architecture/test-discipline-hardening.md)** — F45..F49, the three principles (composition / real-path / new-capability), canonical test shapes |
| Cut over from old behaviour to new without breaking operators (connector swap, ranker swap, schema migration, etc.) | **[`docs/architecture/feature-flag-architecture.md`](docs/architecture/feature-flag-architecture.md)** — F51..F54, default-safe / both-branch-tested / mechanical-retirement principles, capture-flip-soak-gate cutover protocol |
| Avoid known code-smell patterns | [`docs/architecture/ENGINEERING.md#code-smells`](docs/architecture/ENGINEERING.md) — inappropriate intimacy, feature envy, test-shaped APIs |
| Understand security posture | [`SECURITY.md`](SECURITY.md) + F15 (no logging of secret-named variables in plaintext) |

### 5. Deployment + release approach & automation

> **Identity + deploy plane (2026-06):** release/deploy workflows run as the
> **`three-cubes-agent` GitHub App over WIF** — each job mints a short-lived
> installation token at runtime via `github-app-token@v1` (Key Vault → App
> creds, no GitHub-stored secret); tags, releases, and dispatches are authored
> by the App, not `github-actions[bot]`. The **VM deploy uses the canonical
> tc-pipelines `azure-vm-deploy.yml@v1`** (WIF → disk snapshot → `az vm
> run-command` the box-side `scripts/deploy/apply-alpha.sh` → smoke
> `systemctl is-active`), replacing the bespoke HMAC-webhook (retired — the box-side
> apply logic is now the single source in `scripts/deploy/apply-alpha.sh`; the manual
> fallback when CI is down is to run that script directly on the box). Same "canonical in
> tc-pipelines, don't reinvent" rule as the agent-token note: use the shared
> action/workflow, don't re-implement WIF/login/deploy here.

| To do this | Read / run |
|---|---|
| Understand the operational deploy model (Docker compose, healthchecks, secrets-from-KV) | **[`docs/operations/OPERATIONS.md`](docs/operations/OPERATIONS.md)** |
| Deploy the MCP server (HTTP transport, cold-start, readiness gate) | [`docs/operations/MCP-DEPLOYMENT.md`](docs/operations/MCP-DEPLOYMENT.md) |
| Cut an alpha release | `gh workflow run release-alpha.yml -f date_version=YYYY.M.D -f alpha_n=N` — see [`docs/operations/runbooks/how-to-upgrade-kairix.md`](docs/operations/runbooks/how-to-upgrade-kairix.md) |
| Cut a stable release | `gh workflow run release.yml --ref main -f version=vYYYY.M.D -f changelog_label=YYYY.M.D` — workflow tags `main`, pulls `CHANGELOG.md` section into the GitHub Release body. **Also bump the `docs/project/ROADMAP.md` Current-state header to the new version** so the roadmap doesn't drift (GH #693) |
| Browse all runbooks (entity audit, embedding lag, ranking debug, regression…) | [`docs/operations/runbooks/INDEX.md`](docs/operations/runbooks/INDEX.md) |
| Read per-release upgrade notes (operator-facing) | [`docs/upgrades/`](docs/upgrades/) — one file per release; latest is the highest `v2026.M.D.md` |
| Migrate config overlay (pre-upgrade prereq for shared-mount deploys) | [`docs/operations/runbooks/config-overlay-upgrade.md`](docs/operations/runbooks/config-overlay-upgrade.md) |
| Trace a kairix incident (entity graph corruption, recall regression, embedding stall) | [`docs/runbooks/`](docs/runbooks/) (kairix-side) + [`docs/operations/runbooks/`](docs/operations/runbooks/) (operator-side) |
