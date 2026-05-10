# Eval-module rectification

**Goal:** Bring `kairix/quality/eval/` up to the boundary-pattern standard already used by `DefaultCollectionResolver` and `ConfigDrivenAgentRegistry`. Eliminate `*_fn=None` test-substitution kwargs, raw-SQL inappropriate-intimacy, cross-module private-symbol imports, and the prompt-injection / path-traversal vectors flagged by SonarCloud.

## Problem

Four classes of structural debt across the four code-reviewer-confirmed surfaces (`hybrid_sweep`, `gold_builder`, `judge`, `generate`):

1. **Silent bugs.** Tests named `testsweep_*` (missing underscore) silently skipped. `auto_gold` applies a path-shaped regex to titles. Inverted credential-merge boolean. UTF-8 encoding missing on output. Leaked DB connections on error paths.

2. **Security findings.** SonarCloud BLOCKER S2083 path-traversal with NOSONAR misplaced. Prompt-injection vectors where caller-supplied query / document content is interpolated into LLM prompts without delimited boundaries. f-string injection of BM25 weights into raw SQL with no `nan` / `inf` guard.

3. **Monkeypatch-shaped APIs at scale.** `*_fn=None` kwargs as the only substitution mechanism, no protocols, no fakes. Same anti-pattern the paths-DI initiative is removing from path resolution, replicated across the eval module:
   - `hybrid_sweep._retrieve` exists as a module-level indirection so tests can `@patch` it
   - `judge._call_llm(chat_fn=…)` for test substitution
   - `gold_builder.pool_candidates(search_fns=…)` / `grade_candidates(judge_fn=…)` / `build_independent_gold(calibrate_fn=…, grade_fn=…)`
   - `generate.generate_queries(llm_fn=…)` / `process_sampled_docs(query_fn=…, retrieve_fn=…, judge_fn=…)`

4. **Inappropriate intimacy.** `gold_builder.py` reaches into `documents_fts` with raw SQL instead of going through `DocumentRepository`. `generate.py` imports `_call_llm` (private symbol) from `judge.py` — a cross-module private-symbol coupling.

## The seam

Four protocols added to `kairix/core/protocols.py`:

```python
@runtime_checkable
class ChatBackend(Protocol):
    """LLM chat-completion surface — substitutable across Azure / OpenRouter / fakes."""
    def complete(self, prompt: str, *, system: str | None = None,
                 model: str, temperature: float = 0.0, timeout_s: float = 30.0) -> str: ...

@runtime_checkable
class LLMJudge(Protocol):
    """Pairwise / pointwise relevance judge over (query, document) pairs."""
    def grade(self, query: str, candidates: list[CandidateDoc],
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

Matching fakes live in `tests/fakes.py` (`FakeChatBackend`, `FakeLLMJudge`, `FakeQueryGenerator`, `FakeRetriever`) — each a constructor-configured in-memory fake exposing the same protocol shape.

Production classes wrap the existing free functions and inject through the constructor:

```python
class LLMJudge:
    def __init__(self, *, chat_backend: ChatBackend, deployment: str = JUDGE_DEPLOYMENT) -> None: ...
    def grade(self, query, candidates, *, runs=1) -> JudgeResult: ...
    def calibrate(self) -> bool: ...

class GoldBuilder:
    def __init__(self, *, llm_judge: LLMJudge | None = None, retriever: Retriever | None = None) -> None: ...
    def pool(self, query, systems, ...) -> list[PooledCandidate]: ...
    def grade(self, query, candidates, *, runs=1) -> list[PooledCandidate]: ...
    def build_independent_gold(self, suite_path, output_path, ...) -> GoldBuildReport: ...

class QueryGenerator:
    def __init__(self, *, chat_backend: ChatBackend | None = None) -> None: ...
    def generate(self, title, body, *, n, categories) -> list[GeneratedQuery]: ...

