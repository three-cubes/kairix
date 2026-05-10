# Quality-exceptions refactor plan

**Goal:** drive every grandfathered exception in the kairix quality
pipeline to zero. Each exception is a place where a rule was relaxed —
each removal closes a drift surface and tightens the gate.

**Baseline (2026-05-10, this audit):**

| Surface | Count | Where |
|---|---:|---|
| Architecture-fitness baselines (F-rules) | **169 entries** across 9 files | `.architecture/baseline/*.txt` |
| Per-line suppressions in production | **94** | `# pragma: no cover` (36), `# noqa` (25), `# NOSONAR` (21), `# type: ignore` (10), `# nosec` (2) |
| Per-line suppressions in tests | **133** | `# pragma: allowlist secret` (52), `# type: ignore` (39), `# noqa` (19), `# NOSONAR` (18), `# pragma: no cover` (5) |
| Test skip mechanisms | **5** | `pytest.mark.skipif` (1, E2E env-gated) + `pytest.importorskip` (4, optional deps) |
| `continue-on-error: true` in CI workflows | **4** | PR-comment + dependency-review (operationally fine) + 2 artifact-download (path-filter symptoms) |
| SonarCloud issue exclusions | **5** ignore-rules | `sonar-project.properties` |
| Codecov omit list | **8 files** | `pyproject.toml [tool.coverage.run] omit` |

**Total grandfathered surface: ~325 line-level exceptions + 47 file-level allowances.**

## Triage tiers

Each exception is graded by removal viability:

- **🟢 Tier 1 — Trivially removable.** Stale entry, rule no longer fires, file no longer exists. Verify and delete.
- **🟡 Tier 2 — Workable with focused effort.** Need a test, an extracted helper, or a small refactor. Bounded.
- **🔵 Tier 3 — Architecturally needed.** The rule is wrong for our shape; keep the exception but document the rationale where it lives. No change planned.
- **🟠 Tier 4 — Investigation needed.** Each instance needs case-by-case judgement. May span Tier 1-3.

## Phase 1 — Audit + quick wins (2-3 hours, no scope risk)

Mechanical pass to remove obvious dead-weight before any refactor.

- [ ] **Stale baseline entries.** Run `for f in .architecture/baseline/*.txt; do cat "$f" | while read line; do [ ! -f "$line" ] && echo "STALE in $f: $line"; done; done` — drop entries pointing at deleted/renamed files.
- [ ] **Stale `# pragma: no cover`** in production. Many were added during defensive coding; some lines are now reachable through the use-case work. Strip every `# pragma: no cover`, run coverage, re-add only those that actually drop coverage below floor.
- [ ] **Stale `# noqa`.** Strip every `# noqa` and run `ruff check`; re-add only those that actually fire.
- [ ] **Stale `# type: ignore`.** Same — strip and run `mypy --strict`; re-add only the genuinely-needed ones.
- [ ] **The 2 artifact-download `continue-on-error: true` in `ci.yml:538,544`.** Replace with conditional steps that only download when the path-filter signaled the artifact was produced. Removes the silencer, keeps the path-filter behaviour.
- [ ] **The dependency-review `continue-on-error: true` (`ci.yml:502`).** Operationally correct — transitive CVEs may not have fixes — but should be paired with a Dependabot policy that auto-fails when a fixable CVE is ignored. **Decide:** accept as Tier 3 (document the policy intent in a CONSTRAINTS entry) OR move to a separate scheduled audit job that DOES block. Recommend keeping but adding the rationale to `CONSTRAINTS.md`.
- [ ] **The PR-comment `continue-on-error: true` (`ci.yml:304`).** Document as Tier 3 — fork PRs without secrets can't post comments; failure is not a code-quality signal. Add rationale in CONSTRAINTS.

**Expected outcome:** ~30-50 exceptions removed (the stale ones), 2-4 architectural-Tier-3 entries documented, no new tests needed.

## Phase 2 — Coverage-floor backfill (4-6 hours, mechanical)

Resolves: F7 (39 entries), F9 (36 entries), Codecov omit (8 files), production `# pragma: no cover` (36 instances).

