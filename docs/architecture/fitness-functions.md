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
| F7 | Per-file coverage floor at 85% | coverage report | Python + Cobertura XML | CI unit-and-type | `per-file-coverage-floor-files.txt` |
| F8 | Every `test_*` function carries a category marker | structural | Python AST | pre-commit, safe-commit, CI Stage 0 | (none — clean baseline) |

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

## SDLC integration map

Each fitness function fires at multiple lifecycle stages. The same
script is invoked everywhere — there's no drift between local and CI
enforcement.

| Stage | When | F1 | F2 | F3 | F4 | F5 | F6 | F7 | F8 |
|---|---|---|---|---|---|---|---|---|---|
| **IDE** | edit | — | — | — | — | — | — | — | — |
| **`git commit`** | every commit (via `.pre-commit-config.yaml`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| **`bash scripts/safe-commit.sh`** | pre-push / pre-PR | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| **CI Stage 0 — Architecture fitness** | every PR push | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| **CI unit-and-type** | every PR push | — | — | — | — | — | — | ✓ | — |
| **CI gate (fan-in)** | every PR push | requires Stage 0 ✓ |  |  |  |  |  |  |  |
| **Branch protection** | merge attempt | enforced via CI gate |  |  |  |  |  |  |  |

**Reading this table:** F1–F6 and F8 fire at three layers (commit,
manual gate, CI). F7 fires only in CI because it needs the test
runtime. The CI gate fans-in on the Stage 0 result — a failing fitness
function blocks merge regardless of whether other jobs pass.

---

## Harness architecture

### File layout

```
scripts/checks/
├── _arch_lib.py                          # Python helper: gate(), python_files(), repo_relative()
├── _lib.sh                               # Shell helper: arch_gate() function
├── check-no-internal-patches.sh          # F1
├── check-no-env-monkeypatch.sh           # F2
├── check-suppressions-have-rationale.sh  # F3
├── check-env-reads-stay-in-paths.sh      # F4
├── check_no_internal_imports.py          # F5 (AST)
├── check_no_test_only_kwargs.py          # F6 (AST)
├── check_per_file_coverage.py            # F7 (XML)
├── check_test_markers.py                 # F8 (AST)
└── run-all.sh                            # Orchestrator (used by safe-commit + CI Stage 0)

.architecture/baseline/
├── no-internal-patches-files.txt
├── no-env-monkeypatch-files.txt
├── suppressions-have-rationale-files.txt
├── env-reads-in-paths-files.txt          # F4
├── no-internal-test-imports-files.txt
├── no-test-only-kwargs-files.txt
├── per-file-coverage-floor-files.txt
└── test-only-kwargs-allow.txt            # F6 allow-list (separate from baseline)
# F8 ships with no baseline — clean

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
```
