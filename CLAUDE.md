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

**Subagents commit straight to develop. Do NOT use `isolation="worktree"` when delegating** — it creates orphan branches and locked working trees the user has to clean up. Dispatch agents sequentially against the live `develop` branch; each agent runs `safe-commit.sh` in its loop and pushes when green.

## Naming

- Code: `snake_case` functions, `PascalCase` classes, `UPPER_SNAKE_CASE` constants
- User-facing: grade 8 reading level, "knowledge store" not "vault"
- Test agents: generic names (agent-alpha, agent-beta)

## CI

Stages: pre-commit → contracts → unit+bdd+contract+mypy → integration → security → Docker → SonarCloud + Codecov. All must pass before merge.

## Docs

| Topic | Location |
|-------|----------|
| Architecture & patterns | [docs/architecture/ENGINEERING.md](docs/architecture/ENGINEERING.md) |
| Operations & deployment | [docs/operations/OPERATIONS.md](docs/operations/OPERATIONS.md) |
| Evaluation methodology | [docs/evaluation/EVALUATION.md](docs/evaluation/EVALUATION.md) |
| Agent constraints | [CONSTRAINTS.md](CONSTRAINTS.md) |
| Quick start | [docs/getting-started/quick-start.md](docs/getting-started/quick-start.md) |
| Roadmap | [docs/project/ROADMAP.md](docs/project/ROADMAP.md) |