The pattern from this session's CLI-extraction work: **pull pure helpers out of CLI orchestrators, unit-test the helpers, leave the orchestrator as a thin shell**. Already applied to `kairix/agents/briefing/cli.py`, `kairix/core/temporal/cli.py`, `kairix/core/search/cli.py`, etc.

### 2a — Extract + test the F7 grandfathered files

Group by effort. Each file is roughly 1-2 hours.

- **Easy wins (~30 min each):** files where the orchestrator is small and helpers are obvious.
  - `kairix/__init__.py`, `kairix/cli.py` (top-level dispatch), `kairix/credentials.py`, `kairix/secrets.py`, `kairix/core/embed/deps.py`
- **Medium (~1-2h each):** CLI orchestrators with 100-300 LOC.
  - `kairix/knowledge/entities/cli.py` (40.6%), `kairix/knowledge/summaries/cli.py` (50%), `kairix/agents/curator/cli.py` (83.3% — almost there), `kairix/knowledge/store/cli.py`, `kairix/quality/eval/cli.py`, `kairix/agents/briefing/sources.py`, `kairix/platform/setup/wizard.py`
- **Architectural (≥2h each):** non-CLI files where coverage gaps reflect real production-only paths.
  - `kairix/core/factory.py` (75.3%) — production wiring; testable through pipeline-builder contract tests
  - `kairix/core/db/repository.py` (63.7%) — wraps SQLite; tests through fakes
  - `kairix/core/search/{config_validator,rerank,vector_repository}.py` — each has its own shape
  - `kairix/core/temporal/index.py` (80.2%) — needs more `query_temporal_chunks` coverage

### 2b — Codecov omit list

The 8 files explicitly excluded from coverage measurement need a different strategy:

- `kairix/_azure.py`, `kairix/knowledge/graph/client.py` — **Tier 3.** External-I/O adapters; integration tests can hit the boundary but unit-coverage-of-adapter-internals is theatre. Document in `pyproject.toml` as permanent omits with rationale (already done; just confirm the rationale is durable).
- `kairix/knowledge/contradict/cli.py`, `kairix/knowledge/reflib/cli.py`, `kairix/platform/onboard/cli.py`, `kairix/quality/benchmark/cli.py` — **Tier 2.** CLIs without test coverage yet. Apply the helper-extraction pattern. **Move OUT of the omit list once tests land.**
- `kairix/knowledge/wikilinks/audit.py` — **Tier 2.** "0% coverage today, no public callers in tests." Either backfill or delete.

### 2c — Defensive `# pragma: no cover` in production

The 36 production `# pragma: no cover` lines are mostly defensive `except` branches for "shouldn't happen" cases. Two strategies:

- For genuinely unreachable lines (e.g. typing-only branches): convert to `if TYPE_CHECKING:` or remove the dead branch entirely.
- For "reachable only when external system is broken" (e.g. `except ImportError` for an optional dependency): keep `# pragma: no cover` but add a rationale comment.

**Hot files** (most `# pragma: no cover`):
- `kairix/core/embed/recall_check.py` (7), `kairix/core/embed/embed.py` (6), `kairix/quality/eval/gold_builder.py` (5), `kairix/quality/eval/monitor.py` (4), `kairix/quality/benchmark/runner.py` (3)

**Expected outcome:** F7 baseline → ≤10 entries, F9 baseline → ≤10 entries, production `# pragma: no cover` → ≤10 instances, codecov omit list → 2 entries (`_azure.py`, `graph/client.py`).

## Phase 3 — Eliminate test-side anti-patterns (4-6 hours)

Resolves: F1 (3 files), F2 (9 files), F5 (13 files), F12 (2 files), `# NOSONAR` in tests (18).

This is the "no monkeypatch, no internal imports, no @patch on kairix" cleanup that the project's memory feedback explicitly bans for new code. Each baseline entry is a legacy violation that pre-dates the rule.

### 3a — F1 (3 files): no `@patch` on kairix internals

- `tests/graph/test_upsert_edge.py`, `tests/integration/test_eval_auto_gold_cli.py`, `tests/integration/test_mcp_tool_contracts.py`

Each test patches a real kairix symbol. Refactor to use the canonical fakes in `tests/fakes.py`. Effort: ~30 min per file.

