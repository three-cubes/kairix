# Architecture fitness functions

Mechanical, ratcheting checks that block PRs introducing patterns we've
explicitly rejected. Each rule has a script under `scripts/checks/`, a
baseline file under `.architecture/baseline/`, and an entry in
`.pre-commit-config.yaml` plus the `arch-fitness` CI job.

**Ratchet semantics:** the baseline file lists files containing
pre-existing violations. New files (not in the baseline) trigger a
non-zero exit. Pre-existing files are grandfathered. The baseline is
expected to **shrink** over time — never grow. Adding a file to the
baseline requires PR-description rationale and reviewer approval.

## How to run

```bash
# Pre-commit hooks (every git commit)
git commit  # arch checks fire automatically via .pre-commit-config.yaml

# Manual run
bash scripts/checks/run-all.sh

# Without F7 (skips per-file coverage; useful when coverage.xml is stale)
bash scripts/checks/run-all.sh --skip-coverage

# Just one check
python3 scripts/checks/check_no_internal_imports.py
bash scripts/checks/check-no-env-monkeypatch.sh
```

## The rules

### F1 — No `@patch` on kairix internal code

**Rule:** Test files MUST NOT call `@patch("kairix.…")` or
`with patch("kairix.…")`.

**Why:** patches couple tests to module structure (a rename breaks the
test silently). Use constructor injection or a `Protocol` seam from
`kairix.core.protocols`. `tests/fakes.py` exists for exactly this.

**Allowed exceptions:** stdlib (`patch("os.*", "builtins.*", "pathlib.*")`)
and external SDK boundaries (`patch("openai.*", "httpx.*")`).

**Detection:** `scripts/checks/check-no-internal-patches.sh` (grep —
line-level pattern, unambiguous).

**Baseline:** `.architecture/baseline/no-internal-patches-files.txt`.

**Fix pattern:** the production class takes the dependency in `__init__`;
the test passes a fake at construction. Same shape as
`GoldBuilder(llm_judge=..., retriever=..., db_path=...)`.

### F2 — No `monkeypatch.setenv("KAIRIX_*")` in tests

**Rule:** Test files MUST NOT mutate kairix env vars via
`monkeypatch.setenv|setattr|delenv` on `KAIRIX_*` keys.

**Why:** per the boundary-only `KairixPaths` pattern (#139), env vars
are read once at the boundary into `KairixPaths`. Tests construct
`KairixPaths` directly via `tests.fakes.FakePaths`. Mutating process
env is a test-shaped API smell — production code must not require it
to be testable.

**Detection:** `scripts/checks/check-no-env-monkeypatch.sh` (grep).

**Baseline:** `.architecture/baseline/no-env-monkeypatch-files.txt`.

**Fix pattern:** `paths = FakePaths(document_root=tmp_path / "vault")`,
then pass `paths=` to the production constructor.

### F3 — Suppressions require rationale

**Rule:** A bare `# NOSONAR`, `# noqa`, or `# pragma: no cover` is
rejected. The accompanying same-line rationale documents WHY the rule
doesn't apply.

**Why:** suppressions without rationale rot — future readers can't
tell whether the suppression is still load-bearing. The rationale is
the receipt that the suppression is deliberate.

**Accepted:** `x = 1  # NOSONAR — internal log path; not user-controlled`
**Rejected:** `x = 1  # NOSONAR`

**Detection:** `scripts/checks/check-suppressions-have-rationale.sh`
(grep — looks for the bare form at end-of-line).

**Baseline:** `.architecture/baseline/suppressions-have-rationale-files.txt`.

### F5 — No internal-name imports in tests

**Rule:** Test files MUST NOT import private names (`_x`) from
`kairix.*` modules. Importing from a private module path
(`kairix.foo._impl`) is also rejected.

**Why:** drives every branch through the public surface. A test that
imports `_helper` directly couples to that internal name and breaks on
rename. Usually the answer is to test the public function that calls
the helper.

**Allowed pattern:** `from kairix.foo import bar as _alias` — the `as`
clause is a test-local rename of a *public* name. The public name `bar`
is what's being depended on; `_alias` is just the local binding.

**Detection:** `scripts/checks/check_no_internal_imports.py` (AST —
distinguishes private imports from local renames precisely).

**Baseline:** `.architecture/baseline/no-internal-test-imports-files.txt`.

### F6 — No `*_fn=None` test-only kwargs in production

**Rule:** Production functions MUST NOT take parameters whose name
ends in `_fn` and whose default is `None`, unless the parameter is on
the documented allow-list.

**Why:** these are the smell that triggered the #113/#114 reverts.
Production grew complexity for tests without operator value. The
legitimate seam pattern is **constructor injection at a boundary class**
(e.g. `GoldBuilder(llm_judge=, retriever=)`) — not per-helper
substitution kwargs on free functions.

**Allow-list:** `.architecture/baseline/test-only-kwargs-allow.txt`,
format `module.path::function_name::param_name`. An entry must be
accompanied by either:
- A real production caller that passes a non-default value, OR
- A documented Protocol/Adapter wiring point at a true boundary.

**Detection:** `scripts/checks/check_no_test_only_kwargs.py` (AST).

**Baseline:** `.architecture/baseline/no-test-only-kwargs-files.txt`.

**Fix pattern:** if the function has multiple stateful collaborators,
extract a class. If it's a Protocol Adapter, declare the dependency at
the Protocol level. If the parameter exists ONLY for tests, delete it
and refactor the test.

### F7 — Per-file coverage floor at 85%

**Rule:** Every file in `coverage.xml` (kairix/* sources, post-omit)
MUST be ≥ 85% covered.

**Why:** repository-wide coverage averages can hide files at 0%.
Per-file is the correct unit of measurement.

**Detection:** `scripts/checks/check_per_file_coverage.py` (Cobertura
XML parsing). Runs in the `unit-and-type` CI job after pytest emits
`coverage.xml`.

**Baseline:** `.architecture/baseline/per-file-coverage-floor-files.txt`.

**Fix pattern:** add tests driving the public surface that exercises
the uncovered lines. Do **not** add `# pragma: no cover` to silence
the gate — that's exactly the suppression F3 rejects unless
rationale-documented.

## Ratchet hygiene

When you fix a violation in a baselined file:
1. Make the code change.
2. Re-run the relevant check locally to confirm it's clean.
3. Remove the file's line from the baseline.
4. Commit both changes together.

When the baseline reaches zero, delete the baseline file — the rule is
fully enforced going forward.

## Adding a new check

1. Write the script under `scripts/checks/`.
2. Run it once and capture current violations into
   `.architecture/baseline/<rule-name>-files.txt`.
3. Add an entry in `.pre-commit-config.yaml` (and the `arch-fitness`
   CI job if it doesn't piggy-back on the orchestrator).
4. Document the rule in this file.
5. Test the failure path: introduce a fake violation, confirm the
   check fails the build.

## What this doesn't catch

- **Internal-method tests via direct attribute access** —
  `obj._private_method()` is structurally similar to a public call
  and would need data-flow analysis. Caught by F5 only when the
  internal helper is imported by name.
- **Soft assertions / sabotage-defeated tests** — `if results:
  assert ...` style. Reviewer-time check, codified separately as
  guidance in `CLAUDE.md`.
- **Test-only constructor seams** — `GoldBuilder(db_path=...)` is the
  pattern; if someone later adds `GoldBuilder(_search_fn=...)` we'd
  want to flag it, but the AST walk for F6 only inspects free
  functions today. Could extend to methods.
