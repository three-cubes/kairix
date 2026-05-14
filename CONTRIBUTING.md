# Contributing

Read [CLAUDE.md](CLAUDE.md) for engineering standards and [CONSTRAINTS.md](CONSTRAINTS.md) for hard boundaries before starting.

## Setup

```bash
git clone https://github.com/three-cubes/kairix
cd kairix
pip install -e ".[dev,neo4j,agents,rerank]"
```

## Making changes

1. Branch from `develop` (the default branch — also where PRs target)
2. Make your changes
3. Commit via the gated script: `bash scripts/safe-commit.sh "your message"`
4. The script runs lint, format, mypy, tests, and security checks. If any fail, fix and re-run.
5. Run `pre-commit run --all-files` once before pushing — `safe-commit.sh` historically diverges from CI's pre-commit; the explicit run catches it locally.
6. Open a PR targeting `develop` — the repo only allows **merge commits** (no squash, no rebase) to preserve per-commit history.

For routine work where you're confident in `safe-commit` + `pre-commit`, direct push to develop is also fine — the same CI gate runs on push and PR.

## Running tests

```bash
# All tests that must pass before commit (same as safe-commit.sh)
pytest tests/ -m "unit or bdd or contract" -x --timeout=30

# Integration (requires real SQLite index)
pytest tests/ -m integration -v

# E2E (requires running kairix instance + credentials)
KAIRIX_E2E=1 pytest tests/e2e/ -v -s
```

## Testing approach

Tests use protocol fakes, not monkey-patches. See `tests/fakes.py` for fake implementations and `tests/contracts/test_protocols.py` for protocol compliance patterns.

```python
from tests.fakes import FakeClassifier, FakeDocumentRepository
from kairix.core.search.pipeline import SearchPipeline
from kairix.core.search.backends import BM25SearchBackend

pipeline = SearchPipeline(
    classifier=FakeClassifier(),
    bm25=BM25SearchBackend(FakeDocumentRepository(documents=[...])),
    ...
)
result = pipeline.search("test query")
```

See [CONSTRAINTS.md](CONSTRAINTS.md) for what's not allowed in tests.

## Architecture

Protocols define every boundary. Pipelines compose protocols. Factories build production pipelines. See [CLAUDE.md](CLAUDE.md) for the full architecture overview.

Key files for contributors:
- `kairix/core/protocols.py` — all domain boundary interfaces
- `kairix/core/factory.py` — how production pipelines are constructed
- `kairix/core/search/pipeline.py` — the search pipeline orchestrator
- `tests/fakes.py` — fake implementations for testing

```
kairix/
  core/
    protocols.py         # Domain boundary protocols
    factory.py           # Production pipeline construction
    search/
      pipeline.py        # SearchPipeline orchestrator
      backends.py        # BM25, Vector search adapters
      fusion.py          # RRF, BM25Primary fusion strategies
      boosts.py          # Entity, Procedural, Temporal boost strategies
    db/
      repository.py      # SQLiteDocumentRepository
    embed/
      pipeline.py        # EmbedPipeline orchestrator
  knowledge/
    graph/
      repository.py      # Neo4jGraphRepository
  quality/
    eval/
      scorers.py         # NDCG, ExactMatch, LLMJudge scoring strategies
    benchmark/
      pipeline.py        # BenchmarkPipeline orchestrator
  agents/
    briefing/
      pipeline.py        # BriefingPipeline orchestrator
tests/
  fakes.py               # All fake implementations
  contracts/             # Protocol compliance tests
  integration/           # Real DB, real paths
```

## Branching model

| Branch | Purpose |
|---|---|
| `develop` | **Default branch.** All work lands here — direct push or PR. |
| `main` | Release-only: each commit is the SHA of a tagged release. Promoted from `develop` via the `5 · Release` workflow at release time. |
| `feat/*`, `fix/*` | Optional feature branches when grouping multiple commits — PR targets `develop`. |

The `raw.githubusercontent.com/.../main/...` URLs in [README.md](README.md) and [docker-compose.yml](docker-compose.yml) deliberately point at `main` so users following the quick-start get the last-released compose, not in-progress work.

## Versioning

CalVer: `YYYY.MM.DD`. Pre-release: `YYYY.MM.DDaN`.

## Cutting a release

1. Validate on deployment target
2. Confirm `CHANGELOG.md` `[Unreleased]` section is fully populated (no empty sub-sections) and the version label matches CalVer (`vYYYY.M.D[.N]`)
3. Open a PR `develop → main` — gates on the same CI checks as any other PR
4. Once green, merge with the standard merge commit
5. Trigger the **`5 · Release`** workflow (Actions tab → workflow_dispatch) with `version=vYYYY.M.D[.N]`. It tags `main` HEAD, extracts the `[Unreleased]` CHANGELOG section as release notes, and creates the GitHub Release. The release-created event then fires Docker + PyPI publish workflows automatically.

See [scripts/release-checklist.md](scripts/release-checklist.md) for the full end-to-end checklist including post-deploy UAT.