### 3b — F2 (9 files): no `monkeypatch.setenv("KAIRIX_*")`

- `tests/conftest.py`, `tests/eval/test_retrieval_config_resolution.py`, `tests/fakes.py`, `tests/integration/conftest.py`, `tests/search/test_config_loader_contracts.py`, `tests/search/test_config_loader.py`, `tests/test_agent_memory_path_regression.py`, `tests/test_paths.py`, `tests/test_secrets.py`

Each test sets `KAIRIX_*` env vars to drive behaviour. Refactor to inject `FakePaths` / explicit ctx via constructor. Already-established pattern from this session's use-case work. Effort: ~30-60 min per file.

### 3c — F5 (13 files): no internal-name imports in tests

- `tests/bdd/steps/recall_steps.py`, `tests/briefing/test_pipeline.py`, `tests/contracts/test_eval_protocols.py`, `tests/eval/test_generate.py`, `tests/mcp/test_affordance.py`, `tests/reflib/test_extract.py`, `tests/reflib/test_normalise.py`, `tests/search/test_collection_config.py`, `tests/search/test_config_loader.py`, `tests/store/test_crawler.py`, `tests/summaries/test_generate.py`, `tests/test_paths.py`, `tests/wikilinks/test_resolver.py`

Each imports a `_`-prefixed symbol. Drive the same branch through the public surface OR delete the test if the public surface doesn't reach it (= dead code). Effort: ~30-60 min per file.

### 3d — F12 (2 files): BDD without happy-path scenario

- `tests/bdd/features/benchmark_run.feature`, `tests/bdd/features/summarise_cli.feature`

Add a happy-path scenario to each. Effort: ~15-30 min per file.

### 3e — Test-side `# NOSONAR` (18) and `# type: ignore` (39)

Audit each. Many are legitimate (e.g. `# NOSONAR — BDD captures CLI exit code; reraising would defeat the test`). Some are stale and can be stripped. Estimate: 50% removable, 50% Tier-3 with documented rationale.

**Expected outcome:** F1/F2/F5/F12 baselines → 0 entries, test-side suppressions ≤30.

## Phase 4 — Eliminate test-only kwargs in production (3-4 hours)

Resolves: F6 (14 files).

The Phase 1/2/3 use-case extraction work in this session showed the canonical pattern — each `*_fn=None` in production becomes a typed `Deps` dataclass injection point at the boundary. The 14 baseline entries pre-date that pattern.

### 4a — Already-converted use cases (this session)

The following ALREADY use typed Deps and shouldn't have `*_fn=None` kwargs:
- `kairix/agents/briefing/pipeline.py` — has `BriefDeps`? Check.
- `kairix/agents/mcp/server.py` — adapters use typed `deps: Any` now; verify all `*_fn=None` are gone.
- `kairix/agents/research/graph.py` — has `ResearchDeps`? Check.

If these already comply, **drop them from the baseline** (Tier 1 win).

### 4b — Files needing the same treatment

- `kairix/agents/research/nodes.py`
- `kairix/core/embed/embed.py`
- `kairix/core/search/config_loader.py`
- `kairix/knowledge/contradict/detector.py`
- `kairix/knowledge/summaries/generate.py`
- `kairix/platform/llm/backends.py`
- `kairix/platform/onboard/check.py`
- `kairix/platform/setup/wizard.py`
- `kairix/quality/benchmark/runner.py`
- `kairix/quality/eval/retrieval.py`
- `kairix/worker.py`

Each gets a `<Operation>Deps` dataclass at the boundary. Adapters/CLIs construct deps and pass through. Effort: ~30-90 min per file depending on number of `*_fn=None` kwargs.

**Expected outcome:** F6 baseline → 0 entries.

## Phase 5 — Per-line audit (2-3 hours)

After Phases 2-4 land, the per-line suppression count should drop dramatically. Final pass:

- Strip every remaining suppression and re-introduce only what genuinely fails the rule
- Each retained suppression must have a rationale comment (F3 already enforces this for new code; this is the cleanup of legacy unrationalised ones)

## Phase 6 — Tighten the gate (locks in the gains)

Once the baselines are at zero, ratchet:

