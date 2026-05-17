# Architecture fitness functions — canonical reference

**Status:** Authoritative. Linked from CLAUDE.md and the engineering hub.
Point agents and contributors at this file.

This document describes kairix's mechanical, blocking architecture
enforcement. Each rule is implemented as a standalone check, gated at
every layer of the SDLC, and ratcheted via a baseline file. The
**implementation is the source of truth**: when this document and the
scripts under `scripts/checks/` disagree, the scripts win and this
document needs an update.

---

## Table of contents

1. [Intent](#intent)
2. [Compliance-as-code: the ratcheting baseline pattern](#compliance-as-code-the-ratcheting-baseline-pattern)
3. [Rules at a glance](#rules-at-a-glance)
4. [The rules in detail](#the-rules-in-detail)
   - [F1 — No `@patch` on kairix internal code](#f1--no-patch-on-kairix-internal-code)
   - [F2 — No `monkeypatch.setenv("KAIRIX_*")` in tests](#f2--no-monkeypatchsetenvkairix_-in-tests)
   - [F3 — Suppressions require rationale](#f3--suppressions-require-rationale)
   - [F5 — No internal-name imports in tests](#f5--no-internal-name-imports-in-tests)
   - [F6 — No `*_fn=None` test-only kwargs in production](#f6--no-_fnnone-test-only-kwargs-in-production)
   - [F7 — Per-file coverage floor at 85%](#f7--per-file-coverage-floor-at-85)
   - [F4 — No `os.environ.get("KAIRIX_*")` outside `paths.py` / `secrets.py`](#f4--no-osenvirongetkairix_-outside-pathspy--secretspy)
   - [F8 — Every `test_*` function has a category marker](#f8--every-test_-function-has-a-category-marker)
   - [F9 — Per-file 85% floor on union coverage](#f9--per-file-85-floor-on-union-coverage)
   - [F10 — CI workflow silencers require rationale](#f10--ci-workflow-silencers-require-rationale)
   - [F11 — Test skip mechanisms require rationale](#f11--test-skip-mechanisms-require-rationale)
   - [F12 — Every BDD feature has a happy-path scenario](#f12--every-bdd-feature-has-a-happy-path-scenario)
   - [F13 — BDD scenarios reject implementation symbols](#f13--bdd-scenarios-reject-implementation-symbols)
5. [SDLC integration map](#sdlc-integration-map)
6. [Harness architecture](#harness-architecture)
7. [GitHub Actions integration](#github-actions-integration)
8. [Operating the harness](#operating-the-harness)
9. [Adding a new fitness function](#adding-a-new-fitness-function)
10. [Limits — what fitness functions don't catch](#limits--what-fitness-functions-dont-catch)
11. [Cross-references](#cross-references)
12. [For agents: machine-readable rule index](#for-agents-machine-readable-rule-index)

---

## Intent

Fitness functions are **mechanical, blocking checks** that encode
architectural decisions into automation. Three properties distinguish
them from lint rules:

- **They encode decisions, not preferences.** Lint rules ("use snake_case")
  are stylistic. Fitness functions ("no `@patch` on kairix internals")
  are architectural — violating one is a regression on a deliberate
  design choice.
- **They block, they don't warn.** A warn-only check is decorative. The
  rule is `exit 1` on net-new violations.
- **They ratchet.** Pre-existing violations are grandfathered in a
  baseline file; new violations fail the build. The baseline shrinks
  over time, never grows.

The motivation is empirical. During development of kairix the following
patterns were repeatedly introduced, reviewed, and then reverted as
architectural mistakes:

- Test-only `*_fn=None` parameters on production helpers (#113, #114
  reverts).
- `monkeypatch.setenv("KAIRIX_*")` to drive paths in tests instead of
  constructor injection (#139 closure).
- `@patch("kairix.…")` on internal modules instead of using
  `Protocol`/Adapter/Fake at the boundary.

Reviewer vigilance is not enough — these patterns slip through review
because they're locally plausible. Encoding them as fitness functions
makes the rejection automatic and the rationale persistent.

---

## Compliance-as-code: the ratcheting baseline pattern

### The mechanism

Each fitness function has:

1. **A check script** under `scripts/checks/` that scans the repo and
   emits a list of files with the violation.
2. **A baseline file** at
   `.architecture/baseline/<rule-name>-files.txt` listing files
   currently containing the violation. One file path per line.
3. **A gate** that fails the build if any file with the violation is
   *not* in the baseline (= net-new violation introduced).

```
current_violations - baseline_violations = net_new
if net_new not empty: exit 1
```

Pre-existing violations stay green until cleaned. New violations fail
the build immediately. The baseline shrinks file-by-file as cleanup
happens; when it reaches zero, the baseline file is deleted and the
rule is fully enforced.

### Why file-level granularity

The baseline tracks **files**, not lines. A file in the baseline gets
a free pass for every existing violation it contains, but the
expectation is the file is on the cleanup list — not that more
violations of the same type can be added inside it freely.

This is a deliberate trade-off:
- File-level baselines are stable across refactors (line numbers shift
  on every edit).
- The downside (a baselined file could grow more violations) is
  acceptable in practice because the file is already flagged for
  cleanup; net-new violations are caught the moment the file is
  removed from the baseline.

If a rule needs per-instance precision later, the helper library
(`scripts/checks/_arch_lib.py`) can be extended without changing the
gate semantics.

### Adding to a baseline

Adding a file to a baseline is **rare** and requires:

1. PR-description rationale documenting why the violation is
   genuinely the right answer for this case.
2. Reviewer approval of the rationale.
3. A linked follow-up issue or task to revisit and remove the entry
   when the underlying constraint is resolved.

The check's failure message reminds operators that "adding to the
baseline is rare." Treat this as the same friction as adding a
`# pragma: no cover` — possible, documented, and reviewed.

### Removing from a baseline

The intended workflow:

1. Make the code change that fixes the violation.
2. Re-run the relevant check locally — it should pass.
3. Delete the file's line from the baseline file.
4. Commit both changes together.
5. The check now enforces the rule fully on that file going forward.

When all entries are gone, delete the baseline file. The rule is now
fully enforced; new violations anywhere in the codebase block.

---

## Rules at a glance

| ID | Rule | Detection | Tool | SDLC layer | Baseline file |
|----|------|-----------|------|------------|---------------|
| F1 | No `@patch` on kairix internal code | line pattern | shell + grep | pre-commit, safe-commit, CI Stage 0 | `no-internal-patches-files.txt` |
| F2 | No `monkeypatch.setenv("KAIRIX_*")` in tests | line pattern | shell + grep | pre-commit, safe-commit, CI Stage 0 | `no-env-monkeypatch-files.txt` |
| F3 | Suppressions require inline rationale | line pattern | shell + grep | pre-commit, safe-commit, CI Stage 0 | `suppressions-have-rationale-files.txt` |
| F4 | No `os.environ.get("KAIRIX_*")` outside `paths.py`/`secrets.py` | line pattern | shell + grep | pre-commit, safe-commit, CI Stage 0 | `env-reads-in-paths-files.txt` |
| F5 | No internal-name imports in tests | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | `no-internal-test-imports-files.txt` |
| F6 | No `*_fn=None` test-only kwargs in production | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | `no-test-only-kwargs-files.txt` |
| F7 | Per-file coverage floor at 90% (unit) | coverage report | Python + Cobertura XML | CI unit-and-type | `per-file-coverage-floor-files.txt` |
| F8 | Every `test_*` function carries a category marker | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | (none — clean baseline) |
| F9 | Per-file 90% floor on union (unit ∪ integration) coverage | coverage report | Python + `coverage combine` + Cobertura XML | CI Stage 5 (after unit + integration) | `per-file-coverage-floor-union-files.txt` |
| F10 | CI workflow silencers require rationale | line pattern | shell + grep | pre-commit, safe-commit, CI Stage 0 | (none — clean baseline) |
| F11 | Test skip mechanisms require rationale | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | (none — clean baseline) |
| F12 | Every BDD feature has at least one happy-path scenario | structural | Python (Gherkin parser) | pre-commit, safe-commit, CI Stage 0 | (none — clean baseline) |
| F13 | BDD scenarios reject implementation symbols | line pattern | Python (regex) | pre-commit, safe-commit, CI Stage 0 | `bdd-no-implementation-leaks-files.txt` |
| F14 | `sonar.issue.ignore` entries in `sonar-project.properties` require rationale comment | line pattern | Python (regex) | pre-commit, safe-commit, CI Stage 0 | (none — clean baseline) |
| F15 | No logging of secret-named variables in plaintext | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | `no-logging-secrets-files.txt` (empty — clean) |
| F16 | Cognitive complexity ≤ 15 per function | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | `cognitive-complexity-files.txt` |
| F17 | No string literal ≥10 chars duplicated ≥3 times in a module | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | `no-duplicate-string-files.txt` |
| F18 | No commented-out code | line pattern + Python parse | Python AST | pre-commit, safe-commit, CI Stage 0 | `no-commented-out-code-files.txt` (empty — clean) |
| F19 | Unused function parameters must be `_`-prefixed | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | `unused-params-named-files.txt` |
| F20 | Empty function bodies require docstring or intent comment | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | `empty-body-intent-files.txt` |
| F21 | Check-script failure output must carry an action marker (`fix:`, `next:`, `run:`) | structural | Python AST + shell regex | pre-commit, safe-commit, CI Stage 0 | `actionable-feedback-files.txt` |
| F22 | Repo paths follow per-tree naming conventions | structural | Python (regex per tree) | pre-commit, safe-commit, CI Stage 0 | `path-naming-files.txt` (empty — clean) |
| F23 | Every top-level directory has a `README.md` | structural | Python (filesystem walk) | pre-commit, safe-commit, CI Stage 0 | `readme-coverage-files.txt` |
| F24 | No `from tests.*` / `import tests` imports in `kairix/**/*.py` | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | `no-test-imports-in-prod-files.txt` (empty — clean) |
| F25 | Every CLI subcommand has an MCP affordance — real `tool_<command>` binding OR `OperatorOnlyCapability` escalation stub | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | `capability-affordance-files.txt` (empty — clean) |

### Go-side rules (G1–G10)

Active when `services/<name>/go.mod` exists. Full text and rationale in
[`go-integration-plan.md`](go-integration-plan.md) §"Architecture
fitness — extending F1-F24 to Go". The Go gate (`Go quality` workflow)
enforces these in parallel with the Python pipeline.

| ID | Rule | Detection | Tool | SDLC layer | Baseline file |
|----|------|-----------|------|------------|---------------|
| G1 | Every `cmd/<name>/main.go` exposes `--version` | structural | golangci-lint custom rule (planned) | Go-quality workflow | `go-version-flag-files.txt` (empty — clean) |
| G2 | Errors wrap with `%w` (`fmt.Errorf("...: %w", err)`) | structural | `errorlint` (golangci-lint) | Go-quality workflow | (none — clean baseline) |
| G3 | No `interface{}` / `any` in exported signatures | structural | revive `exported` + custom | Go-quality workflow | `go-any-in-exported-files.txt` (planned) |
| G4 | `context.Context` as first arg on exported I/O functions | structural | revive `context-as-argument` | Go-quality workflow | `go-context-propagation-files.txt` (planned) |
| G5 | Every Go package has a doc comment | structural | revive `package-comments` | Go-quality workflow | (none — clean baseline) |
| G6 | No `panic` in non-`main` packages | structural | gocritic + custom | Go-quality workflow | (none — clean baseline) |
| G7 | Tests follow Go conventions (`*_test.go`, `TestXxx(t *testing.T)`) | structural | `go test` discovery + custom | Go-quality workflow | (none — clean baseline) |
| G8 | Logging via `log/slog` only (no `fmt.Println` / `log.Printf` in prod) | structural | custom Python check | Go-quality workflow | `go-logging-discipline-files.txt` (planned) |
| G9 | Every `services/<name>/` has a `README.md` | structural | Python filesystem walk (`check_go_readme_coverage.py`) | safe-commit + Go-quality workflow | `go-readme-coverage-files.txt` (empty — clean) |
| G10 | Third-party deps require a rationale entry in `services/<name>/DEPENDENCIES.md` | structural | custom Python check | Go-quality workflow | `go-dependency-rationale-files.txt` (planned) |

G1 / G3 / G4 / G8 / G10 are **planned** — their detector scripts land
when the first real Go service does (alpha-deploy webhook for
[#272](https://github.com/three-cubes/kairix/issues/272) Phase 4). G2 /
G5 / G6 / G7 land "for free" via golangci-lint's existing rule set; the
plan-of-record reserves the rule ID so it survives reviewers asking
"shouldn't we enforce this?" — yes, we do.

G9 is **active now** because it depends only on filesystem-walk, not on
any Go source. Empty baseline; will trip if any future
`services/<name>/` lands without a README.

---

## The rules in detail

Each rule below is described with: **statement**, **why**,
**detection mechanism**, **examples** (rejected and allowed), and
**fix pattern**.

### F1 — No `@patch` on kairix internal code

#### Statement

Test files MUST NOT call `@patch("kairix.…")` or
`with patch("kairix.…")`.

#### Why

Patches couple tests to module structure (`patch("kairix.foo._helper")`
breaks silently when `_helper` is renamed or moved). They also make
production code grow defensive shims to remain mockable, which is
exactly the test-shaped-API smell.

The replacement is **constructor injection** or a **`Protocol` seam**
from `kairix.core.protocols`. `tests/fakes.py` exists for exactly this:
canonical Fake* implementations of every domain Protocol.

#### Detection

`scripts/checks/check-no-internal-patches.sh`. Grep is the right tool
here — the pattern `@patch("kairix.` is unambiguous at the line level.
The script:

```bash
grep -rEl '(@patch|with patch)\("kairix\.' tests/ --include='*.py'
```

#### Examples

```python
# REJECTED
@patch("kairix.core.search.bm25.bm25_search")
def test_pipeline_handles_bm25_failure(): ...

with patch("kairix.agents.research.graph.build_researcher_graph"):
    ...

# ALLOWED — stdlib boundary
with patch("os.path.exists", return_value=True):
    ...

# ALLOWED — external SDK boundary
with patch("openai.AzureOpenAI") as mock_client:
    ...

# ALLOWED — patches `builtins`
with patch("builtins.input", return_value="y"):
    ...
```

#### Fix pattern

Take the dependency in the constructor of the unit under test, pass a
fake from `tests/fakes.py`:

```python
# Before
def test_run_research_handles_graph_build_failure():
    with patch("kairix.agents.research.graph.build_researcher_graph",
               side_effect=RuntimeError("boom")):
        result = run_research("query")

# After
def test_run_research_handles_graph_build_failure():
    def raising_builder(**_):
        raise RuntimeError("boom")
    result = run_research("query", graph_builder=raising_builder)
```

If the production class doesn't yet have a constructor seam, **add one**
following the pattern of `GoldBuilder(llm_judge=..., retriever=...,
db_path=...)` — one keyword argument per Protocol-shaped collaborator.

#### Allowed exceptions

Patching `os.*`, `builtins.*`, `pathlib.*`, `sys.*` (stdlib boundaries)
or named external SDKs (`openai.*`, `httpx.*`, `mcp.*`) remains
allowed. The check explicitly only matches `"kairix.…"` strings.

---

### F2 — No `monkeypatch.setenv("KAIRIX_*")` in tests

#### Statement

Test files MUST NOT call `monkeypatch.setenv|setattr|delenv` on any
key starting with `KAIRIX_`.

#### Why

Per the boundary-only `KairixPaths` pattern (issue #139), env vars are
read **once at the boundary** into an immutable `KairixPaths` value
object. Inner code receives the value via convenience function or
constructor argument; it never re-reads the env.

Tests construct `KairixPaths` directly via
`tests.fakes.FakePaths(document_root=..., db_path=..., ...)`. Mutating
process env to drive paths is the test-shaped-API smell that #139
explicitly reverted.

#### Detection

`scripts/checks/check-no-env-monkeypatch.sh`:

```bash
grep -rEl 'monkeypatch\.(setenv|setattr|delenv).*KAIRIX_' tests/ --include='*.py'
```

#### Examples

```python
# REJECTED
def test_brief(monkeypatch, tmp_path):
    monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", str(tmp_path / "vault"))
    result = some_function()

# REJECTED — even setattr on os.environ
monkeypatch.setattr("os.environ", {"KAIRIX_DB_PATH": "/x"})

# ALLOWED — non-KAIRIX env (e.g. PATH for subprocess tests)
monkeypatch.setenv("PATH", "/usr/local/bin")

# ALLOWED — direct construction
def test_brief(tmp_path):
    paths = FakePaths(document_root=tmp_path / "vault")
    result = some_function(paths=paths)
```

#### Fix pattern

```python
# Before
def test_x(monkeypatch, tmp_path):
    monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", str(tmp_path))
    monkeypatch.setenv("KAIRIX_DB_PATH", str(tmp_path / "db.sqlite"))
    result = run_something()

# After
def test_x(tmp_path):
    paths = FakePaths(
        document_root=tmp_path,
        db_path=tmp_path / "db.sqlite",
    )
    result = run_something(paths=paths)
```

If `run_something()` doesn't yet take a `paths` argument, **add it** at
the call boundary. The argument is real DI — production constructs
`KairixPaths.resolve()` once at startup; tests construct `FakePaths(...)`
once per test.

---

### F3 — Suppressions require rationale

#### Statement

A bare `# NOSONAR`, `# noqa`, or `# pragma: no cover` is rejected.
The accompanying same-line rationale documents WHY the rule doesn't
apply.

#### Why

Suppressions without rationale rot. Future readers can't tell whether
the suppression is still load-bearing or whether the underlying
condition has changed. A documented suppression is a contract; an
undocumented one is decay.

The rationale also forces the author to **think** about whether the
suppression is actually correct rather than reaching for it
reflexively.

#### Detection

`scripts/checks/check-suppressions-have-rationale.sh`. Three pattern
matches against bare suppressions at end-of-line:

```
# NOSONAR        <-- rejected
# noqa           <-- rejected
# noqa: BLE001   <-- rejected (the `: code` doesn't count as rationale)
# pragma: no cover  <-- rejected
```

A line passes when content follows the suppression token (allowing
trailing whitespace).

#### Examples

```python
# REJECTED — bare suppression
x = float(user_input)  # NOSONAR
y = something()  # noqa: BLE001
def lazy_default():  # pragma: no cover
    ...

# ACCEPTED — rationale follows
x = float(user_input)  # NOSONAR — caller validates is finite via _validate_weights
y = something()  # noqa: BLE001  # broad except is the never-raise contract
def lazy_default():  # pragma: no cover  # production-only init; tests inject explicitly
    ...
```

#### Fix pattern

Append a rationale on the same line. Format conventions:

- `# NOSONAR — <one sentence why>`
- `# noqa: <RULE_CODE>  # <why>`
- `# pragma: no cover  # <why this line is genuinely untestable>`

The rationale should answer: *what about this specific occurrence
makes the rule not apply, and what would invalidate that?*

---

### F5 — No internal-name imports in tests

#### Statement

Test files MUST NOT import private names (`_x`) from `kairix.*`
modules. Importing FROM a private module path
(`kairix.foo._impl`) is also rejected.

#### Why

A test that imports `_helper` directly couples to that internal name's
existence and behaviour. Renaming the helper breaks the test silently
(if the rename happens via a refactor); deleting it breaks the test
loudly with an `ImportError`.

More importantly: the existence of such a test usually means the
public surface doesn't reach the branch the test wants to pin. That's
either dead code (delete it) or a missing public contract (add it).
Either way, the answer is not "test the private name."

#### Detection

`scripts/checks/check_no_internal_imports.py`. Python AST is required
because the rule has to distinguish:

```python
# REJECTED — importing the private name
from kairix.foo import _bar

# ALLOWED — local rename of a public name
from kairix.foo import bar as _alias
```

A regex can't cleanly match the first while excluding the second; the
AST walks `ImportFrom` nodes and inspects `alias.name` and
`alias.asname` separately.

#### Examples

```python
# REJECTED
from kairix.core.search.bm25 import _normalise_fts_query
from kairix.quality.eval.gold_builder import _validate_weights, path_title
from kairix.quality.eval.generate import _retrieve

# REJECTED — private module path
from kairix.core.search._impl import something

# ALLOWED — local rename of public name
from kairix.core.search.intent import classify as _real_classify

# ALLOWED — public names only
from kairix.quality.eval.gold_builder import GoldBuilder, path_title
```

#### Fix pattern

Drive the test through the public surface that calls the helper:

```python
# Before
from kairix.quality.eval.generate import _retrieve

def test_retrieve_returns_empty_on_index_failure():
    paths, snippets = _retrieve("any query", "recall")
    assert paths == []
    assert snippets == []

# After (drive through the public class that uses _retrieve)
def test_suite_generator_handles_index_failure():
    gen = SuiteGenerator()  # production default, no FTS index
    accepted, _, _, _ = gen.process_sampled_docs(...)
    assert accepted == []  # the swallow-on-error contract bubbles up
```

If the public surface doesn't expose the branch you're trying to test:

- The branch may be dead code → delete it.
- The branch may be a real contract that lacks a public way to trigger
  → add a Protocol method or class that exposes it.

---

### F6 — No `*_fn=None` test-only kwargs in production

#### Statement

Production functions in `kairix/*` MUST NOT take parameters whose name
ends in `_fn` and whose default is `None`, unless the parameter is
listed in the documented allow-list.

#### Why

These are the smell that triggered the #113/#114 reverts. Production
grew complexity for tests without operator value. The legitimate
seam pattern is **constructor injection at a boundary class** (e.g.
`GoldBuilder(llm_judge=, retriever=)`) — not per-helper
substitution kwargs on free functions.

The rule's bias: when in doubt, don't add a `_fn` parameter. If a
function is truly hard to test, that's a signal to extract a class
that takes the collaborator at construction time.

#### Detection

`scripts/checks/check_no_test_only_kwargs.py`. Pure structural —
inspects `FunctionDef.args` for parameters whose `arg` ends in `_fn`
with a default `Constant(value=None)`. Both positional-with-default
and keyword-only args are checked.

#### Allow-list

`.architecture/baseline/test-only-kwargs-allow.txt`:

```
# Format: module.path::function_name::param_name
# Each entry must have a real production caller passing a non-default
# value, OR be a Protocol/Adapter wiring point at a true boundary.
kairix.agents.mcp.server::tool_search::search_fn
```

The allow-list is a **separate** file from the baseline — entries are
permanent (or explicitly justified), not "to be cleaned up."

#### Examples

```python
# REJECTED
def render_report(data, *, format_fn=None):  # _fn=None smell
    if format_fn is None:
        format_fn = json.dumps
    return format_fn(data)

# ACCEPTED — at a boundary class
class ReportRenderer:
    def __init__(self, *, formatter: Callable[[dict], str] | None = None):
        self._formatter = formatter or json.dumps
    def render(self, data): return self._formatter(data)

# ACCEPTED — Protocol injection (real production wiring)
def build_pipeline(*, classifier: IntentClassifier) -> SearchPipeline:
    # IntentClassifier is a Protocol; production passes
    # RuleBasedClassifier; tests pass FakeClassifier.
    return SearchPipeline(classifier=classifier, ...)
```

#### Fix pattern

If the function is small and the `_fn=None` is genuinely test-only:
delete it and refactor the test to drive through a public surface that
already constructs the right collaborator.

If the function has multiple stateful collaborators: extract a class
and make them constructor kwargs. The class follows the
`GoldBuilder(llm_judge=, retriever=, db_path=)` pattern: every
collaborator is named, typed by Protocol where one exists, and
defaults to lazy construction of the production implementation when
omitted.

---

### F7 — Per-file coverage floor at 85%

#### Statement

Every file in `coverage.xml` (kairix/* sources, post-omit) MUST be
≥ 85% line-covered.

#### Why

Repository-wide coverage averages can hide files at 0%. A 91% repo
average where 50 files are at 100% and 1 file is at 0% looks healthy
but isn't. Per-file is the correct unit of measurement.

The 85% floor is intentionally above the global 80% threshold — it
applies per-file, not in aggregate. A file at exactly 85% passes.
Files at 84.99% fail.

#### Detection

`scripts/checks/check_per_file_coverage.py`. Reads
`coverage.xml` (Cobertura format, emitted by `pytest --cov-report=xml`).
Iterates every `<class>` element matching `kairix/*`, extracts
`line-rate`, fails if any file is below the floor and not in the
baseline.

#### Where it runs

Only in CI's `unit-and-type` job, immediately after pytest emits
`coverage.xml`. Pre-commit doesn't run F7 because it would require a
full test run on every commit (too slow). `safe-commit.sh` doesn't
run F7 for the same reason — the orchestrator skips it via the
`--skip-coverage` flag.

#### Relationship to Codecov

F7 is the **mechanical** floor — it blocks the merge regardless of
Codecov's status. Codecov complements F7 with:

- **Two coverage flags**: `unit` (Stage 2 — `pytest -m "unit or bdd or
  contract"`) and `integration` (Stage 3 — `pytest -m integration`),
  both with carryforward enabled in `codecov.yml`. The two flags merge
  in the dashboard so production-wiring files only exercised at
  integration scope (`factory.py`, `mcp/server.py`) show their real
  coverage rather than a false 0% from the unit run.
- **Patch target = 85%** in `codecov.yml` — applies the F7 bar to the
  PR diff itself, so a PR that adds new code at <85% is rejected.
- **Components** (Search / Agents / Knowledge / Quality / Core) for
  per-area regression tracking on top of the file-level floor.
- **Test analytics** via `codecov/test-results-action@v1` (uploaded
  from contracts, unit, and integration jobs) — flaky-test detection
  and slow-test trends, separate from coverage signal.

`pyproject.toml`'s `[tool.coverage.run].omit` list is the only place
files are excluded from measurement; `codecov.yml` deliberately has no
`ignore:` block to prevent omit-list drift.

#### Fix pattern

Add tests that drive the public surface exercising the uncovered
lines. Specifically:

- **CLI dispatch files** — extend BDD scenarios to drive the `cmd_*`
  function with appropriate setup, OR refactor the CLI body so the
  orchestration is a thin adapter around an already-covered use case
  (#168 will do this systematically).
- **Production wiring files** (`factory.py`, `mcp/server.py`) — these
  are exercised by integration tests that don't currently feed the
  unit-coverage measurement. The CI workflow uploads integration
  coverage to Codecov with `flags: integration` so the patch-coverage
  measurement counts them. F7 itself only inspects `coverage.xml` from
  the unit run, so a file exercised purely at integration scope still
  fails F7 unless it has unit tests too — the architectural signal is
  to make sure the testable logic in those files isn't trapped behind
  integration-only seams.
- **Real testable logic** — write tests that drive the public surface.

**Do not** add `# pragma: no cover` to silence the gate. That's the
suppression F3 explicitly rejects unless rationale-documented, and a
pragma to defeat F7 should be a last resort.

---

### F4 — No `os.environ.get("KAIRIX_*")` outside `paths.py` / `secrets.py`

#### Statement

Production files in `kairix/*` MUST NOT read `KAIRIX_*` environment
variables anywhere except `kairix/paths.py` (paths) and
`kairix/secrets.py` (credentials).

#### Why

Per the boundary-only `KairixPaths` pattern (#139), env vars are read
**once at the boundary**. F2 catches the test side
(`monkeypatch.setenv("KAIRIX_*")`); F4 catches the production side
(scattered `os.environ.get("KAIRIX_*")` calls).

A `KAIRIX_*` read in any other module means the production code is
bypassing `KairixPaths` — which leaks env-var coupling across modules
and prevents tests from injecting paths cleanly. Both anti-patterns
are documented in #139's closure.

#### Detection

`scripts/checks/check-env-reads-stay-in-paths.sh`:

```bash
grep -rEl 'os\.environ.*KAIRIX_' kairix/ --include='*.py' \
    | grep -vE '^kairix/(paths|secrets)\.py$'
```

Matches `os.environ.get("KAIRIX_X")`, `os.environ["KAIRIX_X"]`, and
`os.environ.pop("KAIRIX_X")` — any read or mutation of a `KAIRIX_*`
key. Allow-listed locations are `kairix/paths.py` and
`kairix/secrets.py`.

#### Examples

```python
# REJECTED — production module other than paths.py/secrets.py
# kairix/agents/briefing/cli.py
default_root = os.environ.get("KAIRIX_AGENT_MEMORY_ROOT", "/data/agents")

# ACCEPTED — kairix/paths.py is the canonical boundary
def _resolve_cached() -> KairixPaths:
    document_root = Path(
        os.environ.get("KAIRIX_DOCUMENT_ROOT")
        or _config_path("document_root")
        or str(_default_document_root())
    ).expanduser()
    ...

# ACCEPTED — kairix/secrets.py for credentials
api_key = os.environ.get("KAIRIX_AZURE_API_KEY", "")
```

#### Fix pattern

Move the env-var read into `KairixPaths.resolve()` (or
`secrets.get_credentials()` for secrets) and expose the resolved value
as a field. Inner code reads `KairixPaths.resolve().<field>`:

```python
# Before
# kairix/agents/briefing/cli.py
default_root = os.environ.get("KAIRIX_AGENT_MEMORY_ROOT")

# After
# kairix/paths.py — single env-var read, exposed as a field
@dataclass(frozen=True)
class KairixPaths:
    agent_memory_root: Path
    ...
    @classmethod
    def resolve(cls):
        return _resolve_cached()  # reads KAIRIX_AGENT_MEMORY_ROOT once

# kairix/agents/briefing/cli.py — uses the resolved value
default_root = KairixPaths.resolve().agent_memory_root
```

---

### F8 — Every `test_*` function has a category marker

#### Statement

Every test function pytest would collect MUST declare its category via
a marker in the recognised set: `unit`, `bdd`, `contract`, `integration`,
`e2e`, `slow` (the marker list registered in
`[tool.pytest.ini_options]` in `pyproject.toml`).

A test function passes when AT LEAST ONE of the following carries a
recognised marker:

  - The function: `@pytest.mark.<category>` decorator.
  - The enclosing class: `@pytest.mark.<category>` class decorator OR
    `pytestmark = pytest.mark.<category>` (or list-form) class attribute.
  - The module: `pytestmark = pytest.mark.<category>` (or list-form)
    module-level assignment.

Pytest fixtures (`@pytest.fixture`-decorated functions) are excluded
even when their name starts with `test_` — pytest distinguishes by
decorator, not by name.

#### Why

The test-pyramid filter (`pytest -m unit`, `pytest -m contract`, etc.)
is only meaningful when every test declares its category. An unmarked
test runs in EVERY filter, defeating the pyramid: a "unit-only" run
silently picks up integration tests; a "contract-only" run picks up
unit tests. The selectivity collapses.

This is not theoretical: kairix relies on pyramid filters in
`safe-commit.sh` (`-m "unit or bdd or contract"`) and across CI stages
(unit-and-type, contracts, integration). One unmarked test
contaminates every selection it lives in.

#### Detection

Python AST walk over `tests/**.py`, in
`scripts/checks/check_test_markers.py`. For each module:

  1. If the module has a category-marker `pytestmark` assignment, it
     passes (covers all tests in the file).
  2. Otherwise, for each top-level `def test_*` (excluding fixtures):
     check for a category-marker decorator on the function.
  3. For each top-level `class`: check whether the class is marked
     (class-level `pytestmark` OR class-level `@pytest.mark.<category>`
     decorator). If marked, every method passes; if not, each
     `def test_*` method must carry its own decorator.

Markers other than the recognised set (e.g. `@pytest.mark.parametrize`,
`@pytest.mark.skipif`) do NOT count — only the registered category
markers do.

#### Examples

Rejected:
```python
# tests/foo/test_bar.py — unmarked test
def test_load_config_returns_value():     # ❌ no category marker
    ...

@pytest.mark.parametrize("x", [1, 2])     # ❌ parametrize is not a category
def test_xs(x):
    ...
```

Allowed:
```python
# Function-level marker
@pytest.mark.unit
def test_load_config_returns_value():
    ...

# Module-level marker covers every test in the file
import pytest
pytestmark = pytest.mark.contract

def test_protocol_compliance():            # ✅ inherits module mark
    ...

# Class-level decorator covers every method in the class
@pytest.mark.contract
class TestCollectionDefaults:
    def test_default_collection(self):     # ✅ inherits class mark
        ...

# Fixture named test_* is fine — pytest never collects it as a test
@pytest.fixture
def test_vault_root(tmp_path):             # ✅ fixture, not a test
    return tmp_path / "vault"
```

#### Fix pattern

Pick the marker that matches the test's tier:

| Tier | Marker | Where it lives |
|------|--------|----------------|
| Pure unit, no I/O | `unit` | `tests/unit/`, most of `tests/` |
| Behaviour-driven scenarios | `bdd` | `tests/bdd/` |
| Protocol compliance | `contract` | `tests/contracts/` |
| Real DB / external | `integration` | `tests/integration/` |
| End-to-end pipelines | `e2e` | `tests/e2e/` |
| Anything > 5s | `slow` | (orthogonal — combine with tier) |

If every test in a file is the same tier, prefer module-level
`pytestmark` over decorating each function.

#### Allowed exceptions

None by default — F8 ships with a clean (zero-file) baseline. If a
genuinely uncategorisable test exists, append the file to
`.architecture/baseline/test-markers-files.txt` with a PR-description
rationale. Expect pushback at review.

---

### F9 — Per-file 85% floor on union coverage

#### Statement

Every kairix/* source file in the **union** of unit and integration
coverage must be ≥ 85% line-covered. F9 is the **holistic** version
of F7's atomic per-file floor, in the sense of Ford / Sadalage / Kua's
*Building Evolutionary Architectures* — it tests "did the system
collectively cover this code" rather than "did one specific scope
cover this code."

#### Why

F7 alone gates the unit run. Files exercised only at integration
scope — `factory.py`, `mcp/server.py`, `db/repository.py`, certain
adapter modules — measure as 0% in the unit run and end up
grandfathered in the F7 baseline forever, even though they're well
exercised by integration tests. F9 closes that loop: an integration
test that drives a previously-uncovered production-wiring file gets
credit, and the file leaves the F9 baseline.

This matches the canonical guidance from ThoughtWorks' *Building
Evolutionary Architectures*: where atomic functions test one
dimension, holistic functions test cross-cutting properties of the
whole system. Coverage union is exactly that shape.

#### Detection

Stage 5 of the CI pipeline:

  1. Stage 2 (unit-and-type) writes `.coverage.unit` via
     `COVERAGE_FILE` and uploads it as the `coverage-data` artifact.
  2. Stage 3 (integration) writes `.coverage.integration` via
     `COVERAGE_FILE` and uploads it as the `coverage-data-integration`
     artifact.
  3. Stage 5 downloads both, runs ``coverage combine --keep
     .coverage.unit .coverage.integration`` to produce a unified
     `.coverage` database, exports it to `coverage-union.xml`, and
     runs ``check_per_file_coverage.py coverage-union.xml
     per-file-coverage-floor-union``.

The per-file 85% floor is identical to F7's; only the source data
differs. The baseline lives in
`.architecture/baseline/per-file-coverage-floor-union-files.txt` and
is independent of F7's baseline so they ratchet independently.

#### Where it runs

Only in CI (Stage 5). Pre-commit and `safe-commit.sh` skip F9 for
the same reason they skip F7 — running both unit + integration
suites on every commit is too slow.

#### Fix pattern

The same as F7, with the additional shortcut: a file that's
production-wiring (e.g. `factory.py`) and exercised only via
integration tests can leave the F9 baseline as soon as those
integration tests are written, **without requiring unit-level
coverage**. This is the legitimate use-case Ford et al. describe —
some code's natural test scope is integration; F9 lets it earn
its keep there.

**Do not** use F9 as a way to avoid writing unit tests for code
that has unit-testable logic. F7 is still in effect for every file
F7 already grandfathers — F9 is a *complement* to F7, not a relaxation.

#### References

  - Ford, Parsons, Kua, *Building Evolutionary Architectures* (2017,
    O'Reilly) — atomic vs holistic fitness functions.
  - `coverage combine` reference:
    https://coverage.readthedocs.io/en/latest/cmd.html#combining-data-files-coverage-combine

---

### F10 — CI workflow silencers require rationale

#### Statement

Every `continue-on-error: true` and `fail_ci_if_error: false` in
`.github/workflows/*.yml` MUST have a same-line trailing comment
explaining why the silencer is intentional. Bare uses are rejected.

#### Why

CI workflow silencers are the most invisible quality bypass available
to agents — failure stops being a signal but the build still goes
green. Each silencer can be legitimate (Codecov outage shouldn't
block the merge; a fork PR with no token can't render a coverage
comment) but each must have a written reason or it's just noise.

The user-reported smell that drove this rule: "are there workarounds
agents have access to that bypass quality bars?" The answer was yes,
and a sweep of `ci.yml` showed nine bare silencers with no rationale.

#### Detection

`scripts/checks/check-workflow-silencers-have-rationale.sh`. Greps
for the bare patterns `continue-on-error: true$` and
`fail_ci_if_error: false$` (no trailing comment). A file is a
violation if any silencer line in it lacks a same-line `#`-comment.

#### Examples

Rejected:
```yaml
      - name: Upload coverage
        uses: codecov/codecov-action@v5
        with:
          fail_ci_if_error: false   # bare — no rationale
```

Allowed:
```yaml
      - name: Upload coverage
        uses: codecov/codecov-action@v5
        with:
          fail_ci_if_error: false  # codecov outage / rate-limit must not block merge — F7 is the mechanical floor, not Codecov
```

#### Fix pattern

For every flagged silencer, either DELETE it (preferred — make CI
fail loudly) or document why with a same-line comment. The rationale
is read at every code review; "we copied this from another workflow"
is not a rationale.

#### Limits

`--cov-fail-under=0` and similar pytest-CLI silencers are not covered
by F10 because they're line-continuation arguments inside `run:`
blocks where same-line comments don't render. Their rationale lives
in the surrounding YAML `#`-comment block. There's only one such
silencer (in the integration job) and it's documented.

---

### F11 — Test skip mechanisms require rationale

#### Statement

Every `pytest.mark.skip`, `pytest.mark.skipif`, `pytest.mark.xfail`,
and `pytest.importorskip(...)` MUST declare a rationale, either as a
`reason=` kwarg or as a same-line / immediately-preceding `#`-comment.

#### Why

A silently-skipping test is a worse signal than a missing test — it
looks present but never runs. The starlette/transport regression in
this branch is the canonical example: the unit test for
`kairix/agents/mcp/transport.py` silently skipped on missing
starlette, F7 saw 0% coverage on the file, and the gate failed.
With a rationale, that skip would be visible from the diff.

#### Detection

`scripts/checks/check_test_skip_rationale.py`. AST walk over
`tests/**.py`. Inspects:

  - Function/class decorators: `@pytest.mark.skip` / `skipif` / `xfail`
    must be a Call (not bare Attribute) and must have a non-empty
    `reason=` kwarg.
  - Module-level `pytestmark = pytest.mark.skip(...)` assignments.
  - `pytest.importorskip("X")` calls — accept `reason=` kwarg, a
    same-line trailing comment, or an immediately-preceding `#`-comment
    block (within 3 lines, no blank-line gap).

#### Examples

Rejected:
```python
@pytest.mark.skip                           # bare — no reason
def test_x(): ...

@pytest.mark.skipif(sys.platform == "win32") # no reason kwarg
def test_y(): ...

pytest.importorskip("foo")                  # no reason, no preceding comment
```

Allowed:
```python
@pytest.mark.skip(reason="see #999 — fixture rewrite in progress")
def test_x(): ...

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only path")
def test_y(): ...

# Skip when the optional [agents] extras aren't installed — the
# transport module imports starlette at module level.
pytest.importorskip("starlette")

pytest.importorskip("yaml", reason="config loader uses PyYAML; skip when not installed")
```

#### Fix pattern

Add a rationale. If the test is broken, fix it; if the dependency is
mandatory, install it (this PR did exactly that for starlette + the
unit-and-type job); if the test is duplicated by integration coverage,
delete it.

---

### F12 — Every BDD feature has a happy-path scenario

#### Statement

Every `tests/bdd/features/*.feature` file MUST contain at least one
scenario whose preceding tag block does NOT include any of `@error`,
`@negative`, `@failure`, `@unhappy`, `@error-path`. A feature with
zero scenarios also fails.

#### Why

The user-reported smell: "many of our BDD tests only document error
states." Per Adzic *Specification by Example* and Wynne *The Cucumber
Book*, a feature exists to document a *capability* — the capability
needs a positive scenario showing what success looks like before we
enumerate failure modes. A feature whose scenarios are all
`@error`/`@negative` is an error catalogue, not a specification of
stakeholder value.

Liz Keogh calls this "test infection of BDD" — scenarios written from
the test author's perspective rather than the stakeholder's.

#### Detection

`scripts/checks/check_bdd_happy_path.py`. Parses each `.feature` file
line-by-line:

  1. Find every `Scenario:` / `Scenario Outline:` line.
  2. Walk backward to collect tag lines (`^\s*@`) until a blank or
     non-tag line.
  3. A scenario is happy-path if its tag set is disjoint from
     `{@error, @negative, @failure, @unhappy, @error-path}`.
  4. The feature passes if it has ≥1 happy-path scenario.

Untagged scenarios count as happy-path (untagged is the positive-flow
default).

#### Examples

Rejected (errors-only catalogue):
```gherkin
Feature: Benchmark error handling

  @error
  Scenario: Invalid YAML rejected
    Given a malformed suite
    When the operator runs the benchmark
    Then an error is shown

  @negative
  Scenario: Missing gold path rejected
    Given a suite with a missing gold reference
    When the operator runs the benchmark
    Then an error is shown
```

Allowed:
```gherkin
Feature: Benchmark suite execution

  Scenario: Operator runs a suite and sees scores
    Given a valid benchmark suite
    When the operator runs the benchmark
    Then the result shows category scores

  @error
  Scenario: Invalid YAML rejected
    Given a malformed suite
    When the operator runs the benchmark
    Then an error is shown
```

#### Fix pattern

Add at least one positive-flow scenario. If the feature is genuinely
about an error mode (uncommon), the right home is probably a
narrower "errors" feature explicitly named so — at which point the
positive-flow scenario lives in the parent feature.

#### References

  - Adzic, *Specification by Example* (2011) — features describe
    capabilities, not exception cases.
  - Wynne, Hellesøy, *The Cucumber Book* — every feature has a
    must-work golden path.
  - Liz Keogh, "Step Away From The Tools" — BDD test-infection.

---

### F13 — BDD scenarios reject implementation symbols

#### Statement

`.feature` files MUST NOT contain references to test-framework
internals (`Mock`, `MagicMock`, `monkeypatch`, `pytest.`, `unittest.`)
or kairix internal module paths (`kairix.<package>.<symbol>`). The
config-file name `kairix.config.yaml` and similar `.yaml`/`.yml`/
`.json` filenames are explicitly allowed.

#### Why

Per Dan North (BDD), scenarios describe stakeholder *outcomes*, not
the code that implements them. A scenario that mentions `Mock` or
`kairix.core.search.bm25.bm25_search` is not a specification — it's
a unit test masquerading as one. Liz Keogh calls this "scenario
describes internals."

The rule complements F12: F12 catches "the feature only documents
errors"; F13 catches "the feature describes how the code works
instead of what the user sees."

#### Detection

`scripts/checks/check_bdd_no_implementation_leaks.py`. Per `.feature`
file, scans every non-comment line for forbidden tokens:

  - Exact matches: `Mock`, `MagicMock`, `monkeypatch`
  - Prefix matches: `pytest.`, `unittest.`
  - Module-path pattern: `kairix\.[a-z_]+\.[a-z_]+` *unless* the
    third segment is one of the allow-listed file extensions
    (`yaml`, `yml`, `json`, `toml`, `py`, `md`, `txt`, `xml`, `lock`,
    `feature`).

#### Examples

Rejected:
```gherkin
  Scenario: Mock benchmark produces category scores
    Given the operator runs the test
    When kairix.core.search.bm25.bm25_search executes
    Then a Mock is returned
```

Allowed:
```gherkin
  Scenario: Operator runs a benchmark
    Given the operator has a kairix.config.yaml with reflib enabled
    When they run the benchmark suite
    Then they see the score summary
```

#### Fix pattern

Rewrite in stakeholder language. If the scenario is genuinely about
internals, it does not belong in `tests/bdd/features/` — move it to a
unit test where it belongs.

#### Limits

F13 catches only the hard symbol leaks. Soft leaks ("the code", "the
function does X") and abstraction-level concerns (whether the
scenario describes a business outcome at all) are Three Amigos /
human-review concerns; see the aspirational practices issue.

#### References

  - Dan North, "Introducing BDD" (2006) — outcomes vs implementation.
  - Liz Keogh on BDD test-infection.

---

### F16 — Cognitive complexity ≤ 15 per function

#### Statement

No function in `kairix/**` may exceed a cognitive-complexity score of
**15** (SonarSource S3776 default).

#### Why

Cognitive complexity (Campbell, 2018) measures how hard the code is to
*read*, not how hard it is to test. The score climbs with each branch
and is amplified by nesting depth — a triple-nested `if` is harder to
follow than three sequential `if` statements. Sonar's PR #247 burndown
surfaced 46 files above the ceiling; F16 prevents regression and gives
the agent a single canonical refactor pattern (extract helper / early
return / dispatch dict).

#### Detection

`scripts/checks/check_cognitive_complexity.py`. AST walks each
`FunctionDef` / `AsyncFunctionDef` and applies the SonarSource scoring
rules: +1 per `if`/`elif`/`else`/`for`/`while`/`try`/`except`/ternary,
+1 per boolean operator in conditions, plus nesting amplifier.

#### Examples

Rejected (score 12+ for a single function — too tall to follow):

```python
def dispatch(cmd, args):
    if cmd == 'search':
        if not args:
            for item in default_items():
                if item.starred:
                    if item.is_remote and item.is_local:
                        ...
    elif cmd == 'index':
        ...
```

Allowed (dispatch dict + helpers — every branch reads in isolation):

```python
_HANDLERS = {'search': _handle_search, 'index': _handle_index}

def dispatch(cmd, args):
    return _HANDLERS.get(cmd, _default_handler)(args)
```

See `kairix/worker.py::WorkerDeps` for the dataclass-extraction pattern
that flattens orchestrator complexity by moving collaborators onto a
single `Deps` object.

---

### F17 — No string literal ≥10 chars duplicated ≥3 times in a module

#### Statement

No string literal of at least 10 characters may appear 3+ times in the
same `kairix/**` module without being extracted to a module-level
constant.

#### Why

Sonar S1192: a duplicated string literal is a refactor smell — the
reader can't tell whether the three sites are *coupled* (they all
reference the same conceptual thing and should change together) or
*coincidentally identical* (renaming one shouldn't affect the others).
Extracting to an UPPER_SNAKE constant makes the coupling explicit and
gives renaming a single edit site.

#### Detection

`scripts/checks/check_no_duplicate_string.py`. AST walks
`ast.Constant`-of-str nodes per file, skipping docstrings and
whitespace-only values, and counts occurrences.

#### Examples

Rejected:

```python
def search(q):
    if not q: raise ValueError("search query must be a non-empty string")
def reindex(q):
    if not q: raise ValueError("search query must be a non-empty string")
def validate(q):
    if not q: raise ValueError("search query must be a non-empty string")
```

Allowed:

```python
_ERROR_BAD_QUERY = "search query must be a non-empty string"

def search(q):
    if not q: raise ValueError(_ERROR_BAD_QUERY)
def reindex(q):
    if not q: raise ValueError(_ERROR_BAD_QUERY)
```

---

### F18 — No commented-out code

#### Statement

A run of 3+ consecutive `#`-prefixed lines in `kairix/**` whose stripped
content lexes as valid Python is a violation.

#### Why

Sonar S125: git history is the archive. Commented-out code accumulates
confusion — is this still relevant? was it disabled in a hurry? is this
the intended replacement for the line below? `git log -p <file>`
recovers any prior state if anyone needs it.

#### Detection

`scripts/checks/check_no_commented_out_code.py`. Line-by-line scan
identifies contiguous `#`-prefixed runs (skipping shebangs, directives
like `# type:`, `# pyright:`, `# noqa`, and docstring lines). Each run
is dedented and passed to `ast.parse`; if it parses AND contains a
syntactic anchor (assignment, call, `def`, `if`, etc.) it's flagged.

#### Examples

Rejected:

```python
# old_path = path.replace('/old/', '/new/')
# if old_path.startswith('/data'):
#     old_path = old_path[6:]
# return old_path
def new_function():
    return new_path()
```

Allowed (real prose):

```python
# Strip leading slash so we can join cleanly with PathLib.
path = path.lstrip('/')
```

If the dead code might come back, reference a ticket instead:
`# TODO #251 — re-enable after refactor`.

---

### F19 — Unused function parameters must be `_`-prefixed

#### Statement

Any non-`_`-prefixed parameter that is never read in the function body
is a violation, unless the function is abstract, an `@overload` stub, a
property setter (`value`), or the parameter is `self`/`cls`/`*args`/
`**kwargs`.

#### Why

Sonar S1172. The fix is one of:

  - **Delete** the parameter if no Protocol/abstract base requires it.
  - **Rename to `_unused`** if the position is required by a Protocol
    that the implementation doesn't need.

The `_`-prefix is the explicit signal that the unused parameter is
load-bearing for the contract, not just leftover code.

#### Detection

`scripts/checks/check_unused_params_named.py`. AST walks each
`FunctionDef` / `AsyncFunctionDef`, collects parameter names, and
checks each against names referenced (Load context) in the body.

#### Examples

Rejected:

```python
def handle(event: Event, context: Context) -> Result:
    return Result(event.id)  # context never used
```

Allowed (Protocol requires both; this impl only uses `event`):

```python
def handle(event: Event, _context: Context) -> Result:
    return Result(event.id)
```

Allowed (no Protocol requires `context` — delete it):

```python
def handle(event: Event) -> Result:
    return Result(event.id)
```

---

### F20 — Empty function bodies require docstring or intent comment

#### Statement

Any `FunctionDef` / `AsyncFunctionDef` whose body is exactly `pass`,
`...`, or `docstring-only + pass/...` must carry either a docstring OR
an `# Intentionally empty — <reason>` comment in the function span (or
on the line above `def`).

Abstract methods (`@abstractmethod`), `@overload` stubs, and bodies
that are `raise NotImplementedError` are exempt.

#### Why

Sonar S1186. An empty body without explanation is indistinguishable
from a truncated/forgotten implementation. The docstring or intent
comment is the receipt that the emptiness is deliberate.

#### Detection

`scripts/checks/check_empty_body_intent.py`. AST walks each function,
detects empty-body shapes, and checks for a leading docstring or an
`Intentionally empty` comment in the function's source span.

#### Examples

Rejected:

```python
class Handler:
    def on_event(self, event):
        pass

    def shutdown(self): ...
```

Allowed:

```python
class Handler:
    def on_event(self, event):
        """No-op default; concrete strategies override this."""

    def shutdown(self):
        # Intentionally empty — Protocol-required method that some
        # adapters genuinely don't need.
        pass
```

---

### F21 — Check-script failure output must carry an action marker

#### Statement

Every fitness-function check under `scripts/checks/` MUST emit failure
text (REMEDIATION constant, error-list append, shell `echo`/here-doc)
that contains at least one of the three lowercase action markers:

- `fix:` — a sentence describing how to correct the violation.
- `next:` — what to do after the fix (re-run, re-check, etc.).
- `run:` — an exact command to copy-paste.

Allow-listed: `_arch_lib.py`, `_lib.sh`, `run-all.sh`,
`audit_baselines.py`, `merge_coverage_xml.py` — shared helpers and the
harness/orchestrator (no per-rule remediation of their own).

#### Why

Convergence with sibling-repo fitness functions (issue #258). A check that fails with
"AssertionError" or a REMEDIATION that only describes the offence
wastes one full agent loop while the cure is re-derived. The markers
turn the failure into an actionable instruction. The verbose
"Refactor to YYY to pass. Pass example: ... Forbidden example: ..."
shape used by F15 / F16 / F20 is an acceptable *extension* — F21 only
requires the minimum: one marker.

#### Detection

`scripts/checks/check_actionable_feedback.py`. AST-based for Python
check scripts (module-level `REMEDIATION = "..."` and
`errors.append(...)` / `violations.append(...)` literals) plus
regex-based for shell scripts (`REMEDIATION="..."` blocks and bare
`echo`/here-doc text). A file with NO detectable remediation text is
also treated as a violation, so silent check scripts can't bypass the
rule. The detector deliberately scans itself — F21's own REMEDIATION
must satisfy F21 (dogfood).

#### Examples

Rejected:

```python
REMEDIATION = "Some files violate the rule. Please update them."
```

Allowed (minimum — one marker):

```python
REMEDIATION = "fix: rewrite the affected REMEDIATION to include an action."
```

Allowed (richer extension — preferred for new checks; matches F15/F20):

```python
REMEDIATION = """Refactor to constructor-injected fakes to pass.

fix: take the dependency as a kwarg of the unit under test and pass a
Fake* from tests/fakes.py.
next: re-run pytest tests/<dir>/ to confirm green.
run: bash scripts/safe-commit.sh "test(<area>): inject fake instead of patch"

Pass example:
  pipeline = SearchPipeline(retriever=FakeRetriever(hits=[...]))

Forbidden example:
  @patch("kairix.core.search.bm25.bm25_search")
  def test_search_returns_hits(mock): ...
"""
```

#### Fix pattern

Open the failing check script, locate the REMEDIATION constant (or
the appended error string), and prepend `fix: <one-line action>` plus
optionally `next: <follow-up>` and `run: <exact command>`. Re-run
`python3 scripts/checks/check_actionable_feedback.py` to confirm.

The pre-existing kairix check scripts use the "Refactor to … to pass."
phrasing, which is descriptive but doesn't carry a literal marker —
they are grandfathered in
`.architecture/baseline/actionable-feedback-files.txt` until each one
is rewritten in a baseline-burndown follow-up.

### F22 — Repo paths follow per-tree naming conventions

#### Statement

Every tracked file under a registered tree-prefix MUST satisfy the
naming regex for that tree. The trees and their rules (first match
wins):

| Tree prefix | Trigger | Allowed basenames |
|-------------|---------|-------------------|
| `kairix/` | `*.py` | `__init__.py`, `conftest.py`, `fakes.py`, or `_?snake_case.py` (leading `_` permitted for private modules) |
| `tests/bdd/features/` | `*.feature` | `snake_case.feature` |
| `tests/bdd/steps/` | `*.py` | `__init__.py`, `conftest.py`, `fakes.py`, or `_?snake_case.py` |
| `tests/` (excl. `tests/bdd/`) | `*.py` | `test_<thing>.py`, `conftest.py`, `fakes.py`, `__init__.py`, or `_?snake_case.py` helpers |
| `scripts/checks/` | `*.py` | `check_<rule>.py`, `_arch_lib.py`, `audit_baselines.py`, `merge_coverage_xml.py` |
| `scripts/checks/` | `*.sh` | `check-<rule>.sh`, `check_<rule>.sh`, `_lib.sh`, `run-all.sh` |
| `docs/operations/runbooks/` | `*.md` | `INDEX.md` or `kebab-case.md` |
| `docs/runbooks/` | `*.md` | `INDEX.md` or `kebab-case.md` |
| `.architecture/baseline/` | `*.txt` | `<rule-name>-files.txt` |

Files outside every registered tree (top-level config, `.github/`,
`docker/`, `reference-library/`, etc.) are not constrained by F22.
Convergence with a sibling repo's `path_naming.py` check (issue #258);
kairix uses its own repo layout.

#### Why

Agents and humans cross-reference paths constantly — in CLAUDE.md, in
runbooks, in error messages, in commit bodies. A consistent shape per
tree means a path mentioned in one place is greppable everywhere.
Mixed shapes (`Search-Pipeline.py` next to `pipeline.py`,
`PipelineTest.py` next to `test_pipeline.py`) force the reader to
guess which convention applies — and pytest collection silently drops
the non-conforming one.

#### Detection

`scripts/checks/check_path_naming.py`. Walks `git ls-files`; for each
tracked path, picks the first tree-rule whose prefix and suffix
trigger both match; checks the basename against that rule's regex
tuple. Out-of-scope paths pass silently.

#### Examples

Rejected:

```
kairix/core/Search-Pipeline.py            # PascalCase + dashes
tests/search/PipelineTest.py              # not test_<thing>.py
tests/bdd/features/SearchReturnsHits.feature
scripts/checks/CheckPathNaming.py         # not check_<rule>.py
docs/runbooks/my_runbook.md               # snake_case, want kebab
```

Allowed:

```
kairix/core/search/pipeline.py
kairix/providers/_base.py                 # leading-underscore private
tests/search/test_pipeline.py
tests/bdd/features/search_returns_hits.feature
scripts/checks/check_path_naming.py
docs/operations/runbooks/how-to-debug-search-ranking.md
.architecture/baseline/path-naming-files.txt
```

#### Fix pattern

Rename the file to fit its tree (use `git mv` so history follows),
update every import / reference that points at the old name, re-run
`python3 scripts/checks/check_path_naming.py`. If the file is in an
unfamiliar tree, check the rule table at the top of the check script
— that's the source of truth.

### F23 — Every top-level directory has a `README.md`

#### Statement

Every top-level directory under the repo root MUST contain a
`README.md` orientation file, unless it's allow-listed. The
allow-list (intentionally narrow) covers `.git`, `.github`,
`.pytest_cache`, `.ruff_cache`, `.architecture`, `.claude`, `.idea`,
`.vscode`, `.venv`, `__pycache__`, `htmlcov`, `logs`, `node_modules`,
`coverage`, `dist`, `build`, and any directory whose name starts
with `.` (dotfile config trees in general).

Convergence with a sibling repo's `repo_ia.py` IA1 check (issue #258).

#### Why

Every directory mention in CLAUDE.md, docs/, or an error message
becomes a click. Landing in a bare directory wastes the click and
makes the reader spelunk for context. The resolver-README pattern
(every top-level dir has one) means every path mention lands
somewhere oriented — what belongs here, what doesn't, where the
canonical docs live.

#### Detection

`scripts/checks/check_readme_coverage.py`. Walks `REPO_ROOT.iterdir()`
for directories; subtracts the allow-list; flags any remaining
directory whose `<dir>/README.md` is not a regular file. The baseline
records the *missing* README paths (i.e. the files that should exist
but don't), so a baseline burndown is "write the README and remove
the line."

#### Examples

Rejected:

```
benchmark-results/                        # no README.md
docs/                                     # no README.md (yes, really)
kairix/                                   # no README.md — the package!
```

Allowed:

```
docker/README.md                          # exists
reference-library/README.md               # exists
```

Allow-listed (no README required):

```
.git/, .github/, .architecture/, __pycache__/, htmlcov/, ...
```

#### Fix pattern

Write a one-screen `<dir>/README.md` with three sections:

1. **What this directory holds** — one sentence.
2. **What does not belong here** — one or two anti-patterns.
3. **Where the canonical docs live** — link to `docs/...`.

Then delete the corresponding line from
`.architecture/baseline/readme-coverage-files.txt`. The baseline is
expected to shrink monotonically.

### F24 — No imports of `tests.*` in `kairix/` production code

#### Statement

Production code under `kairix/**/*.py` MUST NOT contain any
`from tests.<...> import <...>` or `import tests[...]` statement.
The `tests/` package is excluded from the published wheel by
`setuptools` packaging configuration — any production reference to
`tests.*` works on a dev checkout (where pytest puts the repo root
on `sys.path`) but raises `ModuleNotFoundError: No module named
'tests'` the moment an end user `pip install`s kairix.

This rule was created in response to the v2026.5.15.1 → v2026.5.15.2
incident: a production module had `from tests.fakes import
FakeVectorRepository` as a default-parameter import. CI was green
(tests run from the repo, `tests/` is importable). The first end
user who ran the installed wheel hit a boot-time crash. F24 codifies
that mistake into a mechanical gate. Issue #266.

#### Why

The wheel doesn't ship `tests/`. Anything in `tests/fakes.py` or
`tests/conftest.py` is invisible to a `pip install` user. Imports of
`tests.*` in production therefore break the installed posture, even
though they "work" locally. The only way to catch this *before*
release is to forbid the import shape outright — by the time it
shows up in a release-candidate smoke test, the dogfood loop has
already swallowed the noise.

#### Detection

`scripts/checks/check_no_test_imports_in_prod.py`. AST-walks every
`kairix/**/*.py` file:

  - `ast.ImportFrom` where `node.module` is `"tests"` or starts with
    `"tests."` → flagged.
  - `ast.Import` where any `alias.name` is `"tests"` or starts with
    `"tests."` → flagged.

The baseline at
`.architecture/baseline/no-test-imports-in-prod-files.txt` ships
empty — the v2026.5.15.2 release cleaned out the only known
violation. Net-new violations block at pre-commit, in
`safe-commit.sh`, and in CI Stage 0.

#### Examples

Rejected:

```python
# kairix/core/search/pipeline.py
from tests.fakes import FakeVectorRepository      # tests/ not in wheel
import tests                                      # ditto
from tests import fakes                           # ditto
from tests.fixtures.docs import SAMPLE_PAYLOAD    # ditto, deeper path
```

Allowed:

```python
# kairix/core/search/pipeline.py
from kairix.core.vector.null import NullVectorRepository
from kairix.core.protocols import VectorRepository
import json
```

#### Fix pattern

Move the symbol you needed out of `tests/` and into `kairix/`. The
common case is a production-quality default implementation that was
living in `tests/fakes.py` — re-home it under `kairix/` (for example
as a `NullX` / `InMemoryX` in the relevant domain package) so it
ships with the wheel. If the import was for a test seam, the
production code shouldn't carry that seam at all — inject the
dependency via a constructor argument and let the test pass the
fake explicitly (the canonical kairix pattern).

After fixing, verify from the installed-wheel posture, not just the
repo:

```
pip install -e .
python -c "import kairix.<your-module>"
```

That mirrors what the dogfood and release smoke tests do, and proves
the import works without `tests/` on `sys.path`.

---

### F26 — `kairix/core/**` may not import providers/ or transport/

#### Statement

No Python file under `kairix/core/` may import from `kairix/providers/`
or `kairix/transport/`. Domain code crosses those boundaries through
Protocols only.

Allowed from core: sibling `kairix.core.*` modules, `kairix.core.protocols`
(the seam), and any non-kairix import. Rejected: any `Import` or
`ImportFrom` whose module path equals or starts with
`kairix.providers.` or `kairix.transport.`.

Pre-existing violations are grandfathered in
`.architecture/baseline/f26-files.txt`. The check is a no-op when
`kairix/core/` does not yet exist (fresh checkout before the
three-layer scaffold lands).

#### Why

The three-layer provider-plugin split
(`docs/architecture/provider-plugin-architecture.md`) puts a hard
boundary between domain logic, universal endpoint concerns, and
per-provider plugins. Without the F26 gate, every new perf concern
accretes another homegrown class inside `kairix/core/`, every new
provider mutates `_azure.py` further, and the probe code grows
per-provider conditionals — exactly the AI-gateway-in-process shape
the ADR exists to undo.

#### Detection

`scripts/checks/check_provider_layer_imports.py`. AST-walks every
`.py` under `kairix/core/`, scans `Import` and `ImportFrom` nodes,
flags any forbidden prefix match. Anchored on the dotted boundary so
hypothetical siblings (`kairix.providers_helpers`) don't false-positive.

#### Examples

Rejected:

```python
# kairix/core/search/pipeline.py
from kairix.providers.azure_foundry import AzureFoundryProvider  # F26
from kairix.transport.pool import make_openai_client            # F26
import kairix.transport.coalesce                                # F26
```

Allowed:

```python
# kairix/core/search/pipeline.py
from kairix.core.protocols import EmbeddingService, VectorSearchBackend
from kairix.core.factory import build_search_pipeline
import logging  # non-kairix is fine
```

#### Fix pattern

Define or reuse a Protocol in `kairix/core/protocols.py` for the
capability the import was reaching for, then accept it as a
constructor / factory parameter. Production wire-up in
`kairix/core/factory.py` (or the provider registry) supplies the
concrete provider; tests inject a `Fake*` from `tests/fakes.py`.

---

### F27 — `kairix/providers/<a>/**` may not import another provider

#### Statement

No Python file under `kairix/providers/<plugin>/` may import from
`kairix/providers/<other>/`. Plugins must remain independently
shippable as separate pip distributions.

Allowed: sibling imports within the same plugin
(`kairix.providers.<plugin>.*`), shared scaffolding
(`kairix.providers._base` and any `_`-prefixed module under
providers/), `kairix.core.*`, `kairix.transport.*`, and non-kairix
imports. Rejected: any import whose first path segment under
`kairix.providers.` names a different plugin.

Pre-existing violations are grandfathered in
`.architecture/baseline/f27-files.txt`. The check is a no-op when
`kairix/providers/` doesn't exist or holds no plugin subdirectories.

#### Why

The plugin model in the ADR
(`docs/architecture/provider-plugin-architecture.md` — "Plugin
discovery") is that a third party can `pip install kairix-provider-foo`
and register a new endpoint family with zero kairix changes. A plugin
that imports another can't be split out without dragging its sibling
along, and the dependency graph becomes a tangle that defeats the
plugin model. Shared concerns belong in `kairix/transport/`.

#### Detection

`scripts/checks/check_no_cross_provider.py`. For each `.py` under
`kairix/providers/`, derives the owning plugin from the path; AST-walks
imports; flags any `kairix.providers.<other>` reference. The shared
`kairix.providers._base` module is explicitly NOT cross-plugin.

#### Examples

Rejected:

```python
# kairix/providers/openai/embed.py
from kairix.providers.azure_foundry import auth_header  # F27
import kairix.providers.bedrock.sigv4                   # F27
```

Allowed:

```python
# kairix/providers/openai/embed.py
from kairix.providers._base import Provider                  # shared base
from kairix.providers.openai.client import build_client      # same plugin
from kairix.transport.pool import get_openai_client          # transport
from kairix.core.protocols import LLMBackend                 # Protocol
```

#### Fix pattern

Extract the shared concern to `kairix/transport/`. If it's genuinely
provider-specific shape, duplicate it inline rather than importing a
sibling plugin.

---

### F28 — Every provider plugin has matching BDD coverage

#### Statement

For every plugin directory under `kairix/providers/<name>/`, both
must hold:

1. `tests/bdd/features/provider_<name>.feature` exists and has at
   least one Scenario (the per-plugin file).
2. Every `tests/bdd/features/e2e_provider_*.feature` either has an
   Examples-table row whose first non-empty cell equals `<name>`,
   OR carries the opt-out tag `@<name>_no_<journey>` (where
   `<journey>` is the part after `e2e_provider_` in the filename).

Plugin discovery: every immediate non-`_`-prefixed subdirectory of
`kairix/providers/` is a plugin. Bare files at the providers root
(`__init__.py`, `_base.py`) are scaffolding, not plugins.

Pre-existing violations are grandfathered in
`.architecture/baseline/f28-files.txt` (one entry per plugin missing
coverage; format `kairix/providers/<name>`). When `kairix/providers/`
holds no plugins, the check is a no-op. When plugins exist but no
`e2e_provider_*.feature` files exist yet (Wave 1 scaffold), only the
per-plugin requirement fires.

#### Why

The E2E features are Scenario Outlines parameterised over the provider
column — adding a provider is one new fixture + one new Examples row,
not a copy-pasted feature. F28 is the mechanical guard that keeps
that property: a plugin without coverage shouldn't ship. The
per-plugin feature covers auth shape, URL shape, error mapping, and
model-id semantics (provider-specific); the E2E journey covers the
generic "user configures provider X → embed/chat works" path.

#### Detection

`scripts/checks/check_provider_bdd_completeness.py`. Discovers plugins
by listing `kairix/providers/<name>/`; for each, checks per-plugin
feature presence and Examples-row inclusion across every
`e2e_provider_*.feature`. The Examples-row matcher tolerates leading
whitespace, ignores the header row, and matches on the first
non-empty cell.

#### Examples

Rejected:

```
kairix/providers/bedrock/        exists, but
tests/bdd/features/provider_bedrock.feature  does not exist  → F28
```

```
tests/bdd/features/e2e_provider_embed.feature  exists with rows
                                               | openai | ... |
                                               | azure_foundry | ... |
kairix/providers/bedrock/  exists  → F28 (no bedrock row, no @bedrock_no_embed tag)
```

Allowed:

```gherkin
# tests/bdd/features/provider_openai.feature
Feature: openai provider plugin
  Scenario: embed_batch reaches the configured base_url
    Given an openai plugin configured with base_url=https://api.openai.com
    When the caller invokes embed_batch with two texts
    Then the recorded request URL is https://api.openai.com/v1/embeddings
```

```gherkin
# tests/bdd/features/e2e_provider_embed.feature
Feature: E2E provider embed journey
  Scenario Outline: embed with provider <provider>
    ...
    Examples:
      | provider      | model              |
      | openai        | text-embedding-3   |
      | azure_foundry | text-embedding-ada |
      | bedrock       | titan-embed-v1     |
```

Allowed (opt-out for an embed-only plugin):

```gherkin
# tests/bdd/features/e2e_provider_chat.feature
@embedonly_no_chat
Feature: E2E provider chat journey
  Scenario Outline: chat with provider <provider>
    ...
```

#### Fix pattern

Create `tests/bdd/features/provider_<name>.feature` with a happy-path
Scenario per the per-plugin contract (auth, URL, error mapping). Add
`| <name> | <model> | ... |` rows to every
`tests/bdd/features/e2e_provider_*.feature`. Use the
`@<name>_no_<journey>` tag only when the plugin genuinely doesn't
implement that journey (e.g. embed-only plugin with no chat).

---

### F29 — Performance-measurement code lives only under `kairix/quality/probe/`

#### Statement

Any `.py` file under `kairix/` whose basename matches a
perf-measurement pattern (`bench*.py`, `microbench*.py`, `*_bench.py`,
`*_microbench.py`, `*_latency*.py`, `*_perf*.py`) must live under
`kairix/quality/probe/`. Tests (`tests/**`) and operational probe
drivers (`scripts/probe*.{py,sh}`) are exempt because they consume
the probe, they don't reimplement it.

Pre-existing violations are grandfathered in
`.architecture/baseline/f29-files.txt`. The check is a no-op when
`kairix/` is absent.

#### Why

The ADR (`docs/architecture/provider-plugin-architecture.md` —
"Performance") centralises every layer's instrumentation in
`kairix/quality/probe/` so the PVT release gate and the end-user
`kairix probe-config` health check share one implementation. Letting
`transport/` or `providers/` grow ad-hoc benchmarks recreates the
per-provider conditional jungle the split exists to remove.

#### Detection

`scripts/checks/check_perf_singleton.py`. Walks `kairix/`; for each
`.py` whose basename matches the perf regex, checks the file's path
against the allow-list (`kairix/quality/probe/**`, `tests/**`,
`scripts/probe*`). Flags any perf-named file outside the allow-list.

#### Examples

Rejected:

```
kairix/transport/pool/bench_pool.py         # F29
kairix/providers/openai/openai_perf.py      # F29
kairix/core/search/bm25_latency.py          # F29
```

Allowed:

```
kairix/quality/probe/embed_latency.py       # canonical home
tests/integration/test_embed_perf_floor.py  # latency assertion in a test
scripts/probe-config-runner.py              # operational driver
kairix/transport/pool/client.py             # not perf-named — fine
```

#### Fix pattern

Relocate the measurement script under `kairix/quality/probe/`, expose
it via the probe CLI, and consume `kairix/transport/telemetry/`'s
timings hook rather than reinventing measurement plumbing. If the
"measurement" is a test assertion, move it under `tests/` (the
allow-list covers that).

---

## SDLC integration map

Each fitness function fires at multiple lifecycle stages. The same
script is invoked everywhere — there's no drift between local and CI
enforcement.

| Stage | When | F1 | F2 | F3 | F4 | F5 | F6 | F7 | F8 | F9 | F10 | F11 | F12 | F13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **IDE** | edit | — | — | — | — | — | — | — | — | — | — | — | — | — |
| **`git commit`** (pre-commit) | every commit | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| **`bash scripts/safe-commit.sh`** | pre-push / pre-PR | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| **CI Stage 0 — Architecture fitness** | every PR push | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| **CI Stage 2 unit-and-type** | every PR push | — | — | — | — | — | — | ✓ | — | — | — | — | — | — |
| **CI Stage 5 union-coverage** | every PR push (after Stage 3) | — | — | — | — | — | — | — | — | ✓ | — | — | — | — |
| **CI gate (fan-in)** | every PR push | requires Stage 0 + Stage 2 + Stage 5 ✓ |  |  |  |  |  |  |  |  |  |  |  |  |
| **Branch protection** | merge attempt | enforced via CI gate |  |  |  |  |  |  |  |  |  |  |  |  |

**Reading this table:** F1–F6, F8, F10–F13 fire at three layers (commit,
manual gate, CI Stage 0). F7 fires in Stage 2 because it needs unit
coverage. F9 fires in Stage 5 because it needs both unit and integration
coverage to be combined. The CI gate fans-in on every required job — a
failing fitness function blocks merge regardless of whether other jobs
pass.

---

## Harness architecture

### File layout

```
scripts/checks/
├── _arch_lib.py                          # Python helper: gate(), python_files(), repo_relative()
├── _lib.sh                               # Shell helper: arch_gate() function
├── check-no-internal-patches.sh                       # F1
├── check-no-env-monkeypatch.sh                        # F2
├── check-suppressions-have-rationale.sh               # F3 (extended: covers # type: ignore + # nosec)
├── check-env-reads-stay-in-paths.sh                   # F4
├── check_no_internal_imports.py                       # F5 (AST)
├── check_no_test_only_kwargs.py                       # F6 (AST)
├── check_per_file_coverage.py                         # F7 (Cobertura XML) + F9 (with arg)
├── check_test_markers.py                              # F8 (AST)
├── check-workflow-silencers-have-rationale.sh         # F10
├── check_test_skip_rationale.py                       # F11 (AST)
├── check_bdd_happy_path.py                            # F12 (Gherkin parser)
├── check_bdd_no_implementation_leaks.py               # F13 (regex)
└── run-all.sh                                         # Orchestrator (safe-commit + CI Stage 0)

.architecture/baseline/
├── no-internal-patches-files.txt
├── no-env-monkeypatch-files.txt
├── suppressions-have-rationale-files.txt              # F3 (now includes # type: ignore + # nosec sites)
├── env-reads-in-paths-files.txt                       # F4
├── no-internal-test-imports-files.txt
├── no-test-only-kwargs-files.txt
├── per-file-coverage-floor-files.txt                  # F7 (unit only)
├── per-file-coverage-floor-union-files.txt            # F9 (unit ∪ integration)
├── bdd-no-implementation-leaks-files.txt              # F13
└── test-only-kwargs-allow.txt                         # F6 allow-list (separate from baseline)
# F8, F10, F11, F12 ship with no baseline — clean

docs/architecture/
└── fitness-functions.md                  # this document
```

### Helper libraries

**`_lib.sh`** provides `arch_gate()` for shell-based checks. The check
script pipes a list of violation files (one per line, sorted, uniq'd)
into `arch_gate <name> <remediation>`. The helper handles baseline
comparison, exit code, and message formatting.

**`_arch_lib.py`** provides:
- `gate(name, current_set, remediation_str) -> int` — same semantics
  as the shell helper, for Python checks.
- `python_files(*roots)` — yields all `.py` files under given roots,
  skipping `__pycache__`.
- `repo_relative(path)` — converts an absolute path to repo-relative.

### Tooling choice rationale

For each rule, I chose the simplest tool that gives correct detection:

- **Shell + grep** for line-pattern rules (F1, F2, F3) where the
  trigger is an unambiguous string at the line level. AST adds no
  precision; the grep regex is short, readable, and fast.
- **Python AST** for structural rules (F5, F6, F8) where the trigger
  depends on import structure (rejected `from kairix.x import _y`
  vs. allowed `from kairix.x import y as _alias`), function
  signatures (`*_fn=None` requires inspecting `args.args` /
  `args.kwonlyargs` defaults), or decorator/marker inheritance
  (F8 needs to walk class decorators + `pytestmark` assignments).
- **Cobertura XML** for F7 because the data is already in that
  format from `pytest --cov-report=xml`. Standard library
  `xml.etree.ElementTree` is sufficient. The same `coverage.xml` is
  uploaded to Codecov from the same CI step, so the mechanical floor
  (F7) and the dashboard signal (Codecov) read from one source.

I considered and rejected:

- **`ruff` custom rules** — `ruff` doesn't support arbitrary plugins
  (Rust binary with a fixed rule set). Adding rules requires upstream
  contribution or a fork.
- **`flake8` plugin** — would work but introduces a separate linting
  framework alongside the existing ruff usage.
- **`semgrep`** — overkill for these rule shapes; useful when
  data-flow analysis is needed (it isn't here).

### Sabotage discipline

Every check should be **sabotage-tested** before landing. The pattern:

1. Plant a fake violation in a new file (or a new violation in an
   existing baselined file).
2. Run the check and verify it fails with the expected message.
3. Remove the fake violation.
4. Run again and verify clean.

Example for F2:

```bash
cat > /tmp/sabotage.py <<'EOF'
def test_x(monkeypatch):
    monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "/tmp/x")
EOF
cp /tmp/sabotage.py tests/_sabotage.py
bash scripts/checks/check-no-env-monkeypatch.sh  # expect FAIL
rm tests/_sabotage.py
bash scripts/checks/check-no-env-monkeypatch.sh  # expect ok
```

If a check passes the sabotage test on the first commit but starts
quietly missing violations later, the script is the source of truth
for the rule and needs to be debugged.

### Sabotage-test evidence — harness landing

Every fitness function below was sabotage-tested before its harness
commit. The evidence is reproducible (each row gives the plant + the
expected check output):

| Rule | Plant | Detected | Notes |
|---|---|---|---|
| F1 | `tests/_sabotage.py` with `with patch("kairix.core.search.bm25.bm25_search"):` | ✓ | Initial check missed single-quoted form (`patch('kairix.…')`); regex widened to `["']` so both forms match |
| F1 | `tests/_sabotage.py` with `with patch('kairix.core.search.bm25.bm25_search'):` | ✓ | Single-quote form caught by widened regex |
| F2 | `tests/_sabotage.py` with `monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "/x")` | ✓ |  |
| F3 | `tests/_sabotage.py` with `x = 1  # NOSONAR` (no rationale) | ✓ |  |
| F4 | `kairix/_sabotage.py` with `os.environ.get("KAIRIX_DOCUMENT_ROOT")` | ✓ | Confirmed `paths.py` and `secrets.py` (allow-list) still pass |
| F5 | `tests/_sabotage.py` with `from kairix.quality.eval.gold_builder import _validate_weights` | ✓ |  |
| F6 | `kairix/_sabotage.py` with `def render(data, *, format_fn=None):` | ✓ |  |
| F7 | `coverage.xml` injected with `<class filename="_sabotage_f7.py" line-rate="0.50">` | ✓ |  |
| F8 | `tests/_sabotage_f8.py` with `def test_unmarked_function_should_fail_f8(): ...` (no marker) | ✓ |  |
| F8 | `tests/_sabotage_f8_unknown.py` with `@pytest.mark.someothermarker` (non-category marker) | ✓ | Confirms only the registered category set counts |
| F8 | `tests/_sabotage_f8_fixture.py` with `@pytest.fixture` named `test_*` | passed (no false positive) | Fixtures named `test_*` correctly excluded |
| F8 | `tests/_sabotage_f8_modulemark.py` with module-level `pytestmark = pytest.mark.unit` and unmarked function | passed (no false positive) | Module-level mark inheritance works |
| F8 | `tests/_sabotage_f8_listmark.py` with class-level `pytestmark = [pytest.mark.contract]` | passed (no false positive) | List-form pytestmark accepted |
| F3 ext | `tests/_sabotage_f3_typeignore.py` with `x = 1  # type: ignore` | ✓ | Bare `# type: ignore` caught |
| F3 ext | `tests/_sabotage_f3_nosec.py` with bare `# nosec` on a bandit-flagged line | ✓ | Bare `# nosec` caught |
| F3 ext | `tests/_sabotage_f3_typeignore_ok.py` with `x = 1  # type: ignore[attr-defined]  # third-party stub gap` | passed (no false positive) | Rationale form accepted |
| F10 | `.github/workflows/_sabotage_f10.yml` with bare `continue-on-error: true` | ✓ |  |
| F10 | `.github/workflows/_sabotage_f10_ok.yml` with `continue-on-error: true  # rationale` | passed (no false positive) | Same-line comment accepted |
| F11 | `tests/_sabotage_f11_skip.py` with `@pytest.mark.skip` (bare) | ✓ |  |
| F11 | `tests/_sabotage_f11_importorskip.py` with `pytest.importorskip("nonexistent_module")` (no rationale) | ✓ |  |
| F11 | `tests/_sabotage_f11_xfail_bare.py` with `@pytest.mark.xfail` (bare) | ✓ |  |
| F11 | `tests/_sabotage_f11_ok.py` with preceding comment + `@pytest.mark.skip(reason="…")` | passed (no false positive) | Comment-block-above pattern accepted |
| F12 | `tests/bdd/features/_sabotage_f12_errors_only.feature` with two `@error`/`@negative` scenarios only | ✓ | Feature with no happy-path rejected |
| F12 | `tests/bdd/features/_sabotage_f12_empty.feature` with zero scenarios | ✓ | Empty feature rejected |
| F12 | `tests/bdd/features/_sabotage_f12_ok.feature` with one untagged + one `@error` scenario | passed (no false positive) | Mixed feature accepted |
| F13 | `tests/bdd/features/_sabotage_f13.feature` with `Mock` + `kairix.core.search.bm25` references | ✓ | Implementation symbols caught |
| F13 | `tests/bdd/features/_sabotage_f13_ok.feature` with `kairix.config.yaml` (filename) reference | passed (no false positive) | File-extension allowlist works |

After each plant, the file was removed and the check re-run to confirm
the baseline state was preserved. The runner script lives at
`/tmp/sabotage_runner.sh` during development; it is not committed
because it intentionally writes to the repo. New fitness functions
must include a sabotage-test entry in this table at the time they
land.

---

## GitHub Actions integration

### Workflow shape

`.github/workflows/ci.yml` declares the `arch-fitness` job as **Stage 0**:

```yaml
arch-fitness:
  name: "Stage 0 -- Architecture fitness"
  runs-on: ubuntu-latest
  needs: changes
  if: needs.changes.outputs.python == 'true'
  steps:
    - uses: actions/checkout@...
    - uses: actions/setup-python@...
    - name: Run F1-F6 + F8 (no test runtime needed)
      run: bash scripts/checks/run-all.sh --skip-coverage
```

It depends only on the `changes` job (path filter) — runs in parallel
with `pre-commit`, `contracts`, `unit-and-type`, etc. Fast (< 30s
typical) because no test runtime is needed.

F7 runs inside `unit-and-type`:

```yaml
- name: F7 — per-file coverage floor (85%)
  if: matrix.python-version == '3.12'
  run: python3 scripts/checks/check_per_file_coverage.py coverage.xml
```

It runs **after** pytest emits `coverage.xml`, gated to one Python
version (3.12) to avoid duplicate enforcement across the matrix.

The same `coverage.xml` is then uploaded to Codecov in the next step
(`codecov/codecov-action@v5` with `flags: unit`). Codecov's patch
target (`codecov.yml: coverage.status.patch.default.target = 85%`)
mirrors F7's floor, so the dashboard signal and the mechanical gate
stay aligned. The integration job runs the equivalent flow with
`flags: integration` from `coverage-integration.xml`.

Test analytics — flaky-test detection, slow-test trends — runs in
parallel via `codecov/test-results-action@v1`, consuming the JUnit
XMLs already produced by every test stage (contracts / unit /
integration). It does not block the merge; it's diagnostic signal.

### CI gate

The `check` job (the "CI gate" branch-protection target) fans in on
**all** required jobs including `arch-fitness`:

```yaml
check:
  name: "CI gate"
  needs:
    - changes
    - arch-fitness     # <-- listed here
    - pre-commit
    - contracts
    - unit-and-type
    - coverage
    - integration
    - security
    - docker
```

A failing `arch-fitness` job sets `needs.arch-fitness.result` to
`failure`. The gate's `for result in $RESULT_*; do ...` loop fails the
gate. Branch protection rejects the merge.

### Branch protection

The repo's branch protection on `main` and `develop` requires the
`CI gate` job to pass. No additional configuration is needed for
fitness functions — they're transitively enforced via the gate.

### Failure UX

When a fitness function fails in CI, the GitHub Actions log shows:

```
=== Architecture fitness functions ===
ok [arch:no-internal-patches] — 3 grandfathered file(s) still present in baseline.
FAIL [arch:no-env-monkeypatch] — new violation(s) introduced:
  tests/agents/research/test_new.py

Refactor: pass paths as a constructor argument or use FakePaths
from tests/fakes.py. The production code must not require process-env
mutation to be testable — that's the test-shaped-API smell #139 reverted.

If this is genuinely the only practical fix, document why in the
PR description and append the file to .architecture/baseline/no-env-monkeypatch-files.txt
(but expect pushback at review time — adding to the baseline is rare).

=== Architecture fitness functions FAILED ===
```

The message names the file, the rule, the remediation, and the
escape hatch. PR comments from CI are not currently auto-generated;
operators read the job log directly via the failure URL.

---

## Operating the harness

### Running locally

```bash
# Run everything (skips F7 unless coverage.xml is present)
bash scripts/checks/run-all.sh

# Skip F7 explicitly (faster; useful when coverage.xml is stale)
bash scripts/checks/run-all.sh --skip-coverage

# Run one check only
bash scripts/checks/check-no-env-monkeypatch.sh
python3 scripts/checks/check_no_internal_imports.py
python3 scripts/checks/check_per_file_coverage.py coverage.xml
```

### Generating coverage.xml for F7

```bash
pytest tests/ -m "unit or bdd or contract" --cov=kairix --cov-report=xml:coverage.xml
python3 scripts/checks/check_per_file_coverage.py coverage.xml
```

### Pre-commit

The hooks run automatically on `git commit`. To install:

```bash
pre-commit install   # one-time setup
pre-commit run --all-files   # run all hooks against every file (manual)
pre-commit run arch-no-env-monkeypatch --all-files  # one hook only
```

### safe-commit

The `safe-commit.sh` wrapper runs all gates including fitness functions:

```bash
bash scripts/safe-commit.sh "your commit message"
# Order: ruff lint → ruff format → mypy → tests → arch fitness
#        → secrets → confidential check → commit
```

### Debugging a failed check

1. **Read the failure message.** It names the file and the rule.
2. **Read the rule's section in this document.** The "Fix pattern"
   subsection has the remediation.
3. **Check the baseline file.** If your file is listed, you've made a
   net-new violation in a previously-grandfathered file (still
   blocked). If your file isn't listed, you've introduced the rule's
   violation in a clean file.
4. **Run the check in isolation.** `python3 scripts/checks/check_no_internal_imports.py`
   prints all current violations not just net-new — useful for seeing
   the full surface.
5. **Fix the code and re-run.** Don't add to the baseline unless you
   have rationale and reviewer approval.

### Shrinking a baseline

```bash
# 1. Make the code change. Run the check; it should pass.
bash scripts/checks/check-no-env-monkeypatch.sh

# 2. Remove the file's line from the baseline.
sed -i '' '/^tests\/the_fixed_file\.py$/d' .architecture/baseline/no-env-monkeypatch-files.txt

# 3. Re-run to confirm the file is now fully enforced.
bash scripts/checks/check-no-env-monkeypatch.sh

# 4. Commit code + baseline together.
git add tests/the_fixed_file.py .architecture/baseline/no-env-monkeypatch-files.txt
git commit -m "..."
```

### Adding a temporary exception (rare)

If you've genuinely exhausted alternatives:

1. Document in the PR description WHY the violation is correct for
   this case (constraint that prevents the fix, alternative being
   tracked, etc.).
2. Append the file to the appropriate baseline.
3. File a tracking issue or task to revisit.
4. Expect reviewer pushback — this is the rare path, not the easy one.

---

## Adding a new fitness function

The playbook for adding F8, F9, etc.:

1. **Decide the rule shape.** Write a one-sentence statement
   ("MUST NOT…") and a one-paragraph "why."
2. **Choose the detection mechanism.** Line-pattern → shell + grep.
   Structural → Python AST. Coverage / report → Python + parser.
3. **Implement the script** under `scripts/checks/`. Follow the
   existing scripts as templates. Use `arch_gate` (shell) or
   `_arch_lib.gate` (Python).
4. **Seed the baseline.** Run the script; pipe its violation list
   to `.architecture/baseline/<rule-name>-files.txt`. This makes the
   current state pass.
5. **Sabotage-test.** Plant a fake violation in a new file; confirm
   the script fails with the expected message; remove and re-run for
   clean exit.
6. **Wire into pre-commit.** Add an entry to
   `.pre-commit-config.yaml` under the `Architecture fitness
   functions` section.
7. **Wire into safe-commit.** No explicit step — `run-all.sh` picks
   it up if the script is in `scripts/checks/`. Verify by running
   `bash scripts/safe-commit.sh "test"` (use a no-op staged change).
8. **Wire into CI.** Either piggy-back on `arch-fitness` (preferred —
   `run-all.sh` invokes it) or add a separate step if the check has
   special dependencies (like F7 needing `coverage.xml`).
9. **Document in this file.** Add a section to "The rules in detail"
   following the existing template (statement, why, detection,
   examples, fix pattern). Update the "Rules at a glance" table and
   the "SDLC integration map."
10. **Sanity-check the gate.** `bash scripts/checks/run-all.sh`
    should still pass against current state. If it fails, the
    baseline is wrong or the check has a bug.

---

## Limits — what fitness functions don't catch

These are deliberate omissions, not gaps. Each requires a different
enforcement mechanism (review, runtime check, or human judgement):

- **Internal-method tests via direct attribute access.**
  `obj._private_method()` is structurally a normal method call;
  detecting "this method has a `_` prefix" requires data-flow
  analysis that isn't worth the complexity for the rare case. F5
  catches the import; the call is reviewer-time.

- **Soft assertions.** `if results: assert ...` and `assert x or y`
  patterns silently pass when the precondition is false. CLAUDE.md
  documents this as a review-time concern; no automated detector is
  reliable enough yet.

- **Diagnostic-as-fix.** Shipping `logger.warning(...)` and calling a
  bug "fixed" is judgement, not pattern. The CI gate doesn't know
  whether a fix actually changes behaviour.

- **Inappropriate intimacy** between modules. Detecting "module A
  reaches into module B's private state" via static analysis is
  possible (attribute-access tracking) but expensive. CLAUDE.md
  flags it as a smell.

- **Documentation drift.** This file claims to be canonical; only
  reviewer attention keeps it in sync with the scripts.

---

## Cross-references

- **CLAUDE.md** — engineering standards, including non-fitness-function
  guidance (commit hygiene, naming, agent collaboration).
- **`docs/architecture/ENGINEERING.md`** — broader architecture rules
  (Protocol-driven boundaries, factory composition, repository pattern).
- **`docs/architecture/cli-mcp-feature-parity.md`** — issue #168, the
  CLI/MCP convergence initiative; its Phase 2 work will reduce CLI
  body coverage gaps that F7 currently flags.
- **`scripts/checks/`** — implementation source-of-truth.
- **`.architecture/baseline/`** — current grandfathered violations.

---

## For agents: machine-readable rule index

When picking work, consult this section. Each entry: rule ID,
script path, baseline path, pre-commit hook ID.

```yaml
fitness_functions:
  - id: F1
    name: no-internal-patches
    script: scripts/checks/check-no-internal-patches.sh
    baseline: .architecture/baseline/no-internal-patches-files.txt
    precommit_hook: arch-no-internal-patches
    layer: [pre-commit, safe-commit, ci-stage0]

  - id: F2
    name: no-env-monkeypatch
    script: scripts/checks/check-no-env-monkeypatch.sh
    baseline: .architecture/baseline/no-env-monkeypatch-files.txt
    precommit_hook: arch-no-env-monkeypatch
    layer: [pre-commit, safe-commit, ci-stage0]

  - id: F3
    name: suppressions-have-rationale
    script: scripts/checks/check-suppressions-have-rationale.sh
    baseline: .architecture/baseline/suppressions-have-rationale-files.txt
    precommit_hook: arch-suppressions-have-rationale
    layer: [pre-commit, safe-commit, ci-stage0]

  - id: F4
    name: env-reads-in-paths
    script: scripts/checks/check-env-reads-stay-in-paths.sh
    baseline: .architecture/baseline/env-reads-in-paths-files.txt
    precommit_hook: arch-env-reads-in-paths
    layer: [pre-commit, safe-commit, ci-stage0]

  - id: F5
    name: no-internal-test-imports
    script: scripts/checks/check_no_internal_imports.py
    baseline: .architecture/baseline/no-internal-test-imports-files.txt
    precommit_hook: arch-no-internal-test-imports
    layer: [pre-commit, safe-commit, ci-stage0]

  - id: F6
    name: no-test-only-kwargs
    script: scripts/checks/check_no_test_only_kwargs.py
    baseline: .architecture/baseline/no-test-only-kwargs-files.txt
    allow_list: .architecture/baseline/test-only-kwargs-allow.txt
    precommit_hook: arch-no-test-only-kwargs
    layer: [pre-commit, safe-commit, ci-stage0]

  - id: F7
    name: per-file-coverage-floor
    script: scripts/checks/check_per_file_coverage.py
    baseline: .architecture/baseline/per-file-coverage-floor-files.txt
    precommit_hook: null  # CI-only (needs coverage.xml)
    layer: [ci-unit-and-type]

  - id: F8
    name: test-markers
    script: scripts/checks/check_test_markers.py
    baseline: null  # ships clean — no grandfathered files
    precommit_hook: arch-test-markers
    layer: [pre-commit, safe-commit, ci-stage0]

  - id: F9
    name: per-file-coverage-floor-union
    script: scripts/checks/check_per_file_coverage.py
    invoke: python3 scripts/checks/check_per_file_coverage.py coverage-union.xml per-file-coverage-floor-union
    baseline: .architecture/baseline/per-file-coverage-floor-union-files.txt
    precommit_hook: null  # CI-only (needs unit + integration coverage combined)
    layer: [ci-stage5]

  - id: F10
    name: workflow-silencers-have-rationale
    script: scripts/checks/check-workflow-silencers-have-rationale.sh
    baseline: null  # ships clean — no grandfathered files
    precommit_hook: arch-workflow-silencers-have-rationale
    layer: [pre-commit, safe-commit, ci-stage0]

  - id: F11
    name: test-skip-rationale
    script: scripts/checks/check_test_skip_rationale.py
    baseline: null  # ships clean — no grandfathered files
    precommit_hook: arch-test-skip-rationale
    layer: [pre-commit, safe-commit, ci-stage0]

  - id: F12
    name: bdd-happy-path
    script: scripts/checks/check_bdd_happy_path.py
    baseline: null  # ships clean — no grandfathered files
    precommit_hook: arch-bdd-happy-path
    layer: [pre-commit, safe-commit, ci-stage0]

  - id: F13
    name: bdd-no-implementation-leaks
    script: scripts/checks/check_bdd_no_implementation_leaks.py
    baseline: .architecture/baseline/bdd-no-implementation-leaks-files.txt
    precommit_hook: arch-bdd-no-implementation-leaks
    layer: [pre-commit, safe-commit, ci-stage0]

  - id: F24
    name: no-test-imports-in-prod
    script: scripts/checks/check_no_test_imports_in_prod.py
    baseline: .architecture/baseline/no-test-imports-in-prod-files.txt
    precommit_hook: arch-no-test-imports-in-prod
    layer: [pre-commit, safe-commit, ci-stage0]
```
