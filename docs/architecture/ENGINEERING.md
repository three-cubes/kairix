# Engineering Disciplines

Standards, quality gates, and compliance requirements for Kairix contributors.

---

## Contents

1. [Quality Gates](#1-quality-gates)
2. [CI/CD Pipeline](#2-cicd-pipeline)
3. [Testing Standards](#3-testing-standards)
4. [Security Standards](#4-security-standards)
5. [Code Style](#5-code-style)
6. [Dependency Management](#6-dependency-management)
7. [Branch and PR Conventions](#7-branch-and-pr-conventions)
8. [CLI Standards](#8-cli-standards)
9. [Engineering Compliance Checklist](#9-engineering-compliance-checklist)
10. [Architecture Patterns](#10-architecture-patterns)
11. [References](#11-references)

---

## 1. Quality Gates

Every merge to `main` must pass all four CI stages. No exceptions without a documented override (see §2.4).

| Gate | Tool | Threshold | Blocks merge? |
|---|---|---|---|
| Type checking | mypy (strict) | Zero errors | ✅ Yes |
| Linting | ruff | Zero errors | ✅ Yes |
| Unit tests | pytest | 100% pass | ✅ Yes |
| Test coverage (per-file) | F7 | ≥ 85% on every kairix/* file (ratcheted) | ✅ Yes |
| SAST | bandit | Zero HIGH findings | ✅ Yes |
| Dependency CVEs | pip-audit | Zero CVEs with fixes | ✅ Yes |
| Contract tests | pytest -m contract | Zero failures | ✅ Yes |
| Architecture fitness functions | F1–F6, F8 | Zero net-new violations | ✅ Yes |
| Build | pip install -e . | Succeeds | ✅ Yes |

**Architecture fitness functions** are mechanical, blocking checks that encode rejected patterns (e.g. forbidden monkeypatching, internal-name imports in tests, unmarked tests). They run at three layers — pre-commit, `safe-commit.sh`, and CI Stage 0. Pre-existing violations are grandfathered in `.architecture/baseline/`; net-new violations block. Canonical reference: [`fitness-functions.md`](./fitness-functions.md).

**Per-file coverage floor (mechanical, F7):** every `kairix/*` source file must clear 85% line coverage. Pre-existing violations are grandfathered in `.architecture/baseline/per-file-coverage-floor-files.txt`; new files must land at >=85% from day one. The aggregate 80% pytest-cov gate stays in place as a backstop.

**Codecov surfaces:**
- **Coverage** — two flags upload from CI: `unit` (Stage 2: `pytest -m "unit or bdd or contract" --cov`) and `integration` (Stage 3: `pytest -m integration --cov`). Carryforward is enabled for both so the dashboard doesn't flap when only one stage runs. Patch target = 85% (matches F7). Components: Search / Agents / Knowledge / Quality / Core for per-area dashboards.
- **Test analytics** — JUnit XMLs from contracts, unit (3.12), and integration jobs upload via `codecov/test-results-action@v1`. Codecov tracks flaky tests, slow-test trends, and failure history across runs.
- **Bundles** — not applicable; kairix is Python-only with no JS/TS frontend bundle.

Configuration source-of-truth: `codecov.yml` in repo root (validated against `https://codecov.io/validate`). The `[tool.coverage.run].omit` list in `pyproject.toml` is the *only* place files are excluded from coverage measurement; do not add an `ignore:` block to `codecov.yml` (would create a second omit list that drifts).

---

## 2. CI/CD Pipeline

### 2.1 Workflow overview

Five stages run on every push and PR. Stages 3 and 4 run in parallel after Stage 2.

```
push/PR
  │
  ├── Stage 0: Architecture fitness (30s)   ← runs F1-F6, F8 (F7 in Stage 2)
  │     bash scripts/checks/run-all.sh --skip-coverage
  │
  ├── Stage 1: Contracts (30s)              ← fast gate, fails fast
  │     pytest -m contract  → results-contracts.xml
  │     ↳ codecov/test-results-action (flag=contract)   [test analytics]
  │
  ├── Stage 2: Unit + Type (2-3min)         ← runs on py3.10, 3.11, 3.12
  │     mypy --strict
  │     ruff check + format
  │     pytest -m "unit or bdd or contract" --cov  → coverage.xml + results-unit.xml
  │     F7: per-file 85% coverage floor (3.12 only)
  │     ↳ codecov/codecov-action (flag=unit, 3.12 only) [coverage]
  │     ↳ codecov/test-results-action (flag=unit, 3.12 only) [test analytics]
  │
  ├── Stage 3: Integration (5min)  ─┐
  │     pytest -m integration --cov  → coverage-integration.xml + results-integration.xml
  │     ↳ codecov/codecov-action (flag=integration)     [coverage]
  │     ↳ codecov/test-results-action (flag=integration) [test analytics]
  │                                  │ parallel with Stage 4
  └── Stage 4: Security (5min)    ──┘
        bandit (SAST)
        pip-audit (CVE scan)
        detect-secrets (secret scan)
        SonarCloud scan (consumes coverage.xml from Stage 2)
        artifact upload
```

### 2.2 Workflow files

| File | Trigger | Purpose |
|---|---|---|
| `.github/workflows/ci.yml` | Every push + PR | Four-stage pipeline (all gates) |
| `.github/workflows/integration.yml` | PR to main | Full integration suite + PR compliance checks |
| `.github/workflows/benchmark-gate.yml` | Manual dispatch | Benchmark comparison (required for retrieval PRs) |
| `.github/workflows/reflib-benchmark-gate.yml` | Manual dispatch | Reference library benchmark comparison |
| `.github/workflows/dependency-review.yml` | PR | Dependency change review |
| `.github/workflows/docker-publish.yml` | Release/tag | Docker image build and publish |
| `.github/workflows/publish-pypi.yml` | Release/tag | PyPI package publish |
| `.github/dependabot.yml` | Weekly Monday 03:00 AEST | Automated dependency updates |

### 2.3 Deployment

```bash
# Pin to a tagged release — do not deploy from @main
pip install git+https://github.com/quanyeomans/kairix@v2026.04.18

# Or from PyPI when published:
pip install kairix==2026.4.18

# Smoke test after deploy:
kairix onboard check
kairix search "test query" --agent <your-agent>
```

**Rollback:** `pip install git+https://github.com/quanyeomans/kairix@<previous-tag>`. All state is in SQLite/document store — safe.

### 2.4 Override process (emergency only)

If a gate must be bypassed:
1. Document justification with specific business reason in the PR
2. Risk assessment and consequences
3. Mitigation plan with timeline to address
4. Maintainer approval required
5. Auto-expires: must be resolved in the next release cycle

---

## 3. Testing Standards

### 3.1 Test pyramid

```
     ┌─────────┐
     │   E2E   │  ~1%  KAIRIX_E2E=1 required. Never in CI.
     ├─────────┤
     │Integr.  │  ~5%  Real usearch. Skips cleanly if unavailable.
     ├─────────┤
     │Contract │  ~7%  Interface agreements. Zero tolerance. <30s total.
     ├─────────┤
     │  Unit   │  ~60%  Mocked externals. Fast. CI matrix (3.10/3.11/3.12).
     ├─────────┤
     │Eval/BDD │  ~27%  Benchmark, eval, reflib, BDD, and setup tests.
     └─────────┘
```

### 3.2 Test markers

Mark every test class or function with the appropriate marker:

```python
@pytest.mark.contract    # interface agreement — schema, API shape, data format
@pytest.mark.unit        # individual component logic
@pytest.mark.integration # multi-component, real usearch
@pytest.mark.e2e         # live Azure API (requires KAIRIX_E2E=1)
@pytest.mark.slow        # takes >5s
```

Run by stage:
```bash
pytest -m contract               # Stage 1: <30s, must pass
pytest -m "not integration"      # Stage 2: unit only (CI)
pytest -m integration            # Stage 3: requires usearch
KAIRIX_E2E=1 pytest -m e2e      # Manual only
```

### 3.3 Mocking rules

**Mock only external services:**
- Azure OpenAI API (use `unittest.mock.patch` or `responses` library)
- File system (use `tempfile.TemporaryDirectory()`)

**Keep real:**
- SQLite operations (use test DB via `KAIRIX_TEST_DB` env var)
- Internal logic and data structures
- usearch extension (integration tests load the real `.so`)

**Never mock the thing under test.** If the test requires mocking the module being tested, the test is testing the wrong thing.

### 3.4 What must have a test

Every production bug found becomes a test immediately.

### 3.5 Regression prevention

When a bug is found in production:
1. Write a failing test that reproduces it
2. Fix the bug (make the test pass)
3. Tag the test `# regression: <brief description>`
4. Do not ship the fix without the test

### 3.6 Benchmark as evaluation (not CI)

The benchmark (`kairix benchmark`) is NOT a CI test. It's an evaluation tool:
- Requires live Azure API and a populated database
- Runs manually or via scheduled cron
- Results committed to `benchmark-results/`
- Required in PR description when retrieval logic changes

Phase gate rule: Phase N+1 does not start until Phase N benchmark confirms gate score.

---

## 4. Security Standards

### 4.1 Secret management

- **All secrets via Key Vault at runtime.** `az keyvault secret show --vault-name ${KV_NAME}`
- **Never written to disk, environment file, or log**
- **Never passed as function arguments** — use the shared `_azure.py` client
- `detect-secrets` runs in CI on every PR (baseline in `.secrets.baseline`)
- If a secret is exposed: rotate immediately in Key Vault (next process run picks it up)

### 4.2 SAST (bandit)

Run locally before committing:
```bash
bandit -r kairix/ --severity-level medium
```

- Zero HIGH findings: blocks merge
- MEDIUM findings: documented in PR with risk assessment; tracked as issues
- Exclusions (`# nosec`): require inline justification comment

### 4.3 Dependency security (pip-audit)

```bash
pip-audit --requirement <(pip freeze) --format markdown
```

- Zero CVEs with available fixes: blocks merge
- CVEs without fixes: documented in PR, tracked as issues, remediated within 1 week when fix becomes available
- Dependabot opens weekly PRs for dependency updates (see §6)

### 4.4 4-layer defence

| Layer | Tool | Trigger | Gate |
|---|---|---|---|
| SAST | bandit | Every PR | Zero HIGH |
| Dependency scan | pip-audit | Every PR + weekly Dependabot | Zero CVEs with fixes |
| Dynamic testing | pytest security tests | Every PR | All security tests pass |
| Source control | detect-secrets | Every PR | No secrets in diff |

### 4.5 Agent scoping enforcement

The `--agent` parameter in all kairix commands enforces collection boundaries. Tests must verify that:
- Agent A cannot write to Agent B's knowledge collections
- Shared collections are readable by all agents but only writable via explicit `--scope shared`

---

## Security Standards

These rules are enforced by CI (CodeQL, Bandit, detect-secrets) and must be followed in all code changes.

### Logging

- **Never log exception objects** from credential-fetching code paths. An exception raised during Key Vault fetch, secrets file parsing, or auth can contain the raw credential value in its message. Log the operation name and return code only.
- **Never log user query content** at any log level without an explicit opt-in env var (e.g. `KAIRIX_DEBUG_QUERIES=1`). Queries may contain personal or commercially sensitive information.
- **Never log raw LLM responses** at DEBUG. Truncate or omit entirely — `logger.debug("step: failed")` not `logger.debug("step: failed %r", raw_response)`.
- Logging a Key Vault **secret name** (not value) is acceptable at INFO/WARNING for operational tracing.

### Subprocess

- `subprocess.run()` must always use a **list of arguments**, never a shell-interpolated string with `shell=True`.
- If a command string must be split at runtime, use `shlex.split()` rather than `.split()` or f-strings.

### GitHub Actions

- Every job must declare a **minimal `permissions:` block** explicitly. Never rely on inherited defaults.
- The top-level workflow `permissions` should be `contents: read`. Jobs requiring write access declare it individually.

### CodeQL Suppressions

- Use inline `# lgtm[query-id]` comments only for **confirmed false positives** or **intentional product behaviour** (e.g. the secret-agent sidecar writing secrets to tmpfs, or the briefing CLI outputting user-owned documents).
- Every suppression must include a `— reason` comment explaining why it is safe.
- Do not use blanket path exclusions in `codeql-config.yml` — suppress at the specific line.

### detect-secrets

- The `.secrets.baseline` file must be updated when legitimate non-secret strings trigger false positives.
- `detect-secrets` is a **hard gate** in CI — a failed scan blocks merge.
- Never add `continue-on-error: true` to the detect-secrets step.

---

## 5. Code Style

### 5.1 Type annotations

**All public functions must have type annotations.** This is enforced by `mypy --strict` in CI.

```python
# Correct
def rrf_score(bm25_rank: int, vec_rank: int, k: int = 60) -> float:
    ...

# Wrong — will fail mypy
def rrf_score(bm25_rank, vec_rank, k=60):
    ...
```

### 5.2 Named constants

All thresholds, configuration values, and magic numbers must be named constants at module level with a comment explaining their derivation.

```python
# Correct
RRF_K = 60             # Standard RRF constant — prevents high ranks dominating. A/B tested in Phase 1.
ENTITY_BOOST = 0.20    # Boost factor per entity mention. Capped at ENTITY_BOOST_CAP.
ENTITY_BOOST_CAP = 2.0 # Maximum entity boost multiplier.

# Wrong
score = 1 / (60 + rank)   # where did 60 come from?
```

### 5.3 Module docstrings

Every module must have a top-level docstring covering:
- What the module does
- Key inputs and outputs
- Failure modes and fallbacks

```python
"""
kairix.search.rrf
~~~~~~~~~~~~~~~~~

Reciprocal Rank Fusion implementation for combining BM25 and vector search results.

Inputs:
  bm25_results: list of BM25Result from kairix.search.bm25
  vec_results:  list of VecResult from kairix.search.vector

Output:
  list of FusedResult, sorted descending by RRF score

Failure modes:
  - Either input list empty: returns the non-empty list ranked by original score
  - Both empty: returns []
  - Entity boosting DB unavailable: returns fused results without boost (logged)
"""
```

### 5.4 No print() in production code

Use `logging` in all non-CLI modules. `print()` is allowed only in `cli.py` files (for user-facing output). Enforced by ruff `T201` rule.

### 5.5 Commit message convention

```
feat(search): implement RRF fusion with entity boosting (#42)
fix(embed): load usearch before --force DELETE (#38)
test(embed): add TestExtensionLoadOrder for production bug (#39)
docs: add ENGINEERING.md — engineering disciplines
chore(deps): bump requests from 2.31 to 2.32
```

Types: `feat`, `fix`, `test`, `docs`, `refactor`, `chore`, `perf`
Scope: module name or area (`embed`, `search`, `entities`, `ci`, `deps`)

---

## 6. Dependency Management

### 6.1 Dependabot

Weekly automated PRs (Monday 03:00 AEST):
- **Python dependencies:** Minor/patch dev dependencies grouped into one PR. Production dependencies (requests) get individual PRs.
- **GitHub Actions:** Action version updates.

All Dependabot PRs require CI to pass before merge. No manual merge without CI green.

### 6.2 usearch version

usearch is installed as a pip dependency (`usearch>=0.1.6`). No manual extension path configuration needed.

Vector storage uses usearch natively via pip — no SQLite extension or manual path configuration required.

### 6.3 Adding a new dependency

1. Is it really necessary? Can we use stdlib?
2. Check `pip-audit` for known CVEs before adding
3. Pin to a minor version: `requests>=2.31,<3.0`
4. Add to `pyproject.toml` under the correct group (`dependencies` or `dev`)
5. Update `.github/dependabot.yml` if it needs custom grouping

---

## 7. Branch and PR Conventions

### 7.1 Branch naming

```
feat/search-hybrid-rrf         # new feature
fix/embed-extension-load-order # bug fix
refactor/embed-staging-table   # internal restructure
test/search-intent-classifier  # test additions
docs/engineering-disciplines   # documentation
chore/deps-bump-requests       # dependency updates
```

### 7.2 Version discipline

kairix uses CalVer: `YYYY.M.D` for stable releases on `main`, `YYYY.M.Da<N>` for alpha releases on `develop`.

**Rule: the version in `pyproject.toml` must be incremented before deploying to any environment.** This is what allows `pip install --upgrade` to work correctly — pip compares version numbers, not commit SHAs. Deploying without a version bump means pip sees the existing version as current and installs nothing.

| Branch | Version example | Increment rule |
|--------|----------------|----------------|
| `develop` | `2026.4.18a3` | Increment `aN` before each deploy to a test/staging host |
| `main` | `2026.4.18` | Increment date component on each stable release |

Installing from a branch ref (`@develop`, `@main`) rather than a pinned tag does not override this — pip still resolves by version number. Pinned tags are the correct install target for reproducible environments.

### 7.3 PR requirements

**Before opening a PR:**
- [ ] All CI stages pass locally (`pytest tests/`, `mypy kairix/`, `ruff check kairix/`)
- [ ] No secrets in diff (`detect-secrets scan kairix/ tests/`)
- [ ] If retrieval logic changed: benchmark comparison included in description
- [ ] Version bumped in `pyproject.toml` if this set of changes will be deployed

**PR description must include:**
- What changed and why (1-3 sentences)
- How to test/verify
- If retrieval logic changed: before/after benchmark scores (at minimum recall and conceptual categories)
- Any open questions or follow-up work

**Merge strategy:** Squash merge only. PR title becomes commit message.

### 7.3 Review requirements

- At least one maintainer approval
- All CI stages green
- No unresolved comments

---

## 8. CLI Standards

kairix is a tool consumed by both humans and agents. All subcommands must follow standard conventions so any caller (human, shell script, or AI agent) can interact predictably.

### 8.1 Required flags

Every CLI entry point must handle:

| Flag | Behaviour |
|------|-----------|
| `--version`, `-V` | Print `kairix <version>` to stdout and exit 0 |
| `--help`, `-h` | Print usage to stdout and exit 0 |

These are checked at the top-level dispatcher before subcommand dispatch.

### 8.2 Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | User error (bad args, missing file, check failed) |
| 2 | Configuration error (missing env var, bad service.env) |
| 3+ | Reserved for subcommand-specific errors (document in the subcommand's CLI module) |

### 8.3 Output format

- Human-readable output goes to stdout.
- Progress, warnings, and diagnostics go to stderr (logger).
- Structured output (`--json`) must be valid JSON on stdout with no other lines mixed in.
- `kairix onboard check --json` is the canonical machine-readable health signal.

### 8.4 Compliance checklist addition

Add to the PR checklist when adding or modifying a CLI subcommand:

```
CLI CHANGES ONLY
[ ] --version and --help handled at top-level dispatcher
[ ] Exit codes match the table in ENGINEERING.md §8.2
[ ] --json flag added if structured output is expected by agents
```

---

## 9. Engineering Compliance Checklist

Use before merging any PR:

```
PRE-COMMIT
[ ] Type annotations present on all new public functions
[ ] Named constants for all thresholds/magic numbers
[ ] Module docstring present (if new module)
[ ] No print() in non-CLI modules
[ ] No secrets in code or tests

CI GATES (all must be green)
[ ] Stage 1: Contract tests pass
[ ] Stage 2: mypy zero errors (py3.10/3.11/3.12)
[ ] Stage 2: ruff zero errors
[ ] Stage 2: Unit tests 100% pass
[ ] Stage 2: Coverage ≥ 80%
[ ] Stage 3: Integration tests pass (or skip with explanation)
[ ] Stage 4: bandit zero HIGH
[ ] Stage 4: pip-audit zero CVEs with fixes
[ ] Stage 4: No secrets detected

RETRIEVAL LOGIC CHANGES ONLY
[ ] Benchmark before/after in PR description
[ ] No category regressed below baseline

SCHEMA CHANGES ONLY
[ ] Migration script added under the relevant migrations directory
```

---

## 10. Architecture Patterns

Kairix follows a protocol-driven architecture. Domain boundaries are defined as protocols (interfaces), composed by pipelines, and wired together by factories. Data access lives in repositories. Behavioural variation is handled by registered strategies, not conditional branches.

### 10.1 Protocols

Core protocols live in `kairix/core/protocols.py`; domain-local protocols live in their own modules and are listed below with their location.

**Core protocols (`kairix/core/protocols.py`):**

| Protocol | Responsibility |
|---|---|
| `IntentClassifier` | Classify user query intent (factual, procedural, entity, temporal) |
| `DocumentRepository` | CRUD operations on documents and metadata |
| `GraphRepository` | Entity and relationship storage (Neo4j) |
| `VectorRepository` | Vector index read/write (usearch) |
| `EmbeddingService` | Text-to-vector embedding |
| `FusionStrategy` | Combine ranked lists from multiple search backends |
| `BoostStrategy` | Apply contextual score boosts (entity, temporal, procedural) |
| `ScoringStrategy` | Evaluate retrieval quality against gold documents |
| `SearchLogger` | Structured logging for search operations |
| `CollectionResolver` | Resolve agent + scope to a list of collection names (sprint-19 WS3-2) |
| `AgentRegistry` | Declarative agent → collections mapping for multi-agent retrieval (sprint-19 WS3-3) |

**Domain-local protocols:**

| Protocol | Module | Responsibility |
|---|---|---|
| `ConfidenceParser` | `kairix/agents/research/protocols.py` | Extract a numeric confidence from an LLM response |
| `ContradictionScorer` | `kairix/knowledge/contradict/protocols.py` | Score a (claim, candidate) pair on one contradiction category |
| `ClaimExtractor` | `kairix/knowledge/contradict/protocols.py` | Split content into top-N high-signal claims |
| `SuggestionFilter` | `kairix/knowledge/entities/protocols.py` | Drop, promote, or relabel NER suggestions |
| `ReadinessGate` | `kairix/agents/mcp/readiness.py` | Cold-start readiness signal for the MCP server |

Test compliance: `tests/contracts/test_protocols.py` plus per-protocol contract tests under `tests/contracts/` verify every implementation satisfies its protocol via `isinstance()`.

### 10.2 Pipelines

Pipelines are orchestrators that compose protocols into end-to-end workflows:

| Pipeline | File | Purpose |
|---|---|---|
| `SearchPipeline` | `kairix/core/search/pipeline.py` | Orchestrates classify, retrieve, fuse, boost, and rank |
| `EmbedPipeline` | `kairix/core/embed/pipeline.py` | Document ingestion: chunk, embed, store |
| `BenchmarkPipeline` | `kairix/quality/benchmark/pipeline.py` | Run gold-document evaluations and produce scored results |
| `BriefingPipeline` | `kairix/agents/briefing/pipeline.py` | Agent briefing generation from knowledge store |

### 10.3 Factory

`kairix/core/factory.py` constructs production pipelines at the application boundary. It wires real implementations (Azure embeddings, SQLite, Neo4j, usearch) into pipeline constructors. Test code never calls the factory — tests build pipelines from fakes (`tests/fakes.py`).

### 10.4 Repositories

Repositories own all data access. Production code never issues raw SQL or direct index calls outside a repository.

| Repository | File | Backing store |
|---|---|---|
| `SQLiteDocumentRepository` | `kairix/core/db/repository.py` | SQLite (FTS5 for full-text search) |
| `Neo4jGraphRepository` | `kairix/knowledge/graph/repository.py` | Neo4j (entities and relationships) |
| `UsearchVectorRepository` | `kairix/core/search/vector_repository.py` | usearch (HNSW vector index) |

### 10.5 Strategies

Behavioural variation is handled by registered strategies, not `if/elif` branches.

**Fusion strategies** (`kairix/core/search/fusion.py`):
- `RRFFusion` — Reciprocal Rank Fusion combining BM25 and vector results
- `BM25PrimaryFusion` — BM25-weighted fusion for factual queries

**Boost strategies** (`kairix/core/search/boosts.py`):
- `EntityBoost` — boost documents mentioning query entities
- `ProceduralBoost` — boost procedural/how-to content
- `TemporalBoost` — boost recent or time-relevant documents

**Scoring strategies** (`kairix/quality/eval/scorers.py`):
- `SCORERS` registry — pluggable scorer implementations for benchmark evaluation

**Contradiction scorers** (`kairix/knowledge/contradict/scorers.py`, sprint-19 WS2-B):
- `DirectContradictionScorer` — direct factual contradictions
- `OverstatementScorer` — claims asserting a stronger position than evidence supports
- `StatusMismatchScorer` — different states for the same entity at the same time
- `CompositeContradictionScorer` — composes the three categories; aggregates by max with per-category breakdown

**Confidence parsers** (`kairix/agents/research/confidence.py`, sprint-19 WS2-D):
- `JsonModeConfidenceParser` — parses `{"confidence": float}` JSON; returns `(None, "")` on failure
- `RegexExtractConfidenceParser` — extracts confidence from prose responses; tolerant of "Confidence: 70%" idioms
- `ChainedConfidenceParser` — runs parsers left-to-right; first non-failure wins; logs warning on each fallthrough

**Suggestion filters** (`kairix/knowledge/entities/filters.py`, sprint-19 WS2-E):
- `RolePhraseFilter` — drops role-phrase NER hits ("the regional team", "Senior Director")
- `KnownEntityAllowlist` — promotes pre-loaded entity names that NER missed
- `NerLabelFilter` — corrects mistyped labels via override sets
- `ChainedSuggestionFilter` — left-to-right composition

**Claim extractor** (`kairix/knowledge/contradict/extract.py`, sprint-19 WS2-B):
- `EntityDensityClaimExtractor` — ranks sentences by proper-noun count + modal-verb weighting; returns top-N

**Scope enum** (`kairix/core/search/scope.py`, sprint-19 WS3-1):
- `Scope` — typed multi-agent scope (subclasses `str` for backwards-compat). Five values: `SHARED`, `AGENT`, `SHARED_AGENT`, `ALL_AGENTS`, `EVERYTHING`. Closes the SMELL #7 Primitive Obsession on scope strings.

### 10.6 Adapters

Adapters wrap external services behind protocol interfaces, keeping domain logic decoupled from infrastructure.

| Adapter | File |
|---|---|
| `BM25SearchBackend` | `kairix/core/search/backends.py` |
| `VectorSearchBackend` | `kairix/core/search/backends.py` |
| `AzureEmbeddingService` | `kairix/core/search/backends.py` |
| `JsonlSearchLogger` | `kairix/core/search/logger.py` (sprint-19 XC-1) |
| `DefaultCollectionResolver` | `kairix/core/search/resolver.py` (sprint-19 WS3-2) |
| `ConfigDrivenAgentRegistry` | `kairix/core/search/registry.py` (sprint-19 WS3-3) |
| `EventReadinessGate` | `kairix/agents/mcp/readiness.py` (sprint-19 WS1-5) |

### 10.7 MCP transport composer

`kairix/agents/mcp/transport.py` exposes a single public function:

```python
build_mcp_app(server, *, with_sse=True, sse_mount_path="/sse", healthz_path="/healthz", readiness_check=None) -> Starlette
```

Composes the FastMCP server's `streamable_http_app()` (mounted at `/mcp`) plus `sse_app()` (legacy `/sse`, optional via `with_sse=False`) plus a `/healthz` route into a single Starlette app served via uvicorn. Stateless HTTP per request — gateway timeouts on idle SSE connections are no longer a failure mode (the 2026-05-02 dogfood `-32602` cascade). Tool errors are caught by the public `wrap_tool_errors` decorator in `kairix/agents/mcp/errors.py` and returned as structured `{"error": "<class>: <msg>"}` dicts rather than reaching FastMCP's generic `-32602` mapper. See `docs/operations/MCP-DEPLOYMENT.md` and `docs/operations/MCP-CLIENT-MIGRATION.md` for operator and client-side guidance.

---

## 11. References

| Resource | Location |
|---|---|
| Engineering standards | [`CLAUDE.md`](../../CLAUDE.md) |
| Code quality patterns and boundaries | [`CONSTRAINTS.md`](../../CONSTRAINTS.md) |
| Ralph pattern (agent delegation) | [Engineering hub](https://github.com/three-cubes/engineering-hub/tree/main/ralph) |
