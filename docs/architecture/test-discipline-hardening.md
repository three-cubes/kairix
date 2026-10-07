# Test discipline hardening — Wave 0 lock-in before the connector framework

> **Status**: Implemented. Wave 0 of the connector-framework rollout landed on
> `main`: F45–F48 are canonised in `fitness-functions.md` / `_rule_catalogue.py`
> and enforced per-commit (pre-commit + `scripts/safe-commit.sh` + CI Stage 0,
> with the F48 composed-path E2E selector running in CI Stage 4.5). The three
> seeded baselines (`f30-operator-outcome-tests-files.txt`, `f46-files.txt`,
> `f47-integration-factory-files.txt`) paid down to zero, and PLA-472 removed
> all baseline files: these rules now run over the full tree, every violation
> blocks, and F49 (baseline shrinking) is retired. This document remains the
> canonical spec for the three principles and
> the canonical test shapes; the section below names the testing-discipline gaps
> the original audit found and records how each was closed.
>
> Companion to: `connector-ingestion-architecture.md` (the Wave 1+ feature work
> this hardening gated, now itself implemented), `fitness-functions.md` (F-rule
> canon — F45–F48 live there), `provider-plugin-architecture.md` (the pattern
> this hardening preserves quality for at scale).

## 1. Why this landed first

The pre-Wave-0 audit returned a precise picture: ceremony was good, composition was unproven. Wave 0 closed every gap below before the connector framework dispatched; this section is preserved as the record of what was wrong and how each gap was mechanically locked shut.

**What already worked at audit time**:
- F12 (BDD happy-path), F13 (no impl leaks), F28 (provider plugin BDD parity) — all green, all baseline-zero.
- 90 `.feature` files across CLI, MCP, search, providers, transport.
- 66 integration tests on real SQLite + FTS5 + DocumentScanner-built fixtures.
- Canonical fakes regime (`tests/fakes.py`) is universal; no monkey-patching (F1 enforced).
- mypy strict, ruff, ≥90% per-file coverage (F7).

**What the audit named as broken — and how Wave 0 closed it** (every item below is now resolved and held mechanically):
- **`tests/e2e/` was empty** (`__init__.py` only) — no filesystem-level end-to-end test existed. *Closed by F48:* `tests/e2e/test_composed_production_path.py` is the canonical exemplar and many sibling capability paths now live alongside it (connectors, setup wizard, install, entity-summary, recommender, etc.).
- **Only 1 integration test** used `kairix.core.factory` composition; the other 65 built pipelines ad-hoc, leaving the production composition mostly unexercised. *Closed by F47:* the integration suite now builds multi-component pipelines through `kairix.core.factory.build_*`; the `f47-integration-factory-files.txt` baseline has paid down to zero.
- **No test did config → real provider → ingest → query → assertion** through the composed production code — the exact gap the Plan-B-parity / LoCoMo post-mortem (5233 green tests; 5% real recall) named. *Closed by F48* (composed production path E2E running in CI Stage 4.5).
- **The F30 baseline carried 35 exempted entries** — CLI subcommands + MCP tools without subprocess / direct-handler outcome tests. *Closed:* `f30-operator-outcome-tests-files.txt` has paid down to zero; every CLI subcommand and MCP tool now carries an outcome test, and net-new surfaces hard-fail the gate without one.
- **No rule forced new capabilities to ship with a `.feature` file** — F12 only governed the content of features that already existed, so a new CLI subcommand could land with zero behaviour spec. *Closed by F45.*
- **No rule forced BDD step impls or integration tests through `factory.build_*`** — F13 caught negative leakage (no Mock in scenarios) but not the positive requirement (real production path exercised). *Closed by F46 (BDD steps) and F47 (integration tests); the `f46-files.txt` baseline has paid down to zero.*

The connector framework (per `connector-ingestion-architecture.md`) added ~6 connectors, ~6 extractors, Bronze + Silver shared infrastructure, and a new worker integration. Building it on top of the gaps above would have replicated the LoCoMo failure mode across every ingest path. Wave 0 closed the gaps first; the connector framework dispatched only once Wave 0 was on `main`.

## 2. Three principles

The whole hardening pass collapses to three rules every contributor (human or agent) should be able to state from memory:

### 2.1 Composition principle

> BDD step implementations AND multi-component integration tests construct their pipelines through `kairix.core.factory.build_*`, never by direct `SearchPipeline(...)` / `EmbedPipeline(...)` / `ConnectorPipeline(...)` construction.

Direct construction is reserved for two narrow cases: `tests/contracts/` (Protocol shape proofs that don't exercise composition) and `tests/integration/test_<x>_contract.py` (single-layer boundary proofs that intentionally bypass composition to validate one adapter). Everything else goes through the factory with `paths=FakePaths(...)` and any other DI seams the factory exposes.

Why: the factory IS the production composition. A test that doesn't go through it isn't testing what production runs. Plan-B-parity is exactly this failure mode.

Locked by **F46** (BDD steps) and **F47** (integration tests).

### 2.2 Real-path principle

> At least one test exists, runs in CI, and is marked `@pytest.mark.e2e` that exercises: config load → `factory.build_*` → ingest of a fixture document → query through the real composed pipeline → assertion that the ingested document is retrievable.

This is the test that would have failed loudly during Plan-B-parity. It uses real SQLite (tmpdir), real schema, real factory, real composed pipeline. Provider can be a `FakeProvider` that exercises the real HTTP-wrapper code (so transport/pool/retry/cache run), or a stubbed-network real provider — whichever keeps the test air-gapped without bypassing layers.

Every new top-level capability (provider plugin, connector, extractor, retrieval mode) gets a sibling E2E test in the same wave. For Wave 1+ connectors: `tests/e2e/test_composed_connector_path.py`, etc.

Locked by **F48**.

### 2.3 New-capability principle

> Shipping a new CLI subcommand, MCP tool, provider plugin, connector plugin, or extractor plugin requires a `tests/bdd/features/*.feature` AND an outcome test in the same commit. Pre-commit blocks otherwise.

Why: F12 governs content of existing features but a new capability can ship today with no feature file at all. F30 catches missing outcome tests but only after the surface is in `COMMANDS` or `@server.tool()`. F45 closes the window between "code lands" and "behaviour spec lands" to zero commits.

Locked by **F45** (BDD feature presence) and **F30** (outcome test presence — extended to cover plugins per F36 / F43 from the connector doc).

## 3. Fitness functions F45–F49

Each follows the F21 action-marked-failure template (`fix:` / `next:` / `run:`), runs over the full tree with no baseline file, and wires into pre-commit + `scripts/safe-commit.sh` + CI Stage 0.

### F45 — New capability ships with a BDD feature

**Rule**: any commit that adds a CLI subcommand (a new row in `kairix/cli.py:COMMANDS`), an MCP tool (a new `@server.tool()` decorated function in `kairix/agents/mcp/server.py`), a provider plugin (`make_provider` symbol in a new `kairix/providers/<name>/__init__.py`), a connector plugin (`make_connector` symbol in a new `kairix/connectors/<name>/__init__.py`), or an extractor plugin (`make_extractor` symbol in a new `kairix/extractors/<name>/__init__.py`) must add a matching `tests/bdd/features/*.feature` in the same commit.

**Detection**: pre-commit hook diffs the staged changes:
- Parses `kairix/cli.py:COMMANDS` and detects net-new keys.
- Parses `kairix/agents/mcp/server.py` for net-new `@server.tool(...)` decorators.
- Detects net-new `make_provider` / `make_connector` / `make_extractor` factory symbols under the three plugin trees.
- For each detected surface, asserts a matching `.feature` file landed in the same staged set.

Naming convention: `tests/bdd/features/{cli_<name>,mcp_<tool>,provider_<name>,connector_<name>,extractor_<name>}.feature`. The check accepts either the convention or an explicit `# F45-feature: <path>` comment in the surface file pointing at the feature.

**Failure text**: `F45: new surface <surface> introduced without a .feature file. fix: add tests/bdd/features/<convention>.feature with a happy-path scenario. next: see docs/architecture/test-discipline-hardening.md §2.3 (new-capability principle).`

**Baseline**: none — every violation blocks.

### F46 — BDD step impls call factory-composed production code

**Rule**: step implementations under `tests/bdd/steps/*.py` must, somewhere in their call graph (depth ≤ 2), invoke one of:
- A CLI entry point: `kairix.cli.main` OR a per-subcommand `main(...)` function under `kairix/**/cli.py`.
- An MCP tool function: the callable wrapped by a `@server.tool()` decorator.
- A factory constructor: `kairix.core.factory.build_search_pipeline`, `build_embed_pipeline`, etc.

Direct construction of `SearchPipeline(...)`, `EmbedPipeline(...)`, `ConnectorPipeline(...)` in a step file is disallowed.

**Detection**: AST scan of `tests/bdd/steps/*.py`. For each step body, build the call graph (depth 2 via name resolution + simple inline-function tracing). If no sanctioned entry point appears, flag.

**Failure text**: `F46: tests/bdd/steps/<file>.py constructs a pipeline directly instead of going through the factory. fix: use factory.build_search_pipeline(paths=FakePaths(...)) — see tests/integration/test_vec_index_lifecycle.py for the canonical pattern. next: see docs/architecture/test-discipline-hardening.md §4.1 (canonical factory shape).`

**Baseline**: none. The initial AST scan found 6 files; they were paid down to zero (all step files route through `factory.build_*`). Every violation hard-fails the gate.

### F47 — Integration tests build through the factory

**Rule**: tests under `tests/integration/` that import any pipeline class (`SearchPipeline`, `EmbedPipeline`, `ConnectorPipeline`, `IngestPipeline`, etc.) must construct it via `kairix.core.factory.build_*` with a `paths=FakePaths(...)` and any required `db_factory` / `embed_service` / etc. injection seams.

Allowed exceptions:
- `tests/contracts/` — Protocol shape proofs that don't exercise composition.
- `tests/integration/test_*_contract.py` — single-layer boundary proofs that intentionally bypass composition.

**Detection**: AST scan of `tests/integration/test_*.py`. Flag files that import a `*_Pipeline` class AND construct it directly (call expression of the class name), unless the file name matches `*_contract.py`.

**Failure text**: `F47: tests/integration/<file>.py constructs <Pipeline> directly. fix: use kairix.core.factory.build_<pipeline>(paths=FakePaths(...)). next: see tests/integration/test_vec_index_lifecycle.py for the canonical pattern, and docs/architecture/test-discipline-hardening.md §4.2.`

**Baseline**: none. The initial scan found many violations (only 1 integration test used the factory at audit time); they were paid down to zero. Every violation hard-fails the gate.

### F48 — Composed production path E2E test exists and runs

**Rule**: `tests/e2e/test_composed_production_path.py` must exist, must carry `@pytest.mark.e2e` on at least one test function, must be runnable as `pytest -m e2e tests/e2e/test_composed_production_path.py`, and must exercise: config load → `factory.build_search_pipeline(...)` (or other composition) → ingest of a fixture document via the production ingest path → query through the real composed pipeline → assertion that the ingested document is retrievable.

For Wave 1+: every new top-level capability (provider, connector, extractor, retrieval mode) lands with a sibling `tests/e2e/test_composed_<capability>_path.py` in the same wave.

**Detection**: file presence + AST check for `@pytest.mark.e2e` + CI invocation of `pytest -m e2e` in a dedicated stage. Action-marked failure on missing file. Action-marked failure on non-zero exit from the `e2e` test selector in CI.

**Failure text**: `F48: tests/e2e/test_composed_production_path.py is missing or has no @pytest.mark.e2e test. next: write the test per docs/architecture/test-discipline-hardening.md §4.3 (canonical E2E shape).`

**Baseline**: not applicable — binary presence check.

### F49 — Test-discipline baselines shrink per release (retired)

Retired in PLA-472. F49 made each release shrink the F30 / F46 / F47 baseline files. All three reached zero, and PLA-472 removed baseline files entirely, so there is nothing left to shrink — those rules now block every violation directly.

## 4. Canonical patterns (the shapes contributors must match)

### 4.1 Canonical BDD step impl

Counter-example (current shape, F46-violating):

```python
# tests/bdd/steps/search_cli_steps.py — current shape, flagged by F46
def _make_search_fn():
    pipeline = SearchPipeline(            # direct construction — F46 violation
        document_repository=FakeDocumentRepository(...),
        vector_repository=FakeVectorRepository(...),
        embedding_service=FakeEmbeddingService(...),
        ...
    )
    return pipeline.run
```

Canonical shape (F46-clean):

```python
# tests/bdd/steps/search_cli_steps.py — canonical shape
from kairix.core.factory import build_search_pipeline
from tests.fakes import FakePaths, FakeEmbeddingService

@given("a configured search environment with fixture corpus")
def configured_search_environment(context):
    paths = FakePaths.with_fixture_corpus("reflib_fixture")
    context.pipeline = build_search_pipeline(
        paths=paths,
        embedding_service=FakeEmbeddingService.recording(),
    )

@when('I search for "{query}"')
def search_for(context, query):
    context.result = context.pipeline.run(query=query)
```

The step calls the factory; the factory wires real pipeline + adapters; fakes only at the network/storage boundary. The same shape works for `build_embed_pipeline`, `build_connector_pipeline`, etc.

### 4.2 Canonical integration test

Counter-example (current shape, F47-violating):

```python
# tests/integration/test_boosts_integration.py — current shape, flagged by F47
def test_boost_ordering(db, fixture_corpus):
    pipeline = SearchPipeline(                # direct construction — F47 violation
        document_repository=DocumentRepository(db),
        vector_repository=VectorRepository(db),
        boosts=[ChunkDateBoost(), EntityBoost()],
        ...
    )
    result = pipeline.run("query")
    assert ...
```

Canonical shape (F47-clean):

```python
# tests/integration/test_boosts_integration.py — canonical shape
from kairix.core.factory import build_search_pipeline
from tests.fakes import FakePaths

def test_boost_ordering(tmp_path, fixture_corpus_at):
    paths = FakePaths(root=tmp_path).with_corpus(fixture_corpus_at("reflib"))
    pipeline = build_search_pipeline(paths=paths)
    result = pipeline.run("query")
    assert result.documents[0].chunk_date_boost > 0
```

The factory's signature exposes whatever overrides the test needs (boosts, embedders, vector backends). Tests that exercise a single layer in isolation belong in `tests/integration/test_<layer>_contract.py` and are exempt from F47.

### 4.3 Canonical E2E test

```python
# tests/e2e/test_composed_production_path.py — canonical shape (F48)
"""End-to-end composed production path test.

Exercises: config load → factory.build_* → real ingest → real query → assertion.
This is the test that would have failed during Plan-B-parity. Every new
top-level capability gets a sibling test/e2e/test_composed_<capability>_path.py.
"""
import pytest
from kairix.core.factory import build_ingest_pipeline, build_search_pipeline
from kairix.paths import KairixPaths
from tests.fakes import FakePaths, FakeProvider

@pytest.mark.e2e
def test_composed_production_path(tmp_path):
    paths = FakePaths(root=tmp_path)                       # real schema, tmpdir
    provider = FakeProvider.with_recorded_embeddings(...)  # real HTTP wrapper

    ingest = build_ingest_pipeline(paths=paths, provider=provider)
    ingest.ingest_document(
        text="The Plan B-parity post-mortem identified this gap.",
        source_uri="test://fixtures/post_mortem.md",
    )

    search = build_search_pipeline(paths=paths, provider=provider)
    result = search.run(query="Plan B-parity post-mortem")

    assert result.documents, "ingest succeeded but query returned no documents"
    assert "post_mortem.md" in result.documents[0].source_uri, (
        "query returned documents but not the ingested one"
    )
```

Two asserts; both load-bearing. The first is the canary for "did the ingest path even write something queryable"; the second is the canary for "did the search path retrieve what we just wrote." Either failing is a Plan-B-parity-class regression.

### 4.4 `conftest.py` affordance

W0-6 adds an `e2e_db` fixture in `tests/conftest.py` that builds the tmpdir+schema+factory wiring in one line, so F48 tests are short:

```python
@pytest.fixture
def e2e_db(tmp_path) -> KairixPaths:
    """One-line E2E setup: real schema in tmpdir; factory-ready."""
    paths = FakePaths(root=tmp_path)
    paths.initialise_schema()
    return paths
```

> Re-tiering note: a per-function `@pytest.mark.soak`/`@pytest.mark.slow` stacks on top of a module-level `pytestmark = pytest.mark.unit` rather than replacing it, so the test still runs on the per-commit path. To move a test to a slower tier put it in a dedicated module with the tier marker at module scope. See [`ENGINEERING.md §3.7`](ENGINEERING.md) (test cost and isolation hygiene).

## 5. F30 paydown plan — full, not triaged (completed)

Per the standing direction ("we need to get it all done; let's continue to lift the codebase standard as we are only going to keep moving quicker"), the F30 baseline paid down **to zero** in Wave 0, not in phases (the baseline file itself was removed in PLA-472). Every CLI subcommand and MCP tool has an outcome test, and any surface without one hard-fails the gate. The original 35-entry baseline, grouped for the parallel dispatch that cleared it, is retained below as the paydown record and as the canonical outcome-test pattern reference.

Original baseline (35 entries) grouped for parallel dispatch:

### Group A — Agents (5 CLIs)
```
kairix/agents/briefing/cli.py
kairix/agents/curator/cli.py
kairix/agents/mcp/cli.py
kairix/agents/research/cli.py
kairix/agents/usage_guide/cli.py
```

### Group B — MCP tools (10 tools)
```
kairix/agents/mcp/server.py/@tool:benchmark_run
kairix/agents/mcp/server.py/@tool:bootstrap
kairix/agents/mcp/server.py/@tool:contradict
kairix/agents/mcp/server.py/@tool:embed
kairix/agents/mcp/server.py/@tool:embed_rebuild_fts
kairix/agents/mcp/server.py/@tool:prep
kairix/agents/mcp/server.py/@tool:probe_config
kairix/agents/mcp/server.py/@tool:search
kairix/agents/mcp/server.py/@tool:store_crawl
kairix/agents/mcp/server.py/@tool:warm
```

### Group C — Core (5 CLIs)
```
kairix/core/classify/cli.py
kairix/core/embed/cli.py
kairix/core/search/cli.py
kairix/core/search/config_validator.py
kairix/core/temporal/cli.py
```

### Group D — Knowledge (6 CLIs)
```
kairix/knowledge/contradict/cli.py
kairix/knowledge/entities/cli.py
kairix/knowledge/reflib/cli.py
kairix/knowledge/store/cli.py
kairix/knowledge/summaries/cli.py
kairix/knowledge/wikilinks/cli.py
```

### Group E — Platform (3 CLIs)
```
kairix/platform/onboard/cli.py
kairix/platform/setup/cli.py
kairix/platform/warm/cli.py
```

### Group F — Quality (4 CLIs; `probe/cli.py` + `soak/cli.py` retired in v2026.6, replaced by `kairix mcp-calls` + `kairix caches` top-level surfaces and the `kairix benchmark run --mode concurrent|soak` dispatcher)
```
kairix/quality/benchmark/cli.py
kairix/quality/probe/config_cli.py
kairix/quality/probe/mcp_calls_cli.py
kairix/quality/probe/caches_cli.py
```

### Group G — Top-level CLIs (1 CLI; `bootstrap_cli.py` paid down 2026-05-22, commit `2334a49d`)
```
kairix/worker_cli.py
```

Each group dispatches as one worktree. Per F30's existing contract: each new outcome test invokes via `subprocess.run([sys.executable, "-m", "kairix.cli", "<sub>", ...])` (CLI) or calls the MCP tool handler directly (MCP), and asserts on `.stdout` / `.stderr` / returned-envelope content — not on `returncode == 0` alone, not on internal fake call-counts.

**Probe-validated pattern** (from `kairix/bootstrap_cli.py` paydown, commit `2334a49d`):

1. **Inspect the CLI's existing seams.** Most use-case-backed CLIs already have a `Deps` dataclass (`BootstrapDeps`, `BriefDeps`, `UsageGuideDeps` — the pattern is widespread). The seam is usually there; only the CLI-flag is missing.
2. **Add `--document-root PATH` (and `--db-path PATH` / `--config PATH` where the use case demands them)** as additive argparse args. Match the canonical pattern from `kairix/knowledge/store/cli.py` — `--document-root` wins when supplied; otherwise the existing env / config / default chain runs unchanged.
3. **Plumb the flag into the existing `Deps` seam** inside `main()`. When the flag is supplied AND no explicit `deps=` was injected by an in-process caller, build `<Deps>(document_root_fn=lambda: Path(args.document_root), ...)`. In-process callers (existing unit tests) keep winning; the new flag is the subprocess seam.
4. **Use `sys.executable -m kairix.cli <sub>`** in `subprocess.run` (NOT `-m kairix`; `kairix.__main__` doesn't exist as a separate module — `kairix = "kairix.cli:main"` is the console-script entry).
5. **Two tests per surface**: one happy-path envelope assertion (returncode + stdout JSON parse + content keys), one error-path assertion (returncode non-zero + stderr error prefix).
6. **Sabotage-proof both tests** before commit: mutate the production code path the test is supposed to cover, confirm both tests fail, restore.
7. **Run F30** (`uv run tc-fitness run` or `safe-commit.sh`) and confirm it passes. There is no baseline file to edit.

Canonical CLI outcome-test shape:

```python
# tests/integration/test_outcome_<name>_cli.py
import subprocess
import pytest

@pytest.mark.integration
def test_<name>_cli_happy_path(e2e_db):
    result = subprocess.run(
        ["kairix", "<sub>", "--required-arg", "value"],
        capture_output=True, text=True,
        env={**os.environ, "KAIRIX_DATA_DIR": str(e2e_db.data_dir)},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "<expected substring>" in result.stdout
    assert "ERROR" not in result.stderr
```

Canonical MCP outcome-test shape:

```python
# tests/integration/test_outcome_mcp_<tool>.py
import pytest
from kairix.agents.mcp.server import build_server

@pytest.mark.integration
def test_<tool>_outcome(e2e_db):
    server = build_server(paths=e2e_db)
    envelope = server.tools["<tool>"].run(arg1="x", arg2="y")
    assert envelope["status"] == "ok", envelope.get("error")
    assert "<expected key>" in envelope["result"]
```

Each test asserts on actual output, not on the absence of errors. The 35 paydown tests landed alongside the F48 composed-path test; together they constitute the production-path coverage that Plan-B-parity proved was missing.

## 6. Wave 0 work breakdown (completed)

Wave 0 shipped as **10 work items** dispatched per the project's subagent playbook (parallel worktrees + cherry-pick), with W0-1 foreground first and W0-10 picking up the references after the rest landed. All ten items are on `main`.

| Item | Description | Sequencing | Parallel? |
|---|---|---|---|
| **W0-1** | This document on `main` | foreground | — |
| **W0-2** | `tests/e2e/test_composed_production_path.py` (F48 exemplar) | after W0-1 | yes |
| **W0-3** | F45 — `scripts/checks/check_f45_new_capability_bdd.py` + pre-commit + baseline (empty) | after W0-1 | yes |
| **W0-4** | F46 — `scripts/checks/check_f46_bdd_step_composition.py` + pre-commit + baseline (seeded) | after W0-1 | yes |
| **W0-5** | F47 — `scripts/checks/check_f47_integration_factory.py` + pre-commit + baseline (seeded) | after W0-1 | yes |
| **W0-6** | F48 — `scripts/checks/check_f48_e2e_present.py` + CI Stage 4.5 (`pytest -m e2e`) + `e2e_db` fixture in `tests/conftest.py` | after W0-2 | yes |
| **W0-7** | F49 — `scripts/checks/check_baseline_shrinking.py` + `release.yml` integration | after W0-1 | yes |
| **W0-8** | F30 full paydown — Groups A through G in seven parallel worktrees; baseline file pays down to zero | after W0-1 | yes (7 sub-worktrees) |
| **W0-9** | CLAUDE.md edits (§How to test rewrite; §Architecture fitness functions appendix; §Docs resolver row) | after W0-2..W0-8 | foreground |
| **W0-10** | Update `docs/architecture/connector-ingestion-architecture.md` to reference F45–F49 and the E2E exemplar; update `docs/architecture/fitness-functions.md` with F45–F49 canonical entries | after W0-9 | foreground |

W0-1 landed first. W0-2..W0-8 ran as concurrent worktrees once W0-1 was on `main`; W0-8 itself ran as 7 sub-worktrees (Groups A–G). W0-9 and W0-10 picked up the consolidated state afterward.

Actual wall-clock: roughly 1 working week with the parallel dispatch playbook, as estimated.

**Wave 1 (connector scaffold per `connector-ingestion-architecture.md`) dispatched only once W0-10 was on `main` and all F45–F49 baselines were at the target state — that gate is satisfied and the connector framework has since shipped.**

## 7. CLAUDE.md edits (W0-9 — landed; recorded here so contributors can trace them)

These edits are now live in `CLAUDE.md` (the "How to test" section and the fitness-function enumeration both carry F45–F49). The originally-specified text is preserved below as the change record.

### §"How to test" — replaces the existing one-paragraph section

```markdown
## How to test

Test with fakes from `tests/fakes.py`, not monkey-patches. Construct pipelines
through `kairix.core.factory.build_*`, not by direct `SearchPipeline(...)` /
`EmbedPipeline(...)` construction.

**Three principles, all mechanically enforced:**

- **Composition (F46 / F47)** — BDD step impls and multi-component integration
  tests go through the factory with `paths=FakePaths(...)` and any other
  injection seams the factory exposes. Direct pipeline construction is
  reserved for `tests/contracts/` and `tests/integration/test_<x>_contract.py`.
- **Real path (F48)** — `tests/e2e/test_composed_production_path.py` exists,
  is `@pytest.mark.e2e`, runs in CI Stage 4.5, and exercises config →
  factory.build → ingest → query → assertion against composed production
  code. Every new top-level capability gets a sibling
  `tests/e2e/test_composed_<capability>_path.py` in the same wave.
- **New capability (F45)** — shipping a new CLI subcommand, MCP tool,
  provider plugin, connector plugin, or extractor plugin requires a
  `tests/bdd/features/*.feature` AND an outcome test in the same commit.
  Pre-commit blocks otherwise.

See `tests/contracts/test_protocols.py` for protocol compliance patterns;
`tests/integration/test_vec_index_lifecycle.py` for the canonical factory
shape; `docs/architecture/test-discipline-hardening.md` for the full
specification.
```

### §"Architecture fitness functions" — append F45–F49 to the existing list

One line per rule, same shape as the existing F1–F33 + G1–G10 enumeration. The canonical text lives in `docs/architecture/fitness-functions.md` (updated in W0-10).

### §"Docs — agent-actionable resolver" §3 row

Append to the Engineering practices table:

```markdown
| Land a new top-level capability with its discipline carrying | **[`docs/architecture/test-discipline-hardening.md`](docs/architecture/test-discipline-hardening.md)** — F45..F49, the three principles, canonical test shapes |
```

## 8. What this document is *not*

- **Not a rewrite of the existing F1–F33 regime.** F12, F13, F28, F30 stay as they are; F45–F48 are additive and close the gaps the audit named (F49 was too, until PLA-472 retired it).
- **Not a refactor of every existing BDD step or integration test.** F46 and F47 originally seeded baselines that were paid down to zero; since PLA-472 there are no baselines and every test is held to the bar.
- **Not a replacement for the connector-ingestion architecture doc.** That doc owns Wave 1+ feature work; this doc owns the discipline that Wave 1+ runs on top of.
- **Not the place F45–F48 are formally canonised.** The canonical home is `docs/architecture/fitness-functions.md` and `scripts/checks/_rule_catalogue.py`; W0-10 landed the entries there. This document is the spec/rationale, not the rule registry.

## 9. References

- `docs/architecture/connector-ingestion-architecture.md` — Wave 1+ feature work this hardening gated (now implemented)
- `docs/architecture/fitness-functions.md` — F-rule canon (F45–F48 live here; landed in W0-10)
- `docs/architecture/provider-plugin-architecture.md` — the pattern this discipline preserves at scale
- `tests/integration/test_vec_index_lifecycle.py` — canonical factory-composition shape
- `tests/e2e/test_composed_production_path.py` — the F48 composed-path E2E exemplar
- `tests/fakes.py` — canonical fakes (Protocol-compliant, no monkey-patching)
- Architectural context: the two-scope architecture (engagement vs firm)
  and the Python-only language strategy that drive this discipline