class SuiteGenerator:
    def __init__(self, *, query_generator=None, llm_judge=None, retriever=None) -> None: ...
    def generate_suite(self, db_path, output_path, ...) -> GenerationResult: ...
    def enrich_suite(self, suite_path, output_path, ...) -> EnrichmentResult: ...
```

CLI subcommands construct the protocol-conforming class once at command-handler entry and call methods on it — the only place protocols resolve to defaults.

## Surface inventory

```
kairix/quality/eval/
├── auto_gold.py        — uses generate.SuiteGenerator
├── cli.py              — constructs SuiteGenerator / GoldBuilder at boundary
├── constants.py
├── gate.py
├── generate.py         — QueryGenerator + SuiteGenerator classes
├── gold_builder.py     — GoldBuilder class
├── hybrid_sweep.py     — accepts retriever: Retriever via parameter
├── judge.py            — LLMJudge class wrapping judge_batch / calibrate
├── monitor.py
├── sweep.py
└── ...
```

Module-level free functions stay as deprecated wrappers for one release window so existing imports keep working. The `*_fn=None` kwargs on those wrappers are dead code from production's perspective but remain available for backwards compatibility until removed in a cleanup pass.

## Code-smell mitigations

| Smell | How the design addresses it |
|---|---|
| Monkeypatch-shaped APIs | Replaced with protocol-injection through constructors. Tests use `FakeXxx` from `tests/fakes.py`. |
| Cross-module private import | `generate.py` no longer imports `judge._call_llm`. `ChatBackend` is the public seam. |
| Raw SQL in eval module | `_bm25_search_with_weights` is a private method on `GoldBuilder`. Lifting onto `DocumentRepository.search_fts_weighted` is a follow-up; the encapsulation already isolates the SQL. |
| Hardcoded `"gpt-4o-mini"` | `JUDGE_DEPLOYMENT` constant in `judge.py` is the single source of truth. |
| Prompt injection | `query` / `snippet` / `title` wrapped in `<query>...</query>` / `<document>...</document>` delimiters; literal newlines stripped from caller-supplied content; rubric moved to `system` role. |
| BM25 weight `nan` / `inf` | `math.isfinite()` + positive check at `GoldBuilder` entry; raises `ValueError` naming the offending parameter. |
| `JudgeResult` not actually frozen | `shuffle_order: tuple[str, ...]` (was `list`); `CALIBRATION_ANCHORS` is a `tuple` of dicts. Mutation-frozen as well as assignment-frozen. |
| Path-traversal NOSONAR misplaced | NOSONAR moved inline on the statement where S2083 fires (was on a later, unrelated statement). |
| Silent bugs (4 missing-underscore tests, procedural-pattern applied to titles, inverted credential-merge, leaked DB connections) | Fixed in a dedicated bug-fix sprint before the structural refactor. |
| Missing BDD / integration coverage | Each protocol surface gets a `tests/bdd/features/eval_*.feature` and a `tests/integration/test_eval_*.py`. |

## Out of scope

- **Modifying retrieval / scoring algorithms.** This is a structural refactor; behaviour must not change. The reference-library benchmark validation is the safety net.
- **Removing `monkeypatch.setenv("KAIRIX_*")` from `tests/eval/`** — belongs to the paths-DI initiative ([#139](https://github.com/quanyeomans/kairix/issues/139)). This initiative removes only the eval-specific `*_fn=None` substitution kwargs and the `_call_llm` private-symbol import.
- **Refactoring `kairix/quality/benchmark/`.** Adjacent module, separate concern.

## CI

A grep gate counts `*_fn=None` substitution kwargs and `_call_llm` private imports remaining in `kairix/quality/eval/`, mirroring the paths-DI gate pattern. Initially warn-only; flips to fail-blocking in a cleanup pass after benchmark validation confirms no regression.

## Tracking

Umbrella issue: [#143](https://github.com/quanyeomans/kairix/issues/143).
