# CONSTRAINTS.md — Code Quality Standards and Patterns

## Commit gate

Every commit passes through `bash scripts/safe-commit.sh "message"` which runs: ruff lint → ruff format → mypy → pytest (unit + bdd + contract) → architecture fitness functions (F1–F6, F8, F10–F13) → detect-secrets → confidential check. Loop on failures until green.

The fitness functions are mechanical, blocking checks that encode rejected patterns and lazy-workaround bypasses: forbidden monkeypatching (F1, F2), un-rationaled suppressions (F3 — covers `# noqa` / `# NOSONAR` / `# pragma: no cover` / `# type: ignore` / `# nosec`), env-var smuggling (F4), internal-name imports (F5), test-only kwargs (F6), unmarked tests (F8), un-rationaled CI silencers (F10), un-rationaled test skips (F11), BDD scenarios with no happy path (F12), and BDD scenarios that leak implementation symbols (F13). Pre-existing violations are grandfathered in `.architecture/baseline/`, so passing locally requires that the *changeset* introduces no new violations.

F7 (per-file unit coverage floor) and F9 (union coverage floor) run in CI only because they need the test runtime — F7 in Stage 2 and F9 in Stage 5 (after both unit and integration suites finish so their coverage data can be combined). See [docs/architecture/fitness-functions.md](docs/architecture/fitness-functions.md) for the full rule set, why each rule exists, and how to fix violations.

**SonarCloud Quality Gate is blocking** as of v2026.5.10.2. Three intentionally redundant layers — (i) the CI gate polls SonarCloud's `/api/qualitygates/project_status` and fails on `ERROR`; (ii) GitHub branch protection on `main` requires the `SonarCloud Code Analysis` check posted by the Sonar app; (iii) the Docker and PyPI publish workflows each begin with a `sonar-gate` job so even manual release events can't ship without Sonar OK. The Sonar scan step does NOT have `continue-on-error: true` — if Sonar is unavailable, the gate fails and we wait. Triage failing hotspots at https://sonarcloud.io/project/issues?id=quanyeomans_kairix; current backlog tracked in #174.

---

## Architecture patterns

### When you need to call an external service

**Use:** Protocol + Adapter pattern. Define the interface in `kairix/core/protocols.py`, implement the adapter in the relevant module, test with a fake from `tests/fakes.py`.

```python
# Protocol (kairix/core/protocols.py)
class EmbeddingService(Protocol):
    def embed(self, text: str) -> list[float]: ...

# Adapter (kairix/core/search/backends.py)
class AzureEmbeddingService:
    def embed(self, text: str) -> list[float]:
        from kairix._azure import embed_text
        return embed_text(text)

# Test (tests/)
pipeline = SearchPipeline(embedding=FakeEmbeddingService(vector=[0.1] * 1536))
```

### When you need to access the database

**Use:** Repository pattern. All data access goes through repositories in `kairix/core/db/repository.py` or `kairix/knowledge/graph/repository.py`. No direct SQL or Cypher queries in business logic.

```python
# Business logic accepts the repository interface
def enrich_chunk_dates(results: list, doc_repo: DocumentRepository) -> list:
    dates = doc_repo.get_chunk_dates([r.path for r in results])
    ...
```

### When you have multiple algorithms for the same task

**Use:** Strategy pattern. Register implementations by name, look them up at runtime. No if/elif chains.

```python
# Strategy implementations (kairix/core/search/fusion.py)
class RRFFusion:
    def fuse(self, bm25, vec) -> list: ...

class BM25PrimaryFusion:
    def fuse(self, bm25, vec) -> list: ...

# Registry (kairix/quality/eval/scorers.py)
SCORERS = {"ndcg": NDCGScorer, "exact": ExactMatchScorer, "llm": LLMJudgeScorer}
```

### When you need to orchestrate multiple components

**Use:** Pipeline composition. Construct the pipeline from protocols at the boundary (CLI, MCP server). Pass it through to handlers.

```python
# Factory (kairix/core/factory.py)
pipeline = SearchPipeline(classifier=..., bm25=..., vector=..., graph=..., fusion=..., boosts=[...])

# Handler receives the composed pipeline
def tool_search(query, pipeline: SearchPipeline) -> dict:
    return pipeline.search(query).to_dict()
```

### When you need configuration

**Use:** Config resolved once at the boundary, passed as a parameter. Functions never read environment variables or config files directly.

```python
# Boundary (CLI entry point)
config = load_config()
pipeline = build_search_pipeline(config)

# Business logic receives what it needs
def search(self, query: str, budget: int = 3000) -> SearchResult: ...
```

---

## Testing patterns

### How to test business logic

Construct the component with fakes from `tests/fakes.py`. Call the public method. Assert the result.

```python
pipeline = SearchPipeline(
    classifier=FakeClassifier(intent=QueryIntent.SEMANTIC),
    bm25=BM25SearchBackend(FakeDocumentRepository(documents=[...])),
    vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
    graph=FakeGraphRepository(),
    fusion=RRFFusion(k=60),
    boosts=[],
)
result = pipeline.search("test query")
assert len(result.results) > 0
```

### How to verify protocol compliance

Contract tests in `tests/contracts/test_protocols.py` check that both real implementations and fakes satisfy their protocol:

```python
def test_sqlite_repo_satisfies_protocol():
    repo = SQLiteDocumentRepository(db_path)
    assert isinstance(repo, DocumentRepository)
```

### How to test user-facing behaviour

BDD feature files in `tests/bdd/features/`. Step definitions in `tests/bdd/steps/`. Scenarios describe outcomes, not implementation.

### Every test has a marker

`@pytest.mark.unit`, `@pytest.mark.contract`, `@pytest.mark.bdd`, or `@pytest.mark.integration`. Unmarked tests are invisible.

---

## Common code smells and their fixes

| Smell | Example | Fix |
|-------|---------|-----|
| Function reads env var internally | `os.environ.get("KAIRIX_DB_PATH")` | Resolve at boundary, pass as parameter |
| Function constructs its own dependencies | `client = Neo4jClient()` inside business logic | Accept protocol interface as parameter |
| if/elif chain for algorithm selection | `if method == "rrf": ... elif method == "bm25_primary": ...` | Strategy pattern with registry |
| Direct SQL in business logic | `db.execute("SELECT ...")` inside a search function | Repository pattern |
| Test uses `@patch` or `unittest.mock` | `@patch("module.function")` | Use protocol fakes instead |
| Test imports private function | `from module import _helper` | Make it public or test through the public interface |
| Singleton factory function | `_client = None; def get_client()` | Construct in factory, pass through pipeline |
| Module-level constant from env var | `_ROOT = os.environ.get(...)` | Function parameter with lazy default |
| Duplicate logic across modules | Same query tokenization in 3 files | Extract to shared module (e.g. `tokenizer.py`) |
| Function does too many things | `search()` at 56 cognitive complexity | Compose from single-responsibility components |

---

## Security

- No secrets in code, comments, or test fixtures
- No `str(exc)` in user-facing output (may leak paths)
- No `shell=True` in subprocess calls
- No f-string SQL — use parameterised queries. `bm25()` FTS5 float literals are a documented exception.
- No real agent names, client names, or personal data in the public repo

---

## Delegation

- Scope each agent to specific files that don't overlap with other agents
- Each agent runs `bash scripts/safe-commit.sh` and loops on failures until green
- Target: 10-15 loops/hour per agent
- Reference: [Ralph pattern](https://github.com/three-cubes/engineering-hub/tree/main/ralph)
