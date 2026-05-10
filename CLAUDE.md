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

## Naming

- Code: `snake_case` functions, `PascalCase` classes, `UPPER_SNAKE_CASE` constants
- User-facing: grade 8 reading level, "knowledge store" not "vault"
- Test agents: generic names (agent-alpha, agent-beta)

## Architecture fitness functions

Mechanical, blocking checks encode rejected patterns into automation:

- **F1** no `@patch` on kairix internals — **F2** no `monkeypatch.setenv("KAIRIX_*")` — **F3** every per-line suppression (`# noqa` / `# NOSONAR` / `# pragma: no cover` / `# type: ignore` / `# nosec`) has rationale — **F4** no `os.environ.get("KAIRIX_*")` outside `paths.py`/`secrets.py`.
- **F5** no internal-name imports in tests — **F6** no `*_fn=None` test-only kwargs in production.
- **F7** per-file coverage ≥ 85% (unit) — **F9** per-file coverage ≥ 85% on the unit ∪ integration union (Stage 5).
- **F8** every `test_*` carries a category marker (`unit`/`bdd`/`contract`/`integration`/`e2e`/`slow`).
- **F10** CI workflow silencers (`continue-on-error: true`, `fail_ci_if_error: false`) require rationale — **F11** test skip mechanisms (`pytest.mark.skip`/`skipif`/`xfail`/`importorskip`) require rationale.
- **F12** every BDD feature has a happy-path scenario — **F13** BDD scenarios reject implementation symbols (`Mock`, `kairix.<pkg>.<symbol>`).

Pre-existing violations are grandfathered in `.architecture/baseline/`; net-new violations block at pre-commit, in `safe-commit.sh`, and in CI's Stage 0 (or Stage 5 for F9). **Canonical reference:** [docs/architecture/fitness-functions.md](docs/architecture/fitness-functions.md). Read this before adding any silencer, skip, suppression, internal import, or BDD scenario — the gate will reject lazy bypasses.

## CI

Stages: arch-fitness (Stage 0, F1-F6+F8) → pre-commit → contracts → unit+bdd+contract+mypy (Stage 2, includes F7 per-file 85% floor) → integration → security (incl. SonarCloud) → Docker. All must pass before merge.

**SonarCloud Quality Gate is blocking** as of v2026.5.10.2. Three intentionally redundant layers: (i) the `check` (CI gate) job polls SonarCloud's `/api/qualitygates/project_status` and fails on `ERROR`; (ii) GitHub branch protection on `main` requires the separate `SonarCloud Code Analysis` check posted by the Sonar app; (iii) `docker-publish.yml` and `publish-pypi.yml` begin with a `sonar-gate` job so manually-created release events also can't ship without Sonar OK. The Sonar scan step does NOT have `continue-on-error: true` — if Sonar is unavailable, the gate fails and we wait. Triage failing hotspots at https://sonarcloud.io/project/issues?id=quanyeomans_kairix.

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