- [ ] **F7 floor 85% → 90%.** The codebase is already at >85% per-file post-Phase-2; tighten so future regressions can't sneak in.
- [ ] **F9 floor 85% → 90%** (matching F7).
- [ ] **Coverage `fail_under = 80` → `90`** in `pyproject.toml`.
- [ ] **Add F-rule: `no continue-on-error: true without rationale comment`** (mirror of F10's existing workflow-silencer rule).
- [ ] **Add F-rule: `no SonarCloud `sonar.issue.ignore` without rationale comment`** in `sonar-project.properties`.
- [ ] **Periodic baseline audit** as a scheduled GH Actions job — fails if any baseline file has stale entries (the file no longer exists or is now compliant).

## Recommended sequencing

| Phase | Effort | Dependencies | Outcome |
|---|---:|---|---|
| 1 (audit + quick wins) | 2-3h | none | -30-50 exceptions |
| 2a (F7 backfill) | 4-6h | Phase 1 | -20-30 baseline entries |
| 2b (codecov omits) | 2-3h | Phase 1 | -6 omit entries |
| 2c (production no-cover) | 1-2h | Phase 2a | -20-25 suppressions |
| 3 (test-side anti-patterns) | 4-6h | none (parallel-safe) | -30 baseline entries |
| 4 (test-only kwargs) | 3-4h | none (parallel-safe) | -14 baseline entries |
| 5 (per-line audit) | 2-3h | Phases 2-4 | -50-100 suppressions |
| 6 (tighten gate) | 1h | Phases 1-5 complete | locks gains |

**Total: 19-28 hours** to drive the exception count to ~10-20 (down from ~325) and ratchet the floor higher.

## Out of scope

- **Tier 3 exceptions** documented above (Azure/Neo4j adapters, optional-dep `importorskip`, fixture-credential `pragma: allowlist secret`, regex-bounded `NOSONAR`). These are the architecturally-correct exceptions. They survive — but each gets a rationale comment so the next reviewer sees why.
- **Sonar issue-ignore rules.** Already minimal (5) and version-controlled in `sonar-project.properties`. Each has a rationale comment.

## Parallelisation strategy (Ralph + worktrees)

This refactor is unusually well-suited to Ralph-method parallelisation
because most of the work is **file-scoped** — one production file per
backfill, one rule per refactor — and the CI/CD pipeline as it stands
today gives us strong, automated guarantees that misbehaving parallel
work cannot land:

- `develop` is the integration branch; HITL on `main` and release tags.
- Branch protection denies rebase/squash; merge commits only — agent
  commits arrive on develop as discrete merges, history stays legible.
- Auto-merge is on; head branches are deleted on merge — no manual
  shepherding once a PR is green.
- F7 (per-file 85%) and F9 (union 85%) only fail on **net-new**
  violations vs. baseline. Removing an entry from baseline ratchets the
  floor — it cannot regress later.
- Codecov `unit` + `integration` carryforward: a PR that only ships unit
  changes still gets a correct merged dashboard.
- SonarCloud branch analysis runs on every PR — agents iterate against
  the Sonar gate, not only `safe-commit.sh`.

### Conflict-surface matrix

The only files multiple agents are likely to touch concurrently are
the test scaffolding and the rule-specific baselines:

| Surface | Phase 2 (coverage) | Phase 3a (F2) | Phase 3b (F1/F5) | Phase 4 (F6) | Phase 5 | Phase 6 |
|---|---|---|---|---|---|---|
| Prod files | disjoint per-file | none | none | disjoint per-file | broad | config |
| `tests/fakes.py` | possible adds | heavy use | possible adds | possible adds | possible | none |
| `tests/conftest.py` | rare | heavy edits | possible | possible | possible | none |
| Per-rule baseline | F7, F9 (line-remove) | F2 (line-remove) | F1, F5 (line-remove) | F6 (line-remove) | F3 | none |

Line-removal in baseline files is conflict-trivial. The real contention
is `tests/fakes.py` and `tests/conftest.py`. Resolve by **combining
both sides** — both adds belong (per Ralph default in `CLAUDE.md`).

### Wave plan

**Wave 0 — Foundation (sequential, one PR on develop, no isolation).**
Add any Fake* classes the upcoming waves will share — at minimum
`FakeCredentials` for recall_check and embed work, and `FakeEmbedProviderFactory`
if multiple agents need it. Single agent on the main checkout, direct
push to develop. Eliminates the most common Wave-1 conflict before it
starts. ~30-60 min.

**Wave 1 — Coverage backfill + F6 conversions in parallel (worktrees, 4-6 agents).**
File-scoped issues from Phase 2c, 2a, and Phase 4 are independent —
each agent owns one production file. Dispatch with `isolation="worktree"`,
all in parallel, all in the same Agent message. Each agent runs
`safe-commit.sh` in its loop and reports SHA + branch when green.

From the main checkout (on `develop`), `git cherry-pick <sha>` each
agent's commit in completion order. Do **not** merge worktree branches
directly — worktrees branch off `main` (latest tagged release), not
current `develop`, so a direct merge reverts session work. Push after
each cherry-pick; auto-merge is irrelevant because we're committing
direct.

Recommended Wave-1 batch (one issue ↔ one agent):

1. Phase 2c — `kairix/core/embed/recall_check.py` (7 pragmas, alarm code)
2. Phase 2c — `kairix/quality/benchmark/runner.py` (3 pragmas + F6)
3. Phase 2c — `kairix/core/embed/embed.py` (6 pragmas, hot path)
4. Phase 2a — `kairix/core/search/rerank.py` (F7, perf)
5. Phase 2a — `kairix/core/search/vector_repository.py` (F7, perf)
6. Phase 4  — F6 conversions (12 files; can split across 2-3 agents,
   each owning a subset, since each F6 conversion is file-scoped)

Run all 6+ agents in one parallel dispatch. Expected wall clock: 1-2
hours of agent time, ~30 min of cherry-pick + conflict resolution.

**Wave 2 — F2 env-monkeypatch elimination (sequential, single PR).**
Phase 3a touches 9 test files plus `tests/fakes.py`/`conftest.py`
plumbing. The refactor is uniform (`monkeypatch.setenv("KAIRIX_*", ...)`
→ inject `FakePaths`), so a single agent on the main checkout produces
one cohesive PR. Run after Wave 1 lands so the FakePaths plumbing
doesn't conflict with concurrent fake additions.

**Wave 3 — F1 + F5 elimination (sequential, single PR).**
Phase 3b removes `@patch` on kairix internals and internal-name imports
from tests. Touches tests broadly, but the change is mechanical (replace
`@patch("kairix.foo.bar")` with constructor injection of a fake). Single
agent, single PR. Run after Wave 2 because some F1/F5 violations sit
above F2 violations in the same test files — resolve F2 first.

**Wave 4 — Per-line suppression audit (sequential, exploratory).**
Phase 5 strips every `# noqa` / `# pragma: no cover` / `# type: ignore`
that fires no rule, and adds rationale comments to the rest. Touches
everything; do not parallelise — let one agent walk file-by-file.

**Wave 5 — Ratchet (sequential, single config PR).**
Phase 6 lifts F7/F9 floors 85→90, `fail_under` 80→90, adds the new
F-rules for un-rationaled silencers. Config-only.

### What changes from the default delegation playbook

The session's standing instruction (from
`feedback_subagent_dispatch.md`) is "batches ≥2 use parallel worktrees +
cherry-pick; singletons go sequential on main checkout." That still
applies — the wave plan above is just an instance of it, scaled to a
multi-week effort. Two non-default points worth flagging:

- **Wave 0 is sequential by design.** Even though it's "just adding
  fakes," front-loading it removes the conflict that would otherwise
  hit every Wave-1 cherry-pick.
- **No agent gets autonomy over the merge.** Auto-merge is on for the
  repo, but agents in worktrees commit to their own branches and we
  cherry-pick from the main checkout — agent PRs do **not** auto-merge.
  This is an explicit constraint from the worktree pattern, not a
  weakness of auto-merge.

## Tracking

This document is the source of truth for the refactor. Each phase below
should be tracked as a sub-issue under a parent #issue, with a checkbox
ticked when the corresponding baseline entries / suppressions drop. The
parent issue (#193) acts as the burndown.
