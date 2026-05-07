# Planning: eval-module rectification

**Status:** Phase 0 in progress (2026-05-07)
**Target version:** v2026.6.x rolling — phases ship as separate PRs into `develop`
**Owner:** maintainers (foundation phases); delegated agents (Phase 2b file-isolated refactors)
**Primary motivation:** The `kairix/quality/eval/` module has accumulated structural debt across four code-reviewer-confirmed surfaces (`hybrid_sweep`, `gold_builder`, `judge`, `generate`). Specific findings: a 6-bug list of silent failures (including 4 silently-skipped tests), 4 prompt-injection / path-traversal security issues, 11+ `*_fn=None` test-substitution kwargs that monkeypatch is the wrong fix for, raw-SQL inappropriate-intimacy in `gold_builder` (should go through `DocumentRepository`), a private `_call_llm` symbol imported from `judge.py` into `generate.py`, hardcoded `"gpt-4o-mini"` in 5+ places, and zero BDD or integration coverage for the gold-builder / judge / generator pipelines. Mirrors the pattern established in the paths-DI initiative ([#139](https://github.com/quanyeomans/kairix/issues/139)).

---

## Problem statement

Four classes of smell, all interlocking:

1. **Silent bugs.** Four `testsweep_*` tests in `tests/eval/test_hybrid_sweep.py` are missing the underscore separator — pytest doesn't collect them, so `sweep_config_to_retrieval_config` is unverified. `auto_gold.py:116` applies a path-shaped regex (`(?:^|/)how-to-|...`) to titles (which never contain slashes), so the procedural-query filter is permanently empty. Several other latent issues (UTF-8 encoding, inverted credential-merge logic, leaked DB connection on error paths). All confirmed by parallel reviews of the four named files.

2. **Real security findings.** SonarCloud BLOCKER S2083 path-traversal in `eval/cli.py:138` with NOSONAR misplaced (suppression on line 145, rule fires at line 138 — same shape we hit in the wikilinks pilot). Prompt-injection vectors in `judge.py:348` and `generate.py:255` where caller-supplied `query` / document `snippet` are interpolated into LLM prompts without delimited boundaries. f-string injection of BM25 weights into raw SQL with the comment "safe: float() cast" — `float()` doesn't guard `nan` / `inf`, both of which are valid Python floats and produce undefined SQLite behaviour.

3. **Monkeypatch-shaped APIs at scale.** The same anti-pattern the paths-DI initiative is removing from path resolution, replicated across the eval module:
   - `hybrid_sweep._retrieve` is a module-level function whose only purpose is to be `@patch`-ed by `tests/eval/test_hybrid_sweep.py`
   - `judge._call_llm(chat_fn=…)` — `chat_fn` parameter exists only for test substitution
   - `gold_builder.pool_candidates(search_fns=…)`, `grade_candidates(judge_fn=…)`, `build_independent_gold(calibrate_fn=…, grade_fn=…)` — four `*_fn` kwargs across three functions
   - `generate.generate_queries(llm_fn=…)`, `process_sampled_docs(query_fn=…, retrieve_fn=…, judge_fn=…)` — and imports `_call_llm` (private) from `judge.py` as the production default
   No `LLMJudge` / `Retriever` / `QueryGenerator` / `ChatBackend` protocols in `kairix/core/protocols.py`; no fakes in `tests/fakes.py`.

4. **Inappropriate intimacy.** `gold_builder.py:92-176` reaches into `documents_fts` with raw SQL instead of going through `DocumentRepository` (which already exists for this exact purpose). `generate.py:36-43` imports `_call_llm` (private symbol) from `judge.py` — a cross-module private-symbol coupling that should not exist.

---

## Goals & non-goals

### Goals (this initiative)

1. **Fix the silent bugs.** They produce wrong results today (procedural filter empty, sweep-config tests not running, credential merge inverted). Top-priority sprint, no API change.
2. **Resolve the security findings.** Two BLOCKER VULN cleared with NOSONAR placed inline. Prompt-injection vectors hardened with delimited-content boundaries. BM25 weights validated for finiteness.
3. **Define `LLMJudge`, `QueryGenerator`, `Retriever`, `ChatBackend` protocols** in `kairix/core/protocols.py`. Add matching fakes in `tests/fakes.py`. Add contract tests for each.
4. **Refactor production to inject through constructors** instead of `*_fn=None` kwargs. Mirror the paths-DI shape: protocol at boundary, fakes for tests, deprecated shims for one release window, Phase 4 removes them.
5. **Move `gold_builder` FTS access onto `DocumentRepository`.** Eliminate raw SQL from the eval module.
6. **Remove `_call_llm` private-symbol import from `generate.py`.** Define a public `ChatBackend` protocol; `judge.py` adapter implements it.
7. **Split `generate.py`** (876 lines, three jobs) into `eval/generate/sampling.py`, `eval/generate/query_gen.py`, `eval/generate/pipeline.py`. Each independently testable.
8. **BDD + integration coverage.** Every public eval surface gets a `tests/bdd/features/eval_*.feature` and a `tests/integration/test_eval_*.py`. Currently zero — agents add coverage as part of their refactor PRs.
9. **Reference-library benchmark validation.** After all merges, run `kairix eval hybrid-sweep --suite suites/reflib-gold-v3.yaml --collection reference-library --quick` against `:develop`. Compare against the v2026.4.27 baseline (R10 0.8171, NDCG@10 0.8385, Hit@5 0.9629). 0.02 NDCG drop blocks merge to main per existing benchmark gate.

### Non-goals

- **Modifying retrieval / scoring algorithms.** This is a structural refactor; behaviour must not change. The benchmark validation in Phase 5 is the safety net.
- **Removing all monkeypatch usage in `tests/eval/`.** `monkeypatch.setenv("KAIRIX_*")` cleanup belongs to the paths-DI initiative ([#139](https://github.com/quanyeomans/kairix/issues/139)). This initiative removes only the eval-specific `*_fn=None` substitution kwargs and the `_call_llm` private-symbol import.
- **Refactoring `kairix/quality/benchmark/`.** Adjacent module, separate concern. The reference-library benchmark CLI lives there but is not in this scope.
- **Removing `JUDGE_API_VERSION` and `JUDGE_TIMEOUT_S` dead constants.** Cleanup under Phase 0b alongside the security pass.

---

## Surface inventory

### Production files

```
kairix/quality/eval/
├── auto_gold.py        (214 lines — touched by agent-generate)
├── cli.py              (621 lines — touched by Phase 3)
├── constants.py        (no changes)
├── gate.py             (no changes — already shipped clean in #131)
├── generate.py         (876 lines → split into 3 modules — agent-generate)
├── gold_builder.py     (435 lines — agent-gold-builder)
├── hybrid_sweep.py     (657 lines — agent-sweep)
├── judge.py            (439 lines — Phase 2a, owner)
├── monitor.py          (330 lines — touched by Phase 0b for the MINOR vuln)
├── sweep.py            (294 lines — agent-sweep)
└── ...
```

### Test files (incremental — owner adds protocol contract tests in Phase 1, agents add scenario tests as part of their refactor PRs)

```
tests/contracts/
├── test_chat_backend_contract.py    (new — Phase 1)
├── test_llm_judge_contract.py       (new — Phase 1)
├── test_query_generator_contract.py (new — Phase 1)
└── test_retriever_contract.py       (new — Phase 1)

tests/bdd/features/
├── eval_judge_calibration.feature   (new — Phase 2a, owner)
├── eval_sweep.feature               (new — agent-sweep)
├── eval_gold_builder.feature        (new — agent-gold-builder)
├── eval_query_generation.feature    (new — agent-generate)
└── eval_cli.feature                 (new — Phase 3)

tests/integration/
├── test_eval_judge.py               (new — Phase 2a)
├── test_eval_sweep.py               (new — agent-sweep)
├── test_eval_gold_builder.py        (new — agent-gold-builder)
└── test_eval_generate.py            (new — agent-generate)
```

### Cross-file dependency graph (drives Phase 2 ordering)

```
        constants.py     metrics.py
             │               │
             ▼               ▼
         judge.py ◀──── (foundation; Phase 2a, owner)
             │
       ┌─────┼─────────┐
       ▼     ▼         ▼
  gold_builder  generate    (sweep is independent of judge)
       │           │
       │           ▼
       │       auto_gold ──┐
       │                   │
       └──────────┬────────┘
                  ▼
                cli.py     (Phase 3, owner)
```

`hybrid_sweep.py` / `sweep.py` depend on `retrieval.py` only — no judge dependency. They can run in parallel with Phase 2a.

`gold_builder.py` and `generate.py` depend on `judge.py`. They use the new `LLMJudge` protocol from Phase 1 (which exists before Phase 2a runs), so they can refactor against the protocol shape independently — they don't have to wait for `judge.py`'s implementation refactor.

`auto_gold.py` depends on `generate.py`. Bundle them in agent-generate.

`cli.py` depends on all of the above. Phase 3 (owner) sequences after every Phase-2 PR has merged.

---

## Phases

### Phase 0 — Silent-bug fixes (OWNER, single PR)

Touch:
- `tests/eval/test_hybrid_sweep.py` — rename four `testsweep_*` → `test_sweep_*`. Verify pytest now collects them (count goes up).
- `kairix/quality/eval/auto_gold.py:116` — apply `_PROCEDURAL_PATTERNS` to paths via a `path_for(title)` lookup, not titles. Add a regression test.
- `kairix/quality/eval/auto_gold.py:212` — `open(output_path, "w", encoding="utf-8")`.
- `kairix/quality/eval/generate.py:456-470` — replace inverted `resolve_credentials` boolean logic with explicit caller-wins-unless-None semantic. Sentinel string `"gpt-4o-mini"` replaced by `is None` check. Document why.
- `kairix/quality/eval/generate.py:800` — wrap `enrich_suite`'s `resolve_credentials` call in the same try/except shape `generate_suite` uses; append to `errors`.
- `kairix/quality/eval/gold_builder.py:111-176` — wrap DB access in `try/finally` (or `contextlib.closing`); ensure `.close()` runs on every exit path.

Each fix has a regression test added in `tests/eval/`.

**Acceptance:** safe-commit green; the four previously-skipped `test_sweep_*` tests now run; new regression tests for procedural-pattern + UTF-8 + credential-merge + DB-leak pass.

### Phase 0b — Security pass (OWNER, single PR)

Touch:
- `kairix/quality/eval/cli.py:138` — move NOSONAR(python:S2083) inline on the `Path(...).resolve()` line. Add a `_validate_under(path, allowed_root)` helper to verify the resolved path stays under an allowed root.
- `scripts/migrate-domain-structure.py:108` — same shape.
- `kairix/quality/eval/judge.py:348` — wrap `query` and `snippet` in `<query>…</query>` / `<document>…</document>` delimiters. Move the rubric to a `system` role message. Strip literal `\n` from `query` and `snippet` before interpolation.
- `kairix/quality/eval/generate.py:255` — same delimited-content fix for the document body.
- `kairix/quality/eval/gold_builder.py:107-149` — `_validate_weights(w_fp, w_title, w_doc)` raises `ValueError` if any weight is `nan`, `inf`, or `<= 0`. Call at function entry. Comment updated to remove the misleading "safe: float() cast" claim.
- `kairix/quality/eval/judge.py:48-51` — delete dead constants `JUDGE_API_VERSION`, `JUDGE_TIMEOUT_S`. Document where the values actually come from in `_azure.py`.
- `kairix/quality/eval/judge.py:64` — change `CALIBRATION_ANCHORS` to `tuple[…, …]` (frozen).
- `kairix/quality/eval/judge.py:183` — `JudgeResult.shuffle_order: tuple[str, ...]` (matches `frozen=True` intent).
- `tests/graph/test_upsert_edge.py:32, 114` — `# pragma: allowlist secret` annotations on the test fixture password strings.
- `tests/test_secrets.py:179, 249` — same.

**Acceptance:** safe-commit green; SonarCloud BLOCKER VULN count drops from 2 → 0 on the next scan; MINOR VULN at `eval/monitor.py:96` and `benchmark/baseline.py:184` (log injection) dropped via narrower `type(e).__name__` logging.

### Phase 1 — Protocols + fakes + contract tests (OWNER, single PR)

Add to `kairix/core/protocols.py`:

```python
@runtime_checkable
class ChatBackend(Protocol):
    """LLM chat completion surface — substitutable across Azure / OpenRouter / fakes."""
    def complete(self, prompt: str, *, system: str | None = None,
                 model: str, temperature: float = 0.0, timeout_s: float = 30.0) -> str: ...

@runtime_checkable
class LLMJudge(Protocol):
    """Pairwise / pointwise relevance judge over (query, document) pairs."""
    def grade(self, query: str, documents: list[CandidateDoc],
              *, runs: int = 1) -> JudgeResult: ...
    def calibrate(self) -> CalibrationResult: ...

@runtime_checkable
class QueryGenerator(Protocol):
    """Synthesises eval queries from corpus documents."""
    def generate(self, doc: CorpusDoc, *, n: int, intent: str) -> list[GeneratedQuery]: ...

@runtime_checkable
class Retriever(Protocol):
    """Hybrid-search facade for sweep / benchmark callers."""
    def retrieve(self, query: str, *, collections: list[str] | None,
                 cfg: RetrievalConfig) -> RetrievalResult: ...
```

Add to `tests/fakes.py`:

```python
class FakeChatBackend:
    def __init__(self, *, responses: list[str] | None = None,
                 raise_on_call: Exception | None = None) -> None: ...

class FakeLLMJudge:
    def __init__(self, *, fixed_grades: dict[str, int] | None = None) -> None: ...

class FakeQueryGenerator:
    def __init__(self, *, fixed_queries: list[GeneratedQuery] | None = None) -> None: ...

class FakeRetriever:
    def __init__(self, *, fixed_results: dict[str, RetrievalResult] | None = None) -> None: ...
```

Add to `tests/contracts/`:

- `test_chat_backend_contract.py` — `FakeChatBackend` and the production `_AzureChatBackend` adapter both satisfy `ChatBackend` via `isinstance`.
- `test_llm_judge_contract.py` — same shape; production class is added in Phase 2a, contract test imports it after merge.
- `test_query_generator_contract.py`, `test_retriever_contract.py` — same.

**No production code change.** This phase only adds contract surface. Production keeps current `*_fn=None` shape until Phase 2 wraps it.

**Acceptance:** safe-commit green; new contract tests pass for `FakeXxx`. Phase 2 PRs depend on this PR being merged (each agent imports from `tests.fakes`).

### Phase 2a — Refactor judge.py (OWNER, single PR)

Foundation phase, owner-led because every other Phase 2 surface either uses `LLMJudge` or imports from `judge.py`.

- New `LLMJudge` class in `judge.py` wrapping `judge_batch`, `calibrate`, `_call_llm`. Constructor takes `ChatBackend`. Class methods `.grade(...)` and `.calibrate(...)` satisfy the `LLMJudge` protocol.
- `_AzureChatBackend` adapter wrapping `kairix._azure.chat_completion` — implements `ChatBackend`. Lives in `kairix/_azure.py` next to the existing function.
- Module-level `judge_batch(...)` and `calibrate(...)` kept as **deprecated thin wrappers** that construct an `LLMJudge` with the default `_AzureChatBackend` and delegate. Existing callers (gold_builder, generate, tests) keep working until Phase 3 removes the deprecated wrappers.
- `tests/eval/test_judge.py` — refactored to construct `FakeChatBackend` and inject. Floor test count.
- New `tests/integration/test_eval_judge.py` — exercises the real `LLMJudge` against a stubbed `ChatBackend` end-to-end.
- New `tests/bdd/features/eval_judge_calibration.feature` — calibration happy-path, calibration-fails-after-N-errors, no-credentials-emits-clear-error.

**Acceptance:** safe-commit green; `LLMJudge` satisfies `LLMJudge` protocol via contract test; `chat_fn=None` kwarg gone from new class API but kept in deprecated wrapper.

### Phase 2b — Parallel agent fan-out (THREE Ralph agents)

Each agent gets a file-isolated scope. They open separate PRs against `develop`. Merges happen serially as each agent's CI clears (no merge conflicts because the files don't overlap).

#### agent-sweep
- **Production:** `kairix/quality/eval/sweep.py`, `kairix/quality/eval/hybrid_sweep.py`
- **Tests:** `tests/eval/test_sweep.py` (new or existing), `tests/eval/test_hybrid_sweep.py`, `tests/eval/test_logger.py` (float-equality bugs flagged here)
- **Refactor:** `_retrieve` becomes `Retriever` protocol param on `evaluate_single_config` and `sweep_hybrid_params`. Tests inject `FakeRetriever`. Eliminate `unittest.mock.patch("...._retrieve")` usage. Replace 13-positional `aggregate_ndcg_for_config` with a `_RetrievalAccumulator` dataclass. Fix `sweep_config_to_retrieval_config` return type from `Any` to proper `RetrievalConfig` annotation. Fix all float-equality assertions in the test file to `pytest.approx`.
- **BDD:** `tests/bdd/features/eval_sweep.feature` — at minimum: hybrid sweep with mock retriever produces expected ranking, BM25-only mode skips vector search, RRF mode merges both ranks.
- **Integration:** `tests/integration/test_eval_sweep.py` — end-to-end with `FakeRetriever` and small fixture.
- **Backwards compat:** keep `_retrieve` as a deprecated module-level shim for one release window.

#### agent-gold-builder
- **Production:** `kairix/quality/eval/gold_builder.py`
- **Tests:** new `tests/eval/test_gold_builder.py`, new integration test
- **Refactor:** wrap as `GoldBuilder` class. Constructor takes `DocumentRepository`, `LLMJudge`, `Retriever`. Eliminate `pool_candidates(search_fns=…)`, `grade_candidates(judge_fn=…)`, `build_independent_gold(calibrate_fn=…, grade_fn=…)` — all become methods on `GoldBuilder`. **Move FTS access from raw SQL onto `DocumentRepository.search_fts_weighted(...)`** (extend the protocol if necessary, in coordination with the protocols added in Phase 1). Remove the f-string-injected weight construction. Hardcoded `"gpt-4o-mini"` → `JUDGE_DEPLOYMENT` import.
- **BDD:** `tests/bdd/features/eval_gold_builder.feature` — pool-candidates produces fused ranking, grade-candidates assigns 0/1/2 labels, build-independent-gold full pipeline.
- **Integration:** end-to-end with real SQLite fixture + `FakeLLMJudge`.
- **Backwards compat:** module-level `pool_candidates` / `grade_candidates` / `build_independent_gold` kept as thin wrappers that construct a `GoldBuilder` with default deps.

#### agent-generate
- **Production:** `kairix/quality/eval/generate.py` → split into `kairix/quality/eval/generate/__init__.py`, `kairix/quality/eval/generate/sampling.py` (DB sampling), `kairix/quality/eval/generate/query_gen.py` (LLM query generation), `kairix/quality/eval/generate/pipeline.py` (orchestration). Plus `kairix/quality/eval/auto_gold.py`.
- **Tests:** new `tests/eval/test_generate_sampling.py`, `tests/eval/test_generate_query_gen.py`, `tests/eval/test_generate_pipeline.py`, refactored `tests/eval/test_auto_gold.py`.
- **Refactor:** `QueryGenerator` class wrapping `generate_queries`; constructor takes `ChatBackend`. Pipeline `process_sampled_docs` becomes a method on a `SuiteGenerator` class taking `QueryGenerator`, `Retriever`, `LLMJudge`. Eliminate `query_fn=…, retrieve_fn=…, judge_fn=…` kwargs. **Remove `from kairix.quality.eval.judge import _call_llm`** — use `ChatBackend` protocol via `LLMJudge` instead. `_PROCEDURAL_PATTERNS` and `_cat_prefix` co-located at module scope. `resolve_credentials` already fixed in Phase 0; keep as-is.
- **BDD:** `tests/bdd/features/eval_query_generation.feature` — generate queries from doc, distribute by intent category, enrich-suite with judge labels.
- **Integration:** end-to-end with `FakeChatBackend` driving the full `auto_gold` flow.
- **Backwards compat:** module-level functions in `generate.py` re-exported from the new package layout for one release window. `auto_gold.build_suite` keeps signature, internally constructs `SuiteGenerator`.

Each agent prompt template in `eval-module-refactor-agent-prompts.md` (drafted alongside this doc).

### Phase 3 — Integrate refactored shapes into cli.py + remove deprecated kwargs (OWNER, single PR)

After every Phase 2 PR has merged:
- `cli.py` updated to construct `LLMJudge` / `GoldBuilder` / `SuiteGenerator` at command-handler entry, with `_AzureChatBackend` and `DocumentRepository` injected via the existing factory.
- All deprecated module-level functions and `*_fn=None` kwargs deleted.
- `BDD` `eval_cli.feature` covering the user-facing `kairix eval` subcommands.
- `JUDGE_API_VERSION` and `JUDGE_TIMEOUT_S` dead constants deleted (already noted under Phase 0b).

**Acceptance:** safe-commit green; `grep -rn "_fn:\s*Callable" kairix/quality/eval/` returns nothing; `grep -rn "_call_llm" kairix/quality/eval/generate/` returns nothing.

### Phase 4 — Deploy consolidated develop to VM

After all eval-module PRs land on develop:
- `:develop` Docker image rebuilt automatically by `6 · Docker Publish` workflow.
- VM: `docker compose pull && docker compose up -d --force-recreate kairix kairix-worker`.
- Verify worker startup clean (schema migration no-op, scan + embed cycle, recall check).
- Spot-check default-scope search behaviour matches the morning's verification (in_default still excludes archive + reflib).

### Phase 5 — Reference-library benchmark validation

Critical safety net. The eval refactor is structural (no algorithm change), but the only way to prove that empirically is to run the benchmark against the refactored code and compare against baseline.

```bash
docker exec <kairix-worker-container> \
    kairix eval hybrid-sweep \
        --suite suites/reflib-gold-v3.yaml \
        --collection reference-library \
        --quick
```

**Baseline (v2026.4.27):**
- R10: 0.8171
- NDCG@10: 0.8385
- Hit@5: 0.9629
- MRR@10: 0.7614

**Acceptance:** NDCG@10 drop ≤ 0.02 (matches the existing CI benchmark gate). If regression detected, bisect across the four Phase-2 PRs to identify the culprit; revert and re-do the offending phase. Pass triggers the green light to merge `develop → main` for the next CalVer release.

---

## Conflict-avoidance design

Parallel agents in Phase 2b never touch:
- `tests/fakes.py` — owner adds all fakes in Phase 1
- `kairix/core/protocols.py` — owner adds all protocols in Phase 1
- `kairix/quality/eval/cli.py` — owner integrates in Phase 3
- `kairix/quality/eval/judge.py` — owner refactors in Phase 2a
- `tests/conftest.py` — single line addition per agent (BDD step plugin registration); merge-time conflict is trivial

Each agent's scope is pre-listed at the top of their prompt. Agent acceptance criteria reject PRs that touch out-of-scope files.

Backwards compat shims in Phase 2b mean Phase 3 can land *without* coordinating signature changes — the old shape stays available until Phase 3 deletes it deliberately.

---

## CI grep gates

Reuse the warn-only counter pattern from paths-DI Phase 0:

```bash
EVAL_FN_COUNT=$(grep -rln '\b_fn:\s*Callable' kairix/quality/eval/ --include='*.py' | wc -l)
echo "::notice::eval-refactor: $EVAL_FN_COUNT files still have *_fn=None substitution kwargs"

EVAL_PRIVATE_IMPORT_COUNT=$(grep -rln 'from kairix.quality.eval.judge import _call_llm' kairix/quality/eval/ --include='*.py' | wc -l)
echo "::notice::eval-refactor: $EVAL_PRIVATE_IMPORT_COUNT files still import _call_llm (private symbol)"
```

Phase 3 flips these to **fail** when the count > 0.

---

## Tracking

- One umbrella issue on GitHub linking every phase PR
- Roadmap entry on `docs/project/ROADMAP.md` Near-term, above the paths-DI entry
- Each agent PR references the umbrella issue
- Phase 5 benchmark run posted as a comment on the umbrella issue with before/after numbers

---

## Out of scope (separate initiatives)

- **Removing `monkeypatch.setenv("KAIRIX_*")` from `tests/eval/`** — paths-DI Phase 3 (umbrella issue [#139](https://github.com/quanyeomans/kairix/issues/139))
- **Refactoring `kairix/quality/benchmark/`** — adjacent module, separate concern
- **Replacing `random.*` with `secrets.*`** in eval — these are non-security PRNG usages (sampling, calibration shuffle); SonarCloud hotspots, not vulnerabilities. Triage with NOSONAR rationale during Phase 0b.
