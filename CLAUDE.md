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
F1 no `@patch` on kairix internals, F2 no `monkeypatch.setenv("KAIRIX_*")`,
F3 suppressions require rationale, F4 no `os.environ.get("KAIRIX_*")`
outside `paths.py`/`secrets.py`, F5 no internal-name imports in tests,
F6 no `*_fn=None` test-only kwargs in production, F7 per-file coverage
≥ 85%, F8 every `test_*` carries a category marker
(`unit`/`bdd`/`contract`/`integration`/`e2e`/`slow`). Pre-existing
violations are grandfathered in `.architecture/baseline/`; net-new
violations block at pre-commit, in `safe-commit.sh`, and in CI's
Stage 0. **Canonical reference:**
[docs/architecture/fitness-functions.md](docs/architecture/fitness-functions.md).
Read this before adding `@patch`, `monkeypatch.setenv`, `*_fn=None`,
a new suppression, or an unmarked test — the gate will reject them.

## CI

Stages: arch-fitness (Stage 0) → pre-commit → contracts → unit+bdd+contract+mypy → integration → security → Docker → SonarCloud + Codecov. All must pass before merge.

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
