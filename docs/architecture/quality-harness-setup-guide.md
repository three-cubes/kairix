# Quality harness setup guide

How to stand up the kairix quality harness on a new repo (e.g. `tc-agent-zone`). This guide focuses on the **specific configuration nuances** that take time to discover. The canonical *what* and *why* lives in [`fitness-functions.md`](./fitness-functions.md); this is the *how-to-set-up*.

Everything in this document is the lived experience of getting the harness green on kairix — every gotcha section corresponds to a real failure that bit us during the rollout.

## Contents

1. [What you get](#what-you-get)
2. [Step-by-step setup](#step-by-step-setup)
3. [Configuration gotchas (read every one)](#configuration-gotchas-read-every-one)
4. [Reference configs](#reference-configs)
5. [Branch protection](#branch-protection)
6. [What to leave for later](#what-to-leave-for-later)

---

## What you get

The harness is **F1–F13** plus four supporting cross-cuts:

- **Mechanical fitness functions** — file-level ratcheting baselines for forbidden patterns: monkeypatching internals (F1), env-var monkeypatching (F2), un-rationaled suppressions (F3 — covers `# noqa`/`# NOSONAR`/`# pragma: no cover`/`# type: ignore`/`# nosec`), env-var smuggling (F4), private-name imports in tests (F5), `*_fn=None` test-only kwargs (F6), unmarked tests (F8), un-rationaled CI silencers (F10), un-rationaled test skips (F11), BDD features without happy paths (F12), BDD scenarios that leak implementation symbols (F13).
- **Holistic coverage gates** — F7 enforces 85% per-file on unit coverage; F9 enforces the same on the unit∪integration union.
- **Codecov** — coverage flags (unit + integration with carryforward), test analytics (flaky/slow tracking), components for per-area dashboards.
- **SonarCloud** — separate quality gate; reads the same `coverage.xml`.

This document tells you how to put all the pieces in place and connect them.

---

## Step-by-step setup

### 1. Clone the harness scripts

Copy the entire contents of `scripts/checks/` from kairix into the new repo:

```
scripts/checks/
├── _arch_lib.py                                       # Python helper
├── _lib.sh                                            # Shell helper
├── check-no-internal-patches.sh                       # F1
├── check-no-env-monkeypatch.sh                        # F2
├── check-suppressions-have-rationale.sh               # F3 (extended)
├── check-env-reads-stay-in-paths.sh                   # F4
├── check_no_internal_imports.py                       # F5
├── check_no_test_only_kwargs.py                       # F6
├── check_per_file_coverage.py                         # F7 + F9 (with arg)
├── check_test_markers.py                              # F8
├── check-workflow-silencers-have-rationale.sh         # F10
├── check_test_skip_rationale.py                       # F11
├── check_bdd_happy_path.py                            # F12
├── check_bdd_no_implementation_leaks.py               # F13
└── run-all.sh                                         # Orchestrator
```

Adapt the package-name guards: F1, F2, F4, F5 reference `kairix.` and `KAIRIX_*` literals. Search-and-replace to your project's namespace (`tc_agent_zone.` and `TC_AGENT_ZONE_*` or whatever you settle on).

### 2. Create empty baselines

```bash
mkdir -p .architecture/baseline
touch .architecture/baseline/no-internal-patches-files.txt
touch .architecture/baseline/no-env-monkeypatch-files.txt
touch .architecture/baseline/suppressions-have-rationale-files.txt
touch .architecture/baseline/env-reads-in-paths-files.txt
touch .architecture/baseline/no-internal-test-imports-files.txt
touch .architecture/baseline/no-test-only-kwargs-files.txt
touch .architecture/baseline/per-file-coverage-floor-files.txt
touch .architecture/baseline/per-file-coverage-floor-union-files.txt
touch .architecture/baseline/bdd-no-implementation-leaks-files.txt
touch .architecture/baseline/test-only-kwargs-allow.txt   # F6 allow-list
```

Then run each check, capture violations, and seed the corresponding baseline. **Sabotage-prove every check before shipping** — see the discipline section in `fitness-functions.md`.

### 3. Wire pre-commit

Copy `.pre-commit-config.yaml`'s "Architecture fitness functions" block from kairix. The hooks are `language: system`, so they invoke the local scripts directly — no plugin to install.

### 4. Wire `safe-commit.sh`

Copy `scripts/safe-commit.sh`. It runs ruff/format/mypy/pytest then calls `bash scripts/checks/run-all.sh --skip-coverage`. The `--skip-coverage` flag skips F7/F9 since they need test runtime — those run only in CI.

### 5. Wire CI

Copy `.github/workflows/ci.yml`'s job structure. The pipeline runs five stages:

| Stage | Job | Purpose |
|---|---|---|
| **0** | `arch-fitness` | F1–F6, F8, F10–F13 (no test runtime needed) |
| **1** | `contracts` | `pytest -m contract` |
| **2** | `unit-and-type` (matrix py3.10/3.11/3.12) | mypy strict, ruff, `pytest -m "unit or bdd or contract" --cov`, F7 (3.12 only) |
| **3** | `integration` | `pytest -m integration --cov` |
| **4** | `security` | bandit, pip-audit, SonarCloud |
| **5** | `union-coverage` | downloads .coverage from Stages 2 and 3, runs F9 on the union |

Plus a `check` fan-in job that gates branch protection on every required job's result. The `check` job ALSO polls SonarCloud's `/api/qualitygates/project_status` and fails on `ERROR` — a Sonar QG regression is an immediate merge block, not advisory. See §G17 for the rationale.

### 6. Wire Codecov

Copy `codecov.yml`. Add the `CODECOV_TOKEN` secret in repo settings.

### 7. Workflow naming convention

Use `1 → 2 → 3` for primary pipeline workflows; `1a → 1b` for conditional loops within a stage:

```
1  · Quality gate                       (every push/PR)
1a · Benchmark gate (retrieval loop)    (path-filtered)
1b · Reference library benchmark gate   (path-filtered)
2  · Pre-merge integration suite        (PR → main)
2a · Dependency review                  (PR → main)
3  · Docker publish (release)
4  · PyPI publish (release)
```

This is for visual ordering in the GitHub Actions UI; it does not affect branch protection (which references job names, not workflow names).

---

## Configuration gotchas (read every one)

These are the specific failures encountered while standing up the harness on kairix. Skipping any one of them costs at least one CI cycle.

### G1. `actions/upload-artifact@v4.4.0+` excludes dotfiles by default

**Symptom:** `Stage 5 — Union coverage floor` fails with `Unable to download artifact(s): Artifact not found for name: coverage-data-integration`. Upstream warning in the upload step is `No files were found with the provided path: .coverage.integration. No artifacts will be uploaded.`

**Cause:** Since v4.4.0 (2024-09), `actions/upload-artifact` excludes any file starting with `.` by default. `.coverage.unit` and `.coverage.integration` are dotfiles, so they get silently dropped from the artifact even though the file exists on disk.

**Fix:** Add `include-hidden-files: true` to every `upload-artifact` step that uploads a dotfile.

```yaml
- uses: actions/upload-artifact@<v4-sha>
  with:
    name: coverage-data
    include-hidden-files: true   # required for .coverage.* dotfiles
    path: |
      .coverage.unit
      coverage.xml
```

### G2. `pytest-cov` may not preserve `.coverage` for F9

**Symptom:** Stage 5 union-coverage downloads the artifact, calls `coverage combine`, and fails because the unit or integration `.coverage` is missing.

**Cause:** Setting `COVERAGE_FILE=.coverage.unit` *during* the pytest run looks like it should make pytest-cov write directly to that filename. In some pytest-cov configurations (notably when `--cov-fail-under=0` is also set), pytest-cov runs `coverage combine` at finalisation and the data file ends up in an unexpected place — or doesn't get written at all.

**Fix:** Don't use `COVERAGE_FILE`. Run pytest with default `.coverage`, then copy after the run:

```yaml
- name: Run tests with coverage
  run: pytest tests/ -m "unit or bdd or contract" --cov=<pkg> --cov-report=xml:coverage.xml ...

- name: Preserve .coverage as .coverage.unit (for F9 union)
  if: matrix.python-version == '3.12'
  run: |
    if [ -f .coverage ]; then cp .coverage .coverage.unit; fi
```

Same shape on the integration job, copying to `.coverage.integration`.

### G3. Integration coverage inherits `fail_under = 80` and fails

**Symptom:** Integration job pytest exits non-zero with `Required test coverage of 80.0% not reached. Total coverage: 47%`.

**Cause:** Integration tests target wiring (factory composition, server build, repository SQL surface), not the full surface. They naturally measure 30-50% line coverage. The pyproject `[tool.coverage.report].fail_under = 80` applies to every `pytest --cov` invocation that doesn't override it.

**Fix:** Pass `--cov-fail-under=0` explicitly on the integration pytest command, with an inline rationale:

```yaml
- name: Integration tests
  # `--cov-fail-under=0` overrides pyproject's `fail_under = 80` —
  # integration tests target wiring, not the full surface, and the
  # 80% floor is enforced by Stage 2 (where the unit suite runs).
  # F7 enforces per-file coverage from the unit run; F9 (Stage 5)
  # combines this with the unit .coverage to enforce the same floor
  # over the union.
  run: |
    pytest tests/ -m integration --cov=<pkg> --cov-report=xml:coverage-integration.xml --cov-fail-under=0 ...
```

The `--cov-fail-under=0` is the only "silencer" of its kind in the workflow; it has to be there because of the architecture, not as a lazy bypass.

### G4. `pytest.importorskip` silently disables tests when extras aren't installed

**Symptom:** F7 measures a file at 0% coverage in CI even though it has a populated unit-test file in `tests/`.

**Cause:** A test file guarded by `pytest.importorskip("X")` — e.g. `pytest.importorskip("starlette")` — silently skips its entire module when the named module isn't importable. If `[X]` is in an optional extras group (e.g. `[agents]`) and the unit job only installs `[dev]`, all tests in that file silently skip and contribute 0% coverage to the file they nominally test.

**Fix two ways:**

1. Make sure the CI test stage installs every extras group whose unit tests it should run:
   ```yaml
   - run: pip install -e ".[dev,agents,nlp,...]"
   ```
2. Add a `reason=` kwarg or a same-line/preceding-comment rationale to every `importorskip` so F11 doesn't reject it (and so the next agent who hits it sees *why* the skip is correct).

   ```python
   pytest.importorskip("starlette", reason="optional [agents] extras; CI installs them")
   ```

### G5. CI pre-commit ruffs `scripts/`; local `safe-commit.sh` does not

**Symptom:** Local commits pass `safe-commit.sh`; CI pre-commit job fails with `ruff (legacy alias) Failed — files were modified by this hook`.

**Cause:** Local `safe-commit.sh` invokes `ruff check kairix/ tests/` (project source + tests). CI's pre-commit hook runs ruff over **everything** matched by `.pre-commit-config.yaml` patterns, which includes `scripts/`. Format auto-fixes there fail the hook (which is configured `--exit-non-zero-on-fix`).

**Fix:** Run `pre-commit run --all-files` locally before pushing. This is the canonical CI parity check. Alternatively, broaden `safe-commit.sh` to ruff `scripts/` too — but matching the pre-commit invocation is more durable.

### G6. SonarCloud `python:S1244` (float equality)

**Symptom:** SonarCloud quality gate fails with `Reliability Rating = C`. Issue: `Do not perform equality checks with floating point values.`

**Cause:** Tests using literal float equality:

```python
assert result.factor == 1.7   # rejected
```

**Fix:** Use `pytest.approx`:

```python
assert result.factor == pytest.approx(1.7)
```

Even when you "know" the value is read literally from a config and never arithmetic'd, Sonar wants explicit-tolerance comparisons on floats. It's quicker to comply than to argue.

### G7. SonarCloud `githubactions:S7637` (action pinned by tag, not SHA)

**Symptom:** SonarCloud security hotspot: `Use full commit SHA hash for this dependency.`

**Cause:** `.github/workflows/*.yml` has `uses: foo/bar@v5` (tag) instead of `uses: foo/bar@<full-sha>` (commit SHA).

**Fix:** Pin every `uses:` line to a 40-char SHA with the tag in a sidecar comment:

```yaml
uses: codecov/codecov-action@75cd11691c0faa626561e295848008c8a7dddffe # v5
```

To resolve the SHA for a given tag:

```bash
gh api repos/<owner>/<repo>/git/refs/tags/<tag> --jq .object.sha
```

### G8. Codecov tracks no flags despite uploading

**Symptom:** Codecov dashboard shows 0 flags even though uploads carry `flags: integration`. Coverage data appears to collide between unit and integration uploads.

**Cause:** Without a `codecov.yml`, the default behaviour overwrites coverage instead of merging by flag. Carryforward isn't enabled by default.

**Fix:** Ship a `codecov.yml` with explicit flag declarations and carryforward:

```yaml
flag_management:
  default_rules:
    carryforward: true
  individual_flags:
    - name: unit
      paths:
        - <pkg>/
      carryforward: true
    - name: integration
      paths:
        - <pkg>/
      carryforward: true
```

Validate before committing:

```bash
curl -sSL -X POST --data-binary "@codecov.yml" https://codecov.io/validate
```

### G9. Codecov patch target = 85% rejects in-trajectory PRs

**Symptom:** Codecov `patch` check fails on PRs that touch already-low-coverage files. PR's patch coverage measures e.g. 81% — perfectly reasonable, but below an arbitrary 85% target.

**Cause:** Codecov has no equivalent of F7's grandfathered baseline. A fixed `patch.target` either rejects in-trajectory PRs or becomes obsolete once project coverage clears it.

**Fix:** Set `patch.target: auto` in `codecov.yml`. `auto` couples the patch gate to the project's actual trajectory: patch must be ≥ current project base coverage. As files leave `.architecture/baseline/per-file-coverage-floor-files.txt`, the project average rises, and the patch bar rises with it automatically.

The mechanical 85% floor stays as F7 / F9 (per-file, on coverage XML). Codecov patch is the no-regression guard, not a parallel mechanical gate.

### G10. Codecov `ignore:` block creates a parallel omit list that drifts

**Symptom:** Files appear in `coverage.xml` that shouldn't be measured. Or files don't appear that should.

**Cause:** Tempting to set `ignore:` in `codecov.yml` to exclude tests/scripts/docs. But the pyproject `[tool.coverage.run].omit` list is already filtering coverage.xml at the source — adding a second omit list creates drift.

**Fix:** Don't add an `ignore:` block to `codecov.yml`. Keep `[tool.coverage.run].omit` in `pyproject.toml` as the single source of truth.

### G11. SonarCloud `S5754` (BDD test capture)

**Symptom:** SonarCloud reliability issue from BDD scenarios when implementation classes appear in `Then` steps.

**Cause:** SonarCloud's Python rules treat `pytest.raises(SomeException)` as a smell unless properly wrapped — and when a BDD scenario asserts `Then SomeException is raised`, SonarCloud sees it as an unhandled exception path.

**Fix:** In step definitions, capture the exception explicitly:

```python
@then("a ConfigValidationError is raised")
def step_impl(context):
    with pytest.raises(ConfigValidationError) as excinfo:
        ...
    context.exception = excinfo.value
```

Then assert against `context.exception` in subsequent steps.

### G12. Bundles are NOT applicable to Python-only repos

Codecov has a `Bundles` product. It analyses JS/TS frontend bundle size. **Don't try to wire it up on a pure Python project**; there's no JS/TS bundle to analyse. Document this explicitly in `codecov.yml`'s header so future agents don't try.

### G13. F12 "happy-path" is *not* a public-surface enumeration

F12 only requires that every `.feature` file have at least one non-`@error` scenario. It does **not** verify that every CLI subcommand or MCP tool has a feature. The latter is in scope for the aspirational issue; F12 is the cheap mechanical signal that catches "this feature is an error catalogue."

If you want public-surface enumeration as well, plan it as a separate fitness function (F-CDC-1 in the aspirational issue). It needs a maintained `.architecture/boundaries.txt` so the enumeration is explicit.

### G14. Branch protection references job names, not workflow names

Workflow names (the `name:` line at the top of `.yml`) drive only the GitHub Actions UI display. Branch protection rules pin to **job names** (e.g. `Stage 0 -- Architecture fitness`, `CI gate`).

**Rename workflows freely.** Renaming jobs requires a coordinated branch-protection update.

### G15. GHSA allow-listing for transitive vulnerabilities

**Symptom:** `dependency-review` action rejects PR because of a CVE in a transitive dependency that has no fix yet.

**Fix:** In `.github/workflows/dependency-review.yml`, allow-list the GHSA with a documented rationale comment:

```yaml
- name: Dependency review
  uses: actions/dependency-review-action@<sha>
  with:
    # GHSA-69w3-r845-3855 — transformers <5.0.0rc3 arbitrary code execution.
    # We don't ship transformers Trainer; only used in the rerank adapter
    # which doesn't accept untrusted input. Re-evaluate when a fix is
    # released.
    allow-ghsas: GHSA-69w3-r845-3855
```

### G16. Pin every `uses:` in every workflow

If you fix G7 only on `ci.yml`, SonarCloud will start flagging the next workflow file you write. **Sweep every workflow file** at setup time. The SHA-resolution shell snippet is already in G7.

### G17. SonarCloud Quality Gate must be a hard gate, not advisory

**Symptom:** A release ships with SonarCloud reporting `ERROR` on the dashboard (e.g. 35 unreviewed security hotspots, `new_security_hotspots_reviewed = 0%`) and the merge went through silently.

**Cause:** The default wiring is advisory in three accidental layers:

1. The Sonar scan step in `ci.yml` may have been added with `continue-on-error: true` to tolerate Sonar outages. **This is the wrong tradeoff** — it means the *job* always reports success regardless of the QG verdict, and a release ships with no Sonar signal at all. If Sonar is down, the right answer is to wait, not to merge blind.
2. The CI gate fan-in job depends on the Sonar *job*'s success, not the SonarCloud QG verdict. So a failing QG with a successful job (i.e. the common case if continue-on-error is set) doesn't trip the gate.
3. The SonarCloud app posts a separate GitHub status check called `SonarCloud Code Analysis` that DOES carry the QG verdict — but unless branch protection requires that specific check, GitHub's merge UI ignores it.

**Fix — three intentionally redundant enforcement layers:**

1. **In the `check` (CI gate) job, poll the SonarCloud QG API and fail on ERROR.** The kairix workflow does this with up to 90 s of polling to absorb the upload-vs-verdict lag:

   ```yaml
   - name: SonarCloud quality gate verdict
     env:
       PROJECT_KEY: <owner>_<repo>
       PR_NUMBER: ${{ github.event.pull_request.number }}
       BRANCH_REF: ${{ github.ref_name }}
     run: |
       set -euo pipefail
       if [ -n "$PR_NUMBER" ]; then
           URL="https://sonarcloud.io/api/qualitygates/project_status?projectKey=${PROJECT_KEY}&pullRequest=${PR_NUMBER}"
       else
           URL="https://sonarcloud.io/api/qualitygates/project_status?projectKey=${PROJECT_KEY}&branch=${BRANCH_REF}"
       fi
       for i in 1 2 3 4 5 6; do
           RESPONSE=$(curl -fsS --max-time 10 "$URL" || echo '{}')
           STATUS=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('projectStatus',{}).get('status','MISSING'))" 2>/dev/null || echo "MISSING")
           [ "$STATUS" = "OK" ] && exit 0
           [ "$STATUS" = "ERROR" ] && { echo "$RESPONSE"; exit 1; }
           sleep 15
       done
       exit 1   # missing verdict after 90s = fail; do not merge with no Sonar signal
   ```

2. **Add `SonarCloud Code Analysis` to the branch protection's required status checks**, alongside `check`:

   ```bash
   gh api -X POST "repos/<owner>/<repo>/branches/main/protection/required_status_checks/contexts" \
       --input - <<<'["SonarCloud Code Analysis"]'
   ```

3. **Add a `sonar-gate` job to release-event workflows** (Docker publish, PyPI publish). Even a manually-created release event will not trigger image push or PyPI upload until SonarCloud reports `OK` for `main`:

   ```yaml
   jobs:
     sonar-gate:
       name: SonarCloud Quality Gate (release)
       runs-on: ubuntu-latest
       steps:
         - name: SonarCloud quality gate verdict for release
           env:
             PROJECT_KEY: <owner>_<repo>
             BRANCH_REF: main
           run: |
             # Same poll-and-fail logic as the CI gate above
             ...

     build-and-push:
       needs: sonar-gate   # release-event publish gated on Sonar OK
       ...
   ```

4. **Do NOT use `continue-on-error: true` on the Sonar scan step.** If Sonar is unavailable, the security stage MUST fail. The "tolerate outages" tradeoff sounds reasonable but produces silent releases without verdict — exactly what the gate is supposed to prevent.

All three layers — CI gate poll, branch-protection check, release-event poll — protect against each other failing. Fixing only one is fragile.

**Triage:** failing hotspots are at `https://sonarcloud.io/project/issues?id=<projectKey>`. The `new_security_hotspots_reviewed` condition checks the new-code window only; clearing the backlog of pre-existing hotspots is a one-time hygiene task that doesn't repeat. See kairix #174 for the canonical triage runbook.

---

## Reference configs

The exact files that work for kairix. Copy these directly to a new repo and adapt the package name.

### `codecov.yml`

See `codecov.yml` in this repo. Validates against `https://codecov.io/validate`. Bundles deliberately not configured (Python-only).

### `.pre-commit-config.yaml`

See `.pre-commit-config.yaml`. Architecture fitness function hooks are at the bottom.

### `.github/workflows/ci.yml`

See `.github/workflows/ci.yml`. The five-stage pipeline, with all silencers rationaled (F10 ships clean) and all `uses:` pinned (G7).

### `pyproject.toml` quality config

The relevant snippets from `pyproject.toml`:

```toml
[tool.coverage.run]
source = ["<pkg>"]
relative_files = true
omit = [
    "tests/*",
    # Per-file rationales — each entry must explain why measurement is
    # excluded, not just listed.
    "<pkg>/_external_io.py",
]

[tool.coverage.report]
fail_under = 80
show_missing = true
exclude_lines = [
    "pragma: no cover",
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
    "\\.\\.\\.",
]

[tool.pytest.ini_options]
markers = [
    "unit: unit tests, fast, no I/O",
    "bdd: behaviour-driven scenarios",
    "contract: protocol compliance",
    "integration: real DB / external service",
    "e2e: end-to-end pipelines",
    "slow: anything > 5s",
]
```

The `markers` list drives F8.

---

## Branch protection

Required jobs (configure in repo Settings → Branches → branch protection rules):

- `CI gate` — fan-in job; this is the single check the rule depends on
- `Detect changes` — path filter

The `CI gate` job in `ci.yml` `needs:` every required job and fails the gate if any of them failed. Branch protection only needs to pin to that one check — it transitively enforces every fitness function and every test stage.

If you rename jobs (not workflow files), update the branch protection rule to match. Workflow renames are free (see G14).

---

## What to leave for later

Do not try to ship these in the initial setup; they're tracked in the aspirational practices issue.

- **Consumer-Driven Contract testing** for boundary surfaces (Pact-style). Needs a maintained `.architecture/boundaries.txt`.
- **Three Amigos process** for BDD authoring — process gate, not code gate. Add a PR template question.
- **Mutation testing** for integration paths (mutmut / cosmic-ray). Weekly job, not per-PR.
- **Path-coverage / branch-coverage gates** beyond line coverage.

References for these are in the aspirational issue (cite Ford / Sadalage / Kua, Adzic, North, Keogh, Wynne, Robinson, mutmut, cosmic-ray).

---

## Appendix: useful commands

```bash
# Validate codecov.yml
curl -sSL -X POST --data-binary "@codecov.yml" https://codecov.io/validate

# Resolve action tag → commit SHA (for pinning)
gh api repos/<owner>/<repo>/git/refs/tags/<tag> --jq .object.sha

# Run the full local quality gate
bash scripts/safe-commit.sh "message"

# Run pre-commit over all files (CI parity)
pre-commit run --all-files

# Run all fitness functions standalone
bash scripts/checks/run-all.sh --skip-coverage
# With F7:
bash scripts/checks/run-all.sh   # requires coverage.xml in cwd

# Generate F9 union coverage locally
COVERAGE_FILE=.cov_unit pytest tests/ -m "unit or bdd or contract" --cov=<pkg> --cov-report=
COVERAGE_FILE=.cov_integration pytest tests/ -m integration --cov=<pkg> --cov-report= --cov-fail-under=0
cp .cov_unit .coverage.unit
cp .cov_integration .coverage.integration
coverage combine --keep .coverage.unit .coverage.integration
coverage xml -o coverage-union.xml
python3 scripts/checks/check_per_file_coverage.py coverage-union.xml per-file-coverage-floor-union
```

---

## See also

- [`fitness-functions.md`](./fitness-functions.md) — canonical reference for every rule.
- [`ENGINEERING.md`](./ENGINEERING.md) — broader architecture rules.
- Aspirational practices issue — Pact CDC, Three Amigos, mutation testing.
