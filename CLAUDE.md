# CLAUDE.md — Engineering Standards for kairix

Shared knowledge layer for human-agent teams. See [README.md](README.md) for product context.

## How to commit

Use `bash scripts/safe-commit.sh "message"` for every commit. It runs lint, format, mypy, tests, and security checks. Loop on failures until green. See [CONSTRAINTS.md](CONSTRAINTS.md) for what blocks a commit.

## How to test

Test with fakes from `tests/fakes.py`, not monkey-patches. Construct pipelines with fake implementations. See `tests/contracts/test_protocols.py` for protocol compliance patterns.

## Architecture

Protocols define boundaries. Pipelines compose protocols. Factories build production pipelines. Repositories own data access. Strategies replace if/elif branches. See [docs/architecture/ENGINEERING.md](docs/architecture/ENGINEERING.md) for detail.

Key files:
- `kairix/core/protocols.py` — all domain boundary protocols
- `kairix/core/factory.py` — production pipeline construction
- `kairix/core/search/pipeline.py` — SearchPipeline orchestrator
- `tests/fakes.py` — fake implementations for testing

## How to delegate work

Ralph pattern: fine-grained file-scoped work, parallel agents with embedded backpressure loops, `safe-commit.sh` in each loop. 10-15 loops/hour target. See [engineering hub](https://github.com/three-cubes/engineering-hub/tree/main/ralph).

**Default for batches (≥2 independent file-scoped tasks): parallel worktrees + cherry-pick.** Dispatch each agent with `isolation="worktree"`, all in parallel. Each agent commits to its own branch and reports SHA + path. From the main checkout (on `develop`), `git cherry-pick <sha>` each agent's commit — do NOT merge the worktree branch directly, because worktrees branch off `main` (latest tagged release), not current `develop`. Direct merge would revert session work. Resolve `tests/conftest.py` and `tests/fakes.py` conflicts by combining both sides, then push and clean up the worktree.

**Default for single tasks: sequential on the main checkout, no isolation.** One agent at a time, commits and pushes direct to develop.

Every agent runs `safe-commit.sh` in its loop and only commits (and pushes, in non-worktree mode) when green.

**Worktree isolation hygiene (#208, upstream anthropics/claude-code#59019).** Subagents dispatched with `isolation="worktree"` MUST stay inside their assigned worktree for all file writes. Do NOT `cd` to the primary checkout or to another worktree. Symptom of failed isolation: untracked files appear in the primary checkout that mirror paths the subagent claims to have written in its own worktree. Orchestrator-side defense: before each `git cherry-pick <subagent-sha>`, run `python3 scripts/checks/check_worktree_isolation.py` (use `--clean` to delete shadow copies in the primary). The subagent's commit is the canonical source; the primary's untracked copy is the stale shadow.

**Primary-agent review gate before every cherry-pick.** Mechanical gates (`safe-commit.sh`, pre-commit, CI) catch *correctness*. The primary agent is the gate for *intent* — that the subagent's diff matches the dispatch brief and the project's invariants. Before `git cherry-pick <subagent-sha>`, read the diff and apply this checklist, then document the pass in the cherry-pick body or post a short rationale on the PR:

- ☐ **Scope** — diff matches the dispatched task; no scope creep (renames, refactors, doc edits the brief didn't authorise)
- ☐ **Sabotage** — every new `test_*` has a sabotage-proof noted in the agent's report (mutate prod → confirm fail → restore); spot-check one
- ☐ **Baselines** — no F-rule baseline grew unless the commit body explicitly explains why
- ☐ **Worktree** — `python3 scripts/checks/check_worktree_isolation.py` reports clean (no shadow copies in primary)
- ☐ **Affordance** — any new pipeline-blocking message follows the "X found. Refactor to YYY to pass." template with Pass + Forbidden examples (F15 is the reference)

Failing any check: send the subagent back with a `SendMessage` correction or reject and re-dispatch with tighter brief. Don't paper over with manual edits at cherry-pick time.

**Human gate on PR *creation*, not just merge.** Per `feedback_release_hitl` memory: don't push to `main`, merge `develop→main`, or cut releases without explicit per-action authorisation. Extend to release-PR opening: do NOT run `gh pr create` against `main` (or any user-visible PR for review) without the human saying "open it." Draft the body locally, present it, wait for green-light. Same gate, earlier — so the human sees the framing before the PR exists, not just before it merges.

## Languages

**Python is the default.** All retrieval, agents, eval, MCP, and domain logic stays in Python. Hot paths are already native (SQLite FTS5, usearch, sentence-transformers, neo4j C driver, spaCy) — Python is the glue, which is exactly what Python is good at.

**Go is allowed only for operational binaries** that run outside the Python venv — webhook handlers, deploy wrappers, log shippers, health probes. Single-static-binary deploys with no `pip install` on the host. The default answer to "should this be Go?" is no. See [`docs/architecture/go-integration-plan.md`](docs/architecture/go-integration-plan.md) for the four-criterion decision matrix and the G1–G10 Go-side fitness functions.

**Repo layout**: Go binaries live at `services/<name>/cmd/<name>/main.go` with a per-service `go.mod`. CI workflow `Go quality` auto-discovers any `services/*/go.mod` and runs `gofmt -s`, `go vet`, `golangci-lint`, `go test -race -cover`, and cross-compile to linux/amd64+arm64 / darwin/amd64+arm64. The Python `1 · Quality gate` is untouched and independent.

**No Rust, no PyO3, no TypeScript** in scope. Adding a third language requires its own plan-of-record.

## Naming

- Code: `snake_case` functions, `PascalCase` classes, `UPPER_SNAKE_CASE` constants (Python); `gofmt -s` decides for Go.
- User-facing: grade 8 reading level, "knowledge store" not "vault"
- Test agents: generic names (agent-alpha, agent-beta)

## Architecture fitness functions

Mechanical, blocking checks encode rejected patterns into automation:

- **F1** no `@patch` on kairix internals — **F2** no `monkeypatch.setenv("KAIRIX_*")` — **F3** every per-line suppression (`# noqa` / `# NOSONAR` / `# pragma: no cover` / `# type: ignore` / `# nosec`) has rationale — **F4** no `os.environ.get("KAIRIX_*")` outside `paths.py`/`secrets.py`.
- **F5** no internal-name imports in tests — **F6** no `*_fn=None` test-only kwargs in production.
- **F7** per-file coverage ≥ 90% (unit) — **F9** per-file coverage ≥ 90% on the unit ∪ integration union (Stage 5).
- **F8** every `test_*` carries a category marker (`unit`/`bdd`/`contract`/`integration`/`e2e`/`slow`).
- **F10** CI workflow silencers (`continue-on-error: true`, `fail_ci_if_error: false`) require rationale — **F11** test skip mechanisms (`pytest.mark.skip`/`skipif`/`xfail`/`importorskip`) require rationale.
- **F12** every BDD feature has a happy-path scenario — **F13** BDD scenarios reject implementation symbols (`Mock`, `kairix.<pkg>.<symbol>`).
- **F14** every `sonar.issue.ignore.multicriteria.*.ruleKey` in `sonar-project.properties` has a preceding rationale comment.
- **F15** no logging of secret-named variables in plaintext — `logger.*`, `print`, `sys.std{out,err}.write`, `raise X(...)` calls must not pass any `*_api_key`/`*_token`/`*_secret`/`*_password`/`*_credential`/`bearer`/`jwt`/`*_private_key` argument (or f-string interpolation thereof) outside the `kairix/{secrets,credentials}.py` boundary modules.
- **F16** cognitive complexity ≤ 15 per function (Sonar S3776) — extract helpers / early-return / dispatch-dict to flatten — **F17** no string literal of ≥10 chars duplicated ≥3 times in a module (S1192) — **F18** no commented-out code (S125) — **F19** unused function parameters must be `_`-prefixed (S1172) — **F20** empty function bodies require a docstring or `# Intentionally empty —` comment (S1186).
- **F21** every `scripts/checks/check_*.{py,sh}` failure-output string carries at least one of the lowercase action markers `fix:`, `next:`, or `run:` — so the agent reading a gate failure gets the correction action, not just the diagnosis (#258 convergence with tc-agent-zone).
- **F22** repo paths follow per-tree naming conventions — `kairix/**/*.py` snake_case, `tests/**/test_*.py`, `tests/bdd/features/*.feature` snake_case, `scripts/checks/check_*.{py,sh}`, `docs/**/runbooks/*.md` kebab-case, `.architecture/baseline/<rule>-files.txt` (#258).
- **F23** every top-level directory has a `README.md` resolver — landing on `docs/`, `tests/`, `kairix/`, etc. via a path mention must hit a one-screen orientation, not a bare directory listing (#258).
- **F24** no `from tests.*` / `import tests` imports inside `kairix/**/*.py` — `tests/` isn't shipped in the published wheel, so any production import of `tests.<x>` works locally but `ModuleNotFoundError`s the moment an end user `pip install`s kairix (#266; codifies the v2026.5.15.1 → v2026.5.15.2 incident).
- **F26** `kairix/core/**` may not import `kairix/providers/**` or `kairix/transport/**` — domain code talks to those layers through Protocols only (`kairix.core.protocols.*`). Locks the three-layer split from `docs/architecture/provider-plugin-architecture.md`.
- **F27** `kairix/providers/<a>/**` may not import another provider — plugins must stay independently shippable. Cross-provider concerns go through `kairix/transport/`.
- **F28** every plugin under `kairix/providers/<name>/` has a matching `tests/bdd/features/provider_<name>.feature` AND appears as an Examples-table row in every `tests/bdd/features/e2e_provider_*.feature` (or carries the `@<name>_no_<journey>` opt-out tag). Stops new providers shipping without behaviour tests.
- **F29** performance-measurement code (`bench*.py`, `microbench*.py`, `*_latency*.py`, `*_perf*.py`) may only land under `kairix/quality/probe/**` — the single perf surface for PVT and end-user `kairix probe-config`. Stops transport/ and providers/ growing parallel benchmark harnesses.

**Go side (active when `services/<name>/go.mod` exists; see [`docs/architecture/go-integration-plan.md`](docs/architecture/go-integration-plan.md) for full text):**

- **G1** every Go binary exposes `--version`. **G2** errors wrap with `%w`. **G3** no `interface{}`/`any` in exported signatures. **G4** `context.Context` as first arg on exported I/O. **G5** every package has a doc comment. **G6** no `panic` outside `main`/`init`. **G7** Go testing conventions only. **G8** logging via `log/slog`. **G9** every `services/<name>/` has a `README.md`. **G10** dependency-rationale registry per `services/<name>/DEPENDENCIES.md`.

Pre-existing violations are grandfathered in `.architecture/baseline/`; net-new violations block at pre-commit, in `safe-commit.sh`, and in CI's Stage 0 (or Stage 5 for F9). **Canonical reference:** [docs/architecture/fitness-functions.md](docs/architecture/fitness-functions.md). Read this before adding any silencer, skip, suppression, internal import, or BDD scenario — the gate will reject lazy bypasses.

## CI

Stages: arch-fitness (Stage 0, F1-F6+F8+F14) → pre-commit → contracts → unit+bdd+contract+mypy (Stage 2, includes F7 per-file 90% floor) → integration → security (incl. SonarCloud) → Docker. All must pass before merge.

Codecov surfaces:
- **Coverage**: `unit` flag (Stage 2) and `integration` flag (Stage 3) upload via `codecov/codecov-action@v5`. `codecov.yml` carryforwards both flags so the dashboard merges correctly when only one stage runs. Patch target = 85% (matches F7).
- **Test analytics**: JUnit XMLs from contracts / unit / integration upload via `codecov/test-results-action@v1` for flaky-test and slow-test tracking.
- **Bundles**: not applicable (Python-only project).

## Docs

| Topic | Location |
|-------|----------|
| Architecture & patterns | [docs/architecture/ENGINEERING.md](docs/architecture/ENGINEERING.md) |
| **Architecture fitness functions** | [docs/architecture/fitness-functions.md](docs/architecture/fitness-functions.md) |
| Operations & deployment | [docs/operations/OPERATIONS.md](docs/operations/OPERATIONS.md) |
| Evaluation methodology | [docs/evaluation/EVALUATION.md](docs/evaluation/EVALUATION.md) |
| Agent constraints | [CONSTRAINTS.md](CONSTRAINTS.md) |
| Quick start | [docs/getting-started/quick-start.md](docs/getting-started/quick-start.md) |
| Roadmap | [docs/project/ROADMAP.md](docs/project/ROADMAP.md) |
