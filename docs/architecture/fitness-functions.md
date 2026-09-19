# Architecture fitness functions — canonical reference

**Status:** Authoritative. Linked from CLAUDE.md and the canonical index at
[tc-pipelines `governance/STANDARDS.md`](https://github.com/three-cubes/tc-pipelines/blob/main/governance/STANDARDS.md).
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

If a rule needs per-instance precision later, the shared helper library
(`tc_fitness`, which kairix consumes — see "Shared engine" under Harness
architecture) can be extended without changing the gate semantics.

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

### Catalogue index (generated)

The table above is the hand-curated detail surface (detection / tool /
SDLC layer). The table below is the **machine-derived index** — one row
per `RuleEntry` in `scripts/checks/_rule_catalogue.py`, the single source
of truth (the `RuleEntry` schema itself is imported from the shared
`tc_fitness.catalogue`; kairix owns the F-numbered rows). It is regenerated by
`python3 scripts/checks/generate_catalogue_docs.py` and the F92 catalogue-currency
gate fails the build if it drifts. To change it, edit the catalogue.

<!-- BEGIN F-CATALOGUE (generated; edit _rule_catalogue.py) -->

_Generated from `scripts/checks/_rule_catalogue.py` — do not edit by hand._

| ID | Category | Scope | Status | Summary |
|----|----------|-------|--------|---------|
| F26 | layering | per-file | shipped | kairix/core/** may not import kairix/providers/** or kairix/transport/** |
| F27 | layering | per-file | shipped | kairix/providers/<a>/ may not import another provider — plugins ship independently |
| F34 | layering | per-file | shipped | kairix/core/connectors/** may not import kairix/connectors/** or kairix/extractors/** |
| F35 | layering | per-file | shipped | kairix/connectors/<a>/ may not import another connector or any extractor |
| F37 | layering | per-file | shipped | change-detection / sync code only under kairix/connectors or kairix/core/connectors |
| F38 | layering | per-file | shipped | Silver processing (chunking + signal extraction) only in kairix/core/connectors/silver.py |
| F44 | layering | per-file | shipped | engagement-scope code may not import firm-scope storage clients (psycopg etc.) |
| F61 | layering | per-file | shipped | bare _SqliteChunkWriter(db, collection=...) construction only under kairix/core/connectors/ |
| F1 | test-discipline | per-file | shipped | no @patch / monkeypatch on kairix internals — inject Fake* through a seam |
| F2 | test-discipline | per-file | shipped | no monkeypatch.setenv("KAIRIX_*") — pass deps as kwargs instead |
| F5 | test-discipline | per-file | shipped | no internal-name imports in tests — use public surface only |
| F6 | test-discipline | per-method | shipped | no *_fn=None test-only kwargs in production |
| F7 | coverage | per-file | shipped | per-file coverage ≥ 90% (unit) — Stage 2 floor |
| F8 | test-discipline | per-test | shipped | every test_* carries a category marker (unit/bdd/contract/integration/e2e/slow/soak/invariant) |
| F9 | coverage | per-file | shipped | per-file coverage ≥ 90% on union of unit + integration (Stage 5) |
| F11 | test-discipline | per-test | shipped | every pytest.mark.skip/skipif/xfail/importorskip has a rationale comment |
| F12 | test-discipline | per-file | shipped | every BDD feature has a happy-path scenario |
| F13 | test-discipline | per-file | shipped | BDD scenarios reject implementation symbols (Mock, kairix.<pkg>.<symbol>) |
| F45 | test-discipline | per-commit | shipped | every new CLI/MCP/provider/connector/extractor adds a BDD feature in the same commit |
| F46 | test-discipline | per-file | shipped | BDD step impls compose via CLI/MCP/factory — no direct *Pipeline(...) construction |
| F47 | test-discipline | per-file | shipped | integration tests construct multi-component pipelines via kairix.core.factory.build_* |
| F48 | test-discipline | cross-cutting | shipped | tests/e2e/test_composed_production_path.py exists, runs in CI Stage 4.5 |
| F54 | feature-flag | per-flag | shipped | every flag has OFF + ON BDD scenarios, integration tests, and (for top-level) an E2E composed-path test |
| F62 | test-discipline | per-class | shipped | every stateful tick/run_batch component has a multi-tick advance/idempotency test |
| F68 | test-discipline | per-protocol-method | shipped | every Protocol method has a failure-injection contract test |
| F69 | test-discipline | per-test | shipped | every integration test with .fetchall()/list_changes has a ≥10K-row variant |
| F72 | test-discipline | cross-cutting | shipped | every cross-layer integrity invariant has a fixture-scale AND soak-scale test |
| F81 | test-discipline | cross-cutting | shipped | CI fresh-install smoke — clean dir → compose boot → healthz → MCP handshake → wizard 200 → wizard choreography (POST scan partial + key form→redirect) → BM25 search hit (scripts/checks/check-fresh-install-smoke.sh via .github/workflows/fresh-install-smoke.yml; per-commit leg checks the wiring) |
| F82 | test-discipline | per-test | shipped | wall-clock ceiling assertions banned outside soak/probe tiers — elapsed-time vs numeric ceiling requires a slow/soak/load/pvt marker or # F82-allowed: rationale (#493 flake family) |
| F84 | test-discipline | per-method | shipped | every production config-write site (write_config_updates / update_config_file / write_config_yaml / config-writer-named yaml.dump) has a composed write→read round-trip test through the canonical layered reader (#492 overlay split-brain class) |
| F88 | test-discipline | per-method | shipped | every SetupService / KairixSetupService method documenting a concrete Raises: type is either handled (except, incl. superclass) in the wizard route that calls it or render-tested under tests/platform/setup (session-escape-5 raw-500 class) |
| F87 | test-discipline | cross-cutting | shipped | every registered persist/load pair (set_secret/load_secrets_file, FileTokenStore/secrets read, write_config_updates/load_merged_mapping, EmbeddingCache put_many/get_many) ships an adversarial round-trip corpus — multi-line + unicode + large (>=64KiB) + escape-lookalike (the GitHub-PEM consent-failure class) |
| F86 | test-discipline | per-method | shipped | DI-default execution floor (static half) — every _default_* production seam in kairix/** stays visible to the coverage floor: no # pragma: no cover (escape-4 class) |
| F86-dynamic | test-discipline | per-method | shipped | DI-default execution floor (dynamic half) — every _default_* seam body has ≥1 executed line in the union coverage report; skips clean when no report (F9 stage) |
| F28 | plugin-contract | per-plugin | shipped | every provider plugin has matching BDD feature + Examples-table row in E2E features |
| F36 | plugin-contract | per-plugin | shipped | every connector + extractor plugin has matching BDD feature + Examples-table row |
| F40 | plugin-contract | per-plugin | shipped | every Extractor plugin declares module-level version: str + make_extractor factory |
| F41 | plugin-contract | per-plugin | shipped | every plugin tree has py.typed marker + no unjustified # type: ignore |
| F42 | plugin-contract | per-protocol-method | shipped | Protocol methods return frozen-dc/tuple — never dict[str, Any] or bare Any |
| F43 | plugin-contract | per-plugin | shipped | behavioural parity — every contract test runs ONE parametrized body over real + fake (≥2 impl fixtures), not separate real-only/fake-only assertions; plus the per-plugin contract-test presence limb |
| F55 | plugin-contract | per-plugin | vacuous | every Chunker plugin declares version + every Chunk(...) passes chunker_version= |
| F56 | plugin-contract | per-plugin | shipped | every connector declares SourceConnector + at least one of {Poll, Checkpointed, Event}Connector |
| F64 | plugin-contract | per-plugin | shipped | every plugin importing an HTTP client ships a rate-limit test (429/Retry-After) |
| F65 | plugin-contract | per-plugin | shipped | every connector implements metadata_for + propagation test for chunk_date/author |
| F15 | production-safety | per-file | shipped | no logging of secret-named variables in plaintext outside kairix/{secrets,credentials}.py |
| F39 | production-safety | per-method | shipped | every Chunk(...) constructor call passes source_uri + source_modified_at + sensitivity explicitly |
| F50 | production-safety | per-commit | shipped | net-new files may not appear in any per-file F-rule baseline |
| F63 | production-safety | per-file | shipped | every .fetchall() includes LIMIT in the query or carries a # F63-bounded: rationale |
| F66 | production-safety | per-class | shipped | every connector + tick-driven component declares per_tick_max_items + disk_watermark_min_free_bytes |
| F73 | production-safety | per-file | shipped | token-pattern scanner for private infra identifiers (externalised pattern source) |
| F89 | production-safety | per-file | shipped | every served file under a kairix/**/web/static/ tree has a sha256-pinned ASSETS.lock manifest row (upstream version + sha256 + url + rationale); the on-disk sha256 must match the row, so a swapped/outdated htmx.min.js or pico.css fails instead of shipping untraced |
| F94 | production-safety | per-file | shipped | no runtime writes to system/OS paths (/etc, /opt, /usr, ...) — production code in kairix/** persists config + state through kairix.paths (the writable data dir) and the config overlay, never a hardcoded system path, so kairix runs least-privilege on hardened / read-only-root VMs (the wizard-save overlay class, #485/#492) |
| F91 | production-safety | per-file | shipped | wizard browser surface — HTML responses carry nosniff + frame-denial + a Content-Security-Policy (Limb A: contract test + render-path static check), and every inline <script> in the setup templates is F91-inline rationale-tagged, ≤20 lines, and not cross-template-duplicated (Limb B) |
| F57 | schema-integrity | per-file | shipped | every UPDATE topology_cc_pairs SET status=? lives next to a _ALLOWED_TRANSITIONS dispatch dict |
| F58 | schema-integrity | cross-cutting | vacuous | HierarchyConnector impls have a parent-before-child contract test |
| F67 | schema-integrity | per-table | shipped | every pushed_to_<sink> column has a matching UPDATE site flipping 0 → 1 |
| F70 | schema-integrity | per-table | shipped | every CREATE TABLE has at least one INSERT INTO site OR a # table-is-derived: rationale |
| F71 | schema-integrity | per-method | shipped | every preflight _check_* counting external state has a count-equals-ground-truth contract test |
| F51 | feature-flag | per-flag | shipped | every FeatureFlag has target_retire_in ≤ current scm version + 6 months |
| F52 | feature-flag | per-flag | shipped | every flag("<name>") call site references a name that exists in REGISTRY |
| F53 | feature-flag | cross-cutting | shipped | kairix features status CLI subcommand + features_status MCP tool both exist |
| F3 | agent-affordance | per-file | shipped | every # noqa / # NOSONAR / # pragma / # type: ignore / # nosec has rationale text |
| F10 | agent-affordance | cross-cutting | shipped | CI workflow silencers (continue-on-error, fail_ci_if_error: false) require rationale |
| F14 | agent-affordance | cross-cutting | shipped | every sonar.issue.ignore.multicriteria entry has a preceding rationale comment |
| F16 | agent-affordance | per-method | shipped | cognitive complexity ≤ 15 per function (Sonar S3776) |
| F17 | agent-affordance | per-file | shipped | no string literal ≥10 chars duplicated ≥3 times in a module (Sonar S1192) |
| new_code_coverage | coverage | per-file | shipped | new-code coverage ≥ 80% on changed lines (Sonar new-code, local) |
| F18 | agent-affordance | per-file | shipped | no commented-out code (Sonar S125) |
| F19 | agent-affordance | per-method | shipped | unused function parameters must be _-prefixed (Sonar S1172) |
| F20 | agent-affordance | per-method | shipped | empty function bodies require docstring or # Intentionally empty — comment (Sonar S1186) |
| F21 | agent-affordance | cross-cutting | shipped | every check_*.py failure-output carries fix:/next:/run: action markers |
| F23 | agent-affordance | cross-cutting | shipped | every top-level directory has a README.md resolver |
| F83 | agent-affordance | per-file | shipped | gate-runner contract for shell gate scripts — no unguarded VAR=$(...) under set -e (#483 silent-death class), \|\| true requires trailing rationale, shellcheck-clean at error severity, safe-commit.sh/run-all.sh stages emit named OK/FAIL verdicts, and quiet grep output probes cannot produce false failures under pipefail |
| F85 | agent-affordance | per-file | shipped | registered cross-tier contract vocabularies single-sourced — a member of a DECLARED vocabulary (source-auth PHASE_* strings, the azure provider-name set; owned by kairix/platform/setup/service.py) re-declared as a constant/collection in another setup-tier module, or used as a raw string in a template instead of the env.globals symbol, fails (session-escape-8 phase-string class) |
| F90 | agent-affordance | per-file | shipped | setup-wizard template ↔ route referential integrity (F52 applied to the browser tier) — every hx-get/hx-post/href/action /setup URL resolves to a Route/Mount in routes.py, every hx-target/hx-include #id is defined in some template, every template is rendered or extended (dangling-button class: the tour replacing first-search left no dead control) |
| F4 | repo-hygiene | per-file | shipped | no os.environ.get("KAIRIX_*") outside paths.py / secrets.py |
| F22 | repo-hygiene | per-file | shipped | repo paths follow per-tree naming conventions |
| F24 | repo-hygiene | per-file | shipped | no `from tests.*` / `import tests` inside kairix/**/*.py — tests not shipped in wheel |
| F29 | repo-hygiene | per-file | shipped | performance-measurement code only under kairix/quality/probe/ |
| F30 | test-discipline | cross-cutting | shipped | every CLI subcommand + every MCP tool has an outcome test (subprocess or direct handler) |
| F32 | repo-hygiene | per-file | shipped | no real names in test fixtures (use agent-alpha etc. + reference library) |
| F33 | repo-hygiene | per-file | shipped | shellcheck disable directives require rationale |
| F74 | observability | per-class | vacuous | every Stage subclass is only invoked via a StageRunner — never direct .process() call |
| F93 | observability | cross-cutting | shipped | every ci.yml job is in the 'CI gate' aggregator's needs: closure (so its failure blocks the merge) or carries a # fan-in: informational marker — a green merge can't ship with an un-gated job failing |
| F75 | test-discipline | cross-cutting | proposed | every CLI subcommand + MCP tool + connector appears in at least one eval-suite question |
| F76 | production-safety | per-file | shipped | no f-string interpolation of content-like vars (raw/body/payload/markdown/...) in log/exception/dead-letter strings |
| F77 | schema-integrity | per-file | proxy | sqlite3.connect call sites outside the allow-list (worker/factory/scripts/tests) are flagged |
| F78 | production-safety | per-test | proposed | soak suite asserts RSS / peak memory ≤ budget — needs runtime instrumentation |
| F79 | schema-integrity | per-commit | proposed | every schema delta has a tested rollback path; destructive changes keep N-day grace |
| F80 | layering | cross-cutting | proposed | engagement-scope code may not call firm-scope APIs at runtime — extends F44 from import to request |
| baseline-shrinking | coverage | cross-cutting | shipped | F49: each release tag reduces F30/F46/F47 baselines by ≥1 (or keeps at zero) |
| paydown-doc-currency | agent-affordance | cross-cutting | shipped | grandfathering paydown doc reflects current baseline state |
| sonar-new-code | coverage | cross-cutting | shipped | SonarCloud per-file count ratchet — current per-file open-issue/hotspot counts may not exceed the committed baseline (.architecture/baseline/sonar-per-file*.json); deterministic, no live leak period, no skip flag |
| worktree-isolation | process | cross-cutting | shipped | subagent worktree isolation — no shadow copies in primary checkout |
| F92 | process | cross-cutting | shipped | catalogue currency — every check_*.{py,sh} has a RuleEntry, every RuleEntry maps to an existing check, and the generated doc regions match generate_catalogue_docs.py --check (the self-hosting guard for the catalogue-driven runner) |
| capability-affordance | agent-affordance | cross-cutting | shipped | agent-callable capabilities surface their affordances at the call boundary |
| no-hardcoded-user-paths | repo-hygiene | per-file | shipped | F31: no hardcoded /Users/ or /home/<dev>/ paths |
| F95 | production-safety | per-file | shipped | Neo4j write-mode selection lives at ONE chokepoint — only kairix/knowledge/graph/client.py may name `default_access_mode` / the `_is_write_query` derivation, so no caller re-derives read-vs-write and silently opens a READ session for a write query (the #628 silent-write class) |
| F96 | production-safety | per-file | shipped | embed-discovery completeness queries that LEFT JOIN content_vectors ... IS NULL must reference a state column (model / embedded_at), never presence alone — a presence-only join counts an un-promoted placeholder as 'done' (the chunk-0 #627 class, where seq=0 placeholders were never embedded) |
| F97 | agent-affordance | per-class | shipped | every agent-facing result surface embeds or returns the shared SourceRef breadcrumb — each registered result-row dataclass (search / timeline / entity / prep / research / contradict) declares a SourceRef-typed field OR a source_ref() accessor, so the canonical resolvable source_uri is surfaced uniformly and the per-surface pointer drift can't re-accrue (PLA-274) |
| F98 | agent-affordance | per-class | shipped | every agent result-row surface exposes an expand-acceptable locator AND expand accepts a source_uri-only call — each registered surface (search / timeline / entity / prep / research / contradict / expand) exposes the resolvable source_uri (source_ref() / SourceRef field / source_uri field) an agent feeds to expand, and run_expand keeps its seq parameter optional, so a document/section-level (L2) hit whose seq is null never dead-ends at a guessed #0 (the anti-dead-end lock, PLA-297) |
| SGO-156 | repo-hygiene | per-file | shipped | no AI/LLM self-attribution residue in first-party source/docs — no model co-author trailer, no AI-vendor no-reply author identity, no robot emoji (the metadata agent tools stamp onto commits); agent work is authored by the canonical bot/human, never advertised as model-generated (decision D1) |
| SGO-158 | process | per-commit | shipped | every commit author AND committer over the PR range (cutover..HEAD) carries an allow-listed identity — the canonical three-cubes-agent App, the named human maintainer, and the platform merge/bot committers — so an off-allowlist or marker-in-name identity can't slip in; guard-forward via cutover_ref (decision D2) |
| SGO-270 | process | cross-cutting | shipped | the root harness references the central engineering canon — as a product repo, kairix carries the full root harness set (CLAUDE.md, AGENTS.md, RESOLVER.md, ETHOS.md, SCORECARD.md, CONTRIBUTING.md) and at least one harness file carries BOTH the `Canonical standards` marker AND a link to tc-pipelines governance/STANDARDS, so agents route to canon instead of re-deriving a parallel standard (Golden Path convergence) |
| SGO-269 | process | cross-cutting | shipped | kairix's CI consumes the ONE shared quality gate — Stage-0 arch-fitness runs the `tc-fitness run` engine via the python-quality-gate reusable — rather than a hand-rolled fork, so the single standard is enforced mechanically (Golden Path convergence) |
| F99 | agent-affordance | cross-cutting | shipped | bundled usage guide stays in sync with the tool registry — every capability in tool_capabilities() (each registered MCP tool + CLI subcommand) is discoverable in kairix/agents/usage_guide/data/agent-usage-guide.md via its CLI / bare <mcp_tool> wire-name token (the guide is generated from CAPABILITIES_CATALOG with the bare names), so an agent self-training from the guide can find every shipped tool (the drift class that let `expand` fall out); flag-gated-off surfaces are excluded (PLA-299 / PLA-321) |
| G1 | go-discipline | per-file | shipped | every Go binary exposes --version |
| G6 | go-discipline | per-file | shipped | no panic outside main/init |
| G8 | go-discipline | per-file | shipped | logging via log/slog (no log.Println or fmt.Println in service code) |
| G9 | go-discipline | per-plugin | shipped | every services/<name>/ has a README.md |
| G10 | go-discipline | per-plugin | shipped | dependency-rationale registry per services/<name>/DEPENDENCIES.md |

<!-- END F-CATALOGUE -->

### F73 pattern source — org config, not a local file

F73's token-pattern set is **externalised to org config** in the
`three-cubes` org, single-sourced for CI and local dev:

- **CI** reads the org **secret** `PRIVATE_INFRA_PATTERNS` into the
  `PRIVATE_INFRA_PATTERNS` env var (wired in `.github/workflows/ci.yml`).
- **Local dev** reads the org **variable** `PRIVATE_INFRA_PATTERNS`
  (org-member-readable) with the `gh` CLI:

  ```bash
  # Export into the current shell (preferred):
  eval "$(bash scripts/fetch-fitness-config.sh)"

  # Or cache it to the gitignored .private-infra-patterns fallback:
  make fitness-config
  ```

The hand-maintained `.private-infra-patterns` file is a **last-resort
fallback/cache only** — the org variable is the canonical local source.
With no patterns loaded the scanner is a no-op locally (CI remains the
backstop), so a fresh clone never hard-fails on a missing file.

F73 is also why shared, reusable CI workflows stay **secret-free**: a
reusable workflow must take secrets from its caller (`secrets:` inputs /
`${{ secrets.* }}`), never hard-code a literal, and public artefacts
(committed workflows, docs, BDD scenarios) must not name private sibling
repositories or infrastructure. The token-pattern scanner backstops the
second half; the first half is review-time discipline on every
`workflow_call` author.

---

## The rules in detail

Each rule below is described with: **statement**, **why**,
**detection mechanism**, **examples** (rejected and allowed), and
**fix pattern**.

### F1 — No internal-substitution patching of kairix code

#### Statement

Test files MUST NOT reach into a production kairix module's namespace
to swap an implementation. F1 flags six structurally-identical shapes:

1. `@patch("kairix.X.Y", ...)` — decorator
2. `with patch("kairix.X.Y", ...):` — context manager
3. `kairix.X.Y = <expr>` — full-path attribute assignment
4. `<alias>.Y = <expr>` where alias resolves via imports to a kairix module
5. `monkeypatch.setattr("kairix.X.Y", ...)` — string-target form
6. `monkeypatch.setattr(<kairix module ref>, "attr", fake)` — ref-target form

Stdlib (`os`, `time`, `pathlib`, `sys`, `importlib`, ...) and external
SDKs (`httpx`, `openai`, `boto3`, `anthropic`, `requests`, `numpy`,
`neo4j`, ...) remain allowed — patching at those boundaries fixtures
genuinely external state at the kairix edge.

#### Why

Patches couple tests to module structure (`patch("kairix.foo._helper")`
breaks silently when `_helper` is renamed or moved). They also make
production code grow defensive shims to remain mockable, which is the
test-shaped-API smell.

The replacement is **constructor injection** or a **`Protocol` seam**
from `kairix.core.protocols`. `tests/fakes.py` holds canonical Fake*
implementations of every domain Protocol. When the only way to test
a function is to reach into its module's namespace, the function is
the problem: it has a hidden dependency (function-local imports or
at-call-time resolution) that should be moved to construction time via
a `*Deps` dataclass with `default_factory`. See `EmbedDependencies`,
`LLMBackendDeps`, `BenchmarkDeps` for the canonical shape, then inject
the Fake* at construction.

#### Detection

`scripts/checks/check-no-internal-patches.sh` delegates to
`scripts/checks/check_no_internal_patches.py`. The detector is
AST-based, walks each test file's imports to resolve aliases, and
flags any of the six shapes against the alias-resolved root.
Multi-line constructs, aliased imports
(`import kairix.paths as paths_mod`), from-imports
(`from kairix import providers as providers_mod`), and full-path
forms (`kairix.paths.provider_name = ...`) are all caught.

The detector's own tests live at
`tests/architecture/test_check_no_internal_patches.py` — each of the
six shapes has a positive (kairix target → violation) and negative
(stdlib/external target → allowed) test. To verify the gate stays
honest: comment out the detector branch for a shape, run the matching
positive test, confirm red, restore, confirm green.

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
# kairix/agents/briefing/sources.py
docs_root = os.environ.get("KAIRIX_DOCUMENT_ROOT", "/data/documents")

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
# kairix/agents/briefing/sources.py
docs_root = os.environ.get("KAIRIX_DOCUMENT_ROOT")

# After
# kairix/paths.py — single env-var read, exposed as a field
@dataclass(frozen=True)
class KairixPaths:
    document_root: Path
    ...
    @classmethod
    def resolve(cls):
        return _resolve_cached()  # reads KAIRIX_DOCUMENT_ROOT once

# kairix/agents/briefing/sources.py — uses the resolved value
docs_root = KairixPaths.resolve().document_root
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
| Cross-layer integrity (F72 / Bundle E) | `invariant` | `tests/integrity_invariants/` |
| Production-scale soak (nightly) | `soak` | `tests/soak/`, `*_soak` modules |
| Anything > 5s | `slow` | (orthogonal — combine with tier) |

If every test in a file is the same tier, prefer module-level
`pytestmark` over decorating each function.

Re-tiering caveat: a per-function `@pytest.mark.soak` does NOT cancel a
module-level `pytestmark = pytest.mark.unit` — pytest STACKS markers, so
the test still runs on the per-commit `unit` path. To actually move a test
off the per-commit path, relocate it into a dedicated soak module
(`tests/soak/…`) carrying `pytestmark = pytest.mark.soak`, don't just add
the decorator. (Canonical tiering spec: ADR-024.)

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

Allow-listed: `_lib.sh`, `run-all.sh`,
`audit_baselines.py`, `merge_coverage_xml.py` — shared helpers and the
harness/orchestrator (no per-rule remediation of their own). The Python
gating helpers now live in the installed `three-cubes-fitness` package
(`tc_fitness`), outside the F21 scan tree.

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
| `scripts/checks/` | `*.py` | `check_<rule>.py`, `_fitness_rule.py`, `audit_baselines.py`, `merge_coverage_xml.py` |
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

### F30 — Every CLI subcommand and MCP tool has an outcome test

#### Statement

Every subcommand listed in `kairix/cli.py:COMMANDS` AND every
`@server.tool()`-decorated function in `kairix/agents/mcp/server.py`
MUST have at least one test that:

1. Spawns the kairix subprocess (or invokes the MCP tool handler), AND
2. Asserts on captured stdout / stderr / returned envelope content —
   NOT on `returncode == 0` alone, NOT on internal call-counts of fakes.

Pre-existing surfaces without outcome tests are grandfathered in
`.architecture/baseline/f30-operator-outcome-tests-files.txt`. The
baseline shrinks only — adding an outcome test removes the
corresponding entry in the same commit. Net-new subcommands /
net-new MCP tools without outcome tests hard-fail.

#### Why

Plan B-parity (v2026.5.x) shipped 5 capabilities across 6 weeks with
5233 unit + contract + BDD tests green at every gate. The post-Plan-B
LoCoMo benchmark returned **5.0%** — below the **11%** pre-Plan-B
baseline. Root cause: the `_hit_from_fact` adapter returned empty
`snippet` / `path`, the synthesiser saw empty context, and the
user-visible response was "No relevant content found in the knowledge
store" — even though `fact_retriever` was firing and 2536 facts were
indexed.

Every layer's unit tests passed because every layer injected fakes.
The composition `subprocess → kairix prep → SearchPipeline →
fact_retriever → fusion → synthesiser → LLM` was untested against a
real ingested fact.

> **Tests at object/method boundaries can be silently irrelevant.**
> A passing test suite is not evidence that the user-visible feature
> works — it's evidence that each component, given the inputs the
> test author imagined, returns the outputs the test author expected.
> The composition through the production code path is what the user
> consumes; outcome tests are the only thing that mechanically verifies
> the composition.

F30 mechanises the rule "the production code path gets exercised
end-to-end with realistic input + observable output assertions."

#### Detection

`scripts/checks/check_f30_operator_outcome_tests.py`. Steps:

1. AST-parse `kairix/cli.py` → enumerate `COMMANDS` dict keys (subcommand names)
2. AST-parse `kairix/agents/mcp/server.py` → enumerate `@server.tool()`-decorated function names (MCP tool names)
3. Walk `tests/**/*.py`; for each test file:
   - For each `subprocess.run(...)` / `subprocess.Popen(...)` call, collect every string-literal in the first positional argument. If any matches a subcommand name AND the same file contains at least one `assert` referencing `.stdout` / `.stderr`, that subcommand counts as covered.
   - For each direct call to `tool_<name>(...)` (or `<obj>.tool_<name>(...)`), if the matching MCP tool exists AND the same file contains at least one `assert` operating on a `Subscript` or `Attribute` (envelope-content assertion), that MCP tool counts as covered.
4. Subcommands NOT covered → anchor the violation at the canonical implementation file (resolved from `COMMANDS` module path).
5. MCP tools NOT covered → anchor the violation at the synthetic path `kairix/agents/mcp/server.py/@tool:<name>` (one entry per uncovered tool).
6. Gate via `tc_fitness.gate(...)` — net-new violations fail; baseline shrinks only.

#### Hard-fail mechanics

Per the project directive *"hard fail for any changed code (so this is
refactored upfront in any new features or defect remediation)"*:

- **New subcommand or MCP tool** without a matching outcome test → hard-fail commit. Baseline cannot be expanded.
- **Existing baselined subcommand whose outcome test exists** — removing or weakening the test → hard-fail commit.
- **Existing baselined subcommand** — modifying the subcommand's primary implementation file is permitted, but PR review should expect the outcome test to land in the same PR. The "refactor upfront" mechanic relies on author + reviewer judgement here; F30's mechanical gate is on the existence of the outcome test, not on the diff scope.
- **Baseline shrinks only.** Every commit that adds an outcome test removes the corresponding baseline entry. Net direction is monotonic.

#### Out of scope

F30 measures the **existence of an outcome test**. It does not measure:

- Test coverage percentage within the outcome test (that's F7 / F9)
- Whether the outcome test exercises every code path
- Whether the outcome test runs in CI (assumed — CI markers are F8's job)
- Performance of the outcome test (acceptable for it to be slow — `@pytest.mark.integration`)

The single signal: does the production code path get exercised
end-to-end with realistic input + observable output assertions?
F30 is binary per subcommand / per MCP tool.

#### Examples

**Pass**:

```python
@pytest.mark.integration
def test_ingest_chat_then_prep_round_trip_surfaces_fact(tmp_path):
    """Ingest a fact, query for it, assert the value appears."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"role": "user", "content": "Caroline is VP of People"}) + "\n"
    )
    subprocess.run(["kairix", "ingest-chat", str(transcript)], ..., check=True)

    r = subprocess.run(
        ["kairix", "prep", "What is Caroline's role?"],
        capture_output=True, check=False, env=env, timeout=120,
    )
    out = r.stdout.decode()
    assert "VP" in out                          # value surfaces
    assert "No relevant content" not in out     # explicit anti-template
```

**Forbidden** (the pattern Plan B-parity used everywhere):

```python
def test_prep_smoke(fake_search):
    result = prep_main(["What is X?"], search=fake_search)
    assert fake_search.called  # internal-fake assertion, not outcome
    # — never exercises the subprocess path; synth bugs invisible
```

#### Fix pattern

Add `tests/integration/test_<subcommand>_outcome.py` with:

- `@pytest.mark.integration` marker (carries category for F8)
- `subprocess.run(["kairix", "<subcommand>", ...])` invocation
- Realistic input (multi-word natural-language, not lorem-ipsum)
- Assert on captured stdout / stderr content (not just `returncode == 0`)
- Sabotage-proof: mutate the production implementation → confirm the
  outcome test fails → restore. Document the sabotage proof in a
  comment per existing project convention.

Then remove the file's entry from `.architecture/baseline/f30-operator-outcome-tests-files.txt` in the same commit.

#### Reference

The Plan B-parity post-mortem document is the canonical motivation
record: see `docs/architecture/decisions/2026-05-21-plan-b-parity-remediation.md` (also `/tmp/plan-b-remediation-2026-05-21.md`).

---

### F31 — No hardcoded user/machine paths in committed code

Detects literal `/Users/<dev>/` and `/home/<dev>/` patterns in tracked
files, with `/Users/runner/` and `/home/runner/` exempt (GitHub Actions
hosted-runner workspace).

#### Motivation

A path like `/Users/developer/Development/kairix/scripts/...` only
resolves on one human's laptop. The release session that introduced
this rule surfaced an instance of worktree-path leakage into a
subagent's report — the rule converts that ad-hoc smell into a
mechanical gate so the next leak gets caught at safe-commit, not at
cherry-pick time.

#### Mechanism

`scripts/checks/check_no_hardcoded_user_paths.py` walks every tracked
file (`git ls-files`) and scans for either pattern. Files in
`.architecture/baseline/`, `reference-library/`, `benchmark-results/`,
and any markdown documentation are exempt. The baseline at
`.architecture/baseline/no-hardcoded-user-paths-files.txt`
grandfathers pre-existing offenders (empty at landing — the repo is
clean today). Net-new violations block at safe-commit and CI.

#### Fix pattern

Replace the hardcoded path with one of:

- A relative resolution: `ROOT = Path(__file__).resolve().parents[N]`
- An environment variable: `os.environ.get("KAIRIX_DATA_DIR", "/opt/kairix/data")`
- A pytest fixture path scoped to the test: `tmp_path`

---

### F32 — No real first names or organisation/client names in fixtures + docs

The mechanical version of the `feedback_no_confidential_in_public_artefacts`
memory: public repo artefacts must use generic placeholders
(`agent-alpha`, `Acme`, `your-team`) rather than identifiers tied to a
specific human or client.

#### Motivation

kairix is a public, dogfooded knowledge-store project. Test fixtures,
BDD scenarios, reference-library corpora, and user-facing docs that
seed examples with a specific contributor's friends, family, or clients
leak that context into every public commit, issue thread, and release
note. Reviewer vigilance is not enough — the slip is locally plausible
("just a fixture name") and gets reproduced across new fixtures by
copy-paste. F32 converts the ad-hoc rule into a mechanical gate.

#### Mechanism

`scripts/checks/check_no_real_names_in_fixtures.py` walks every tracked
file (`git ls-files`) and scans the ones in scope:

- `tests/**/*.py` — pytest fixtures + assertions
- `tests/bdd/**/*.feature` — Gherkin scenarios
- `reference-library/**/*.{md,jsonl}` — corpus prose + transcripts
- `docs/**/*.md` — user-facing documentation

The `REAL_NAMES` tuple in the detector is the curated word-list. It
is intentionally narrow — only identifiers actually leaked into kairix
artefacts or explicitly flagged by the user for leak-prevention — to
avoid false-positives on common English names that legitimately appear
as citations in third-party reference-library content (e.g. "Dan North"
in BDD literature, "Daniel Kahneman" in behavioural-economics
references).

Generic placeholders that are explicitly NOT in `REAL_NAMES` (and so
pass trivially):

- Persons:   `agent-alpha`, `agent-beta`, `agent-gamma`, `agent-delta`, `agent-epsilon`, `Alice`, `Bob`, `Carol`
- Orgs:      `Acme`, `Example Corp`, `your-team`, `your-org`

The baseline at `.architecture/baseline/no-real-names-in-fixtures-files.txt`
grandfathers pre-existing offenders so the rule lands without forcing a
sweep. The baseline shrinks file-by-file as fixtures get migrated to
generic placeholders; net-new violations block at safe-commit and CI.

#### Fix pattern

Replace the real identifier with a generic placeholder:

- For person names: `agent-alpha` / `agent-beta` (kairix convention) or
  `Alice` / `Bob` / `Carol` (cryptography/CS canon).
- For organisations: `Acme` / `Example Corp` / `your-team` / `your-org`.

Then remove the file's entry from
`.architecture/baseline/no-real-names-in-fixtures-files.txt` in the
same commit.

**Pass**:

```python
record = FakeFactRecord(entity="agent-alpha", attribute="role", value="VP")
transcript = "agent-beta works at Acme."
```

**Forbidden**:

```python
record = FakeFactRecord(entity="<real-first-name>", attribute="role", value="VP")
transcript = "<real-full-name> works at <real-org>."
```

---

### F33 — `# shellcheck disable=<rule>` directives require rationale

Shell counterpart to F3 — every `# shellcheck disable=<rule>` line must
carry an inline rationale (after the directive on the same line) OR the
immediately preceding line must be a substantive `#` comment that
justifies the disable.

#### Motivation

A bare `# shellcheck disable=SC2034` is a silent override. Six months
later nobody can tell whether the disable is still load-bearing or
whether the underlying warning has become a real bug. F3 catches the
same shape for Python (`# noqa`, `# type: ignore`, `# nosec`,
`# NOSONAR`, `# pragma: no cover`); F33 closes the equivalent hole in
shell.

#### Mechanism

`scripts/checks/check_shellcheck_disable_with_reason.py` walks every
tracked file whose name ends in `.sh` OR whose first line is a `#!`
shebang naming `bash` / `sh`. For each line that matches
`# shellcheck disable=<rules>`, the detector accepts the disable if any
of these holds:

- The same line carries an inline `#`-comment rationale (e.g.
  `# shellcheck disable=SC2034  # exported via process substitution`).
- The immediately preceding non-blank line is a `#`-comment with a
  substantive body (≥ ~10 chars, not a shebang, not a copy of the
  directive).
- The rationale uses one of the canonical marker prefixes: `fix:`,
  `next:`, `run:`, `why:`, `rationale:`, `reason:`, `because:`.

Files in `.architecture/baseline/`, `reference-library/`, and
`benchmark-results/` are exempt. The detector and its test self-exempt
because their docstrings embed example disable lines. The baseline at
`.architecture/baseline/shellcheck-disable-with-reason-files.txt`
grandfathers pre-existing offenders (one file at landing:
`scripts/install/permissions-preflight.sh` carries two undocumented
`# shellcheck disable=SC1090` lines that pre-date the rule). Net-new
violations block at safe-commit and CI.

#### Fix pattern

Add an inline rationale after the directive, OR a substantive `#`
comment on the line above:

```bash
# safe -- sourced path is computed from a vetted config var
# shellcheck disable=SC1090
. "$SECRETS_FILE"

# shellcheck disable=SC2034  # exported via process substitution below
```

Forbidden:

```bash
# shellcheck disable=SC1090
. "$SECRETS_FILE"
```

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

### F45 — new top-level capability ships with a BDD feature

Forward-only rule landed in Wave 0. Every commit
that adds a new CLI subcommand (a new row in `kairix/cli.py:COMMANDS`),
MCP tool (a new `@server.tool()` decorated function in
`kairix/agents/mcp/server.py`), or plugin factory (`make_provider` /
`make_connector` / `make_extractor` symbol in a new
`kairix/providers/<name>/__init__.py`, `kairix/connectors/<name>/__init__.py`,
or `kairix/extractors/<name>/__init__.py`) must add a matching
`tests/bdd/features/*.feature` in the same commit.

Naming convention: `tests/bdd/features/{cli_<name>,mcp_<tool>,provider_<name>,connector_<name>,extractor_<name>}.feature`.
The check also accepts an explicit `# F45-feature: <path>` comment in the
surface file pointing at a non-conventionally-named feature.

Detector: `scripts/checks/check_f45_new_capability_bdd.py`.
Baseline: `.architecture/baseline/f45-files.txt` (empty; forward-only).

---

### F46 — BDD step impls call factory-composed production code

Wave 0 rule locking the **composition principle**
for BDD step files. Step implementations under `tests/bdd/steps/*.py`
must invoke (call-graph depth ≤ 2) one of:

- A CLI entry point: `kairix.cli.main` OR a per-subcommand `main(...)`
  function.
- An MCP tool function: the callable wrapped by `@server.tool()`.
- A factory constructor: `kairix.core.factory.build_search_pipeline`
  (and future `build_embed_pipeline` / `build_connector_pipeline` etc).

Direct construction of `SearchPipeline(...)`, `EmbedPipeline(...)`,
`ConnectorPipeline(...)`, `IngestPipeline(...)` in a step file is
disallowed.

Detector: `scripts/checks/check_f46_bdd_step_composition.py`.
Baseline: `.architecture/baseline/f46-files.txt` (seeded at landing;
shrinks via F49).

---

### F47 — integration tests build through the factory

Wave 0 rule locking the **composition principle**
for integration tests. Tests under `tests/integration/` that exercise a
multi-component pipeline must construct it via `kairix.core.factory.build_*`
with `paths=FakePaths(...)`. Direct construction of `*Pipeline(...)`
classes is allowed only in:

- `tests/contracts/` — Protocol shape proofs.
- `tests/integration/test_<x>_contract.py` — single-layer boundary proofs.

Detector: `scripts/checks/check_f47_integration_factory.py`.
Baseline: `.architecture/baseline/f47-integration-factory-files.txt`
(seeded at landing; shrinks via F49).

---

### F48 — composed production path E2E test exists and runs

Wave 0 rule locking the **real-path principle**.
`tests/e2e/test_composed_production_path.py` must exist, must carry
`@pytest.mark.e2e` on at least one test function, must be runnable as
`pytest -m e2e tests/e2e/test_composed_production_path.py`, and must
exercise: config load → `factory.build_*` → ingest → query → assertion.

Every new top-level capability (provider, connector, extractor, retrieval
mode) ships with a sibling `tests/e2e/test_composed_<capability>_path.py`.

Detector: `scripts/checks/check_f48_e2e_present.py`. CI Stage 4.5
(`e2e-composed-path` job in `.github/workflows/ci.yml`) runs the e2e
selector. No baseline — binary presence check.

---

### F49 — test-discipline baselines shrink per release

Wave 0 rule preventing test-debt accretion.
Each release tag (matching `v[0-9]*.[0-9]*.[0-9]*`) must reduce each of:

- `.architecture/baseline/f30-operator-outcome-tests-files.txt`
- `.architecture/baseline/f46-files.txt`
- `.architecture/baseline/f47-integration-factory-files.txt`

by ≥1 entry compared to the previous tagged release, OR keep all three
at zero. F30 reached zero in Wave 0.

Detector: `scripts/checks/check_baseline_shrinking.py`. Wired into
`.github/workflows/release.yml` BEFORE tag creation. No per-commit gate.

Canonical reference for F45–F49 mechanics + paydown patterns:
[`test-discipline-hardening.md`](test-discipline-hardening.md).

---

### F34–F44 — connector framework discipline pre-arm

Landed in connector-framework Wave 0 (2026-05-22
hardening + this repo's `docs/architecture/connector-ingestion-architecture.md`
spec). Pre-arms the discipline before Wave 1 creates `kairix/connectors/`
and `kairix/extractors/` surfaces — all eleven checks pass vacuously
today (except F41 and F43 which carry seeded baselines on the existing
provider plugins) and fire mechanically the moment Wave 1 lands a
non-conforming change.

| Rule | Locks | Mirrors |
|---|---|---|
| **F34** | `kairix/core/connectors/**` cannot import `kairix/connectors/**` or `kairix/extractors/**` | F26 |
| **F35** | `kairix/connectors/<a>/**` cannot import another connector or any extractor | F27 |
| **F36** | Every plugin under `kairix/{connectors,extractors}/<name>/` has matching BDD coverage | F28 |
| **F37** | Change-detection / sync code only under `kairix/connectors/<name>/` or `kairix/core/connectors/` | F29 |
| **F38** | Silver processing (chunking + entity-signal extraction) only in `kairix/core/connectors/silver.py` | (new — singular Silver surface) |
| **F39** | Every `Chunk(...)` write passes `source_uri`, `source_modified_at`, `sensitivity` explicitly | F15 (boundary at write surface) |
| **F40** | Every extractor plugin declares `version: str = "..."` written through to `documents_media.extractor_version` | (new — re-extract tractability) |
| **F41** | Every plugin (`kairix/{connectors,extractors,providers}/<name>/`) has `py.typed` + rationalised `# type: ignore` | (new — strictness at plugin boundary) |
| **F42** | Connector-surface Protocol returns are frozen dataclasses or tuples of them; never `dict`/`list[dict]` | (new — typed boundary) |
| **F43** | Every plugin has `tests/contracts/test_<plugin>_protocol.py` exercising canonical fake + real impl | F30 (contract version) |
| **F44** | Engagement-scope code (every dir under `kairix/`) cannot import firm-scope storage clients (`psycopg`/`asyncpg`/etc) | (new — locks pro ADR-017 two-scope boundary) |

Canonical specs + Bronze/Silver context + plugin discovery shape live in
[`connector-ingestion-architecture.md`](connector-ingestion-architecture.md).

---

### F51–F54 — feature flag lifecycle discipline

Landed alongside `kairix/core/features/` (Feature-flag PR-3 per
`feature-flag-architecture.md` §9). Locks the cutover pattern that
gates IM-6 + Wave 5+ connector enablement, plus every future ranker /
chunker / schema-version swap.

| Rule | Locks | Mechanism |
|---|---|---|
| **F51** | Every flag in `kairix/core/features/registry.py:REGISTRY` has a `target_retire_in` ≤ current `setuptools-scm` version + 6 months | `scripts/checks/check_f51_flag_retirement.py`; fires past deadline unless registry entry carries a `# retire-extension: <reason>` rationale comment |
| **F52** | Every `flag("<name>")` call site references a name that exists in REGISTRY | `scripts/checks/check_f52_flag_call_sites.py`; AST scan over `kairix/**/*.py` |
| **F53** | `kairix features status` CLI subcommand AND `tool_features_status` MCP tool both exist with F30-compliant outcome tests | `scripts/checks/check_f53_features_status_surface.py` |
| **F54** | Every flag has BDD scenarios for OFF + ON branches, integration tests exercising both branches, and an E2E composed-path test for top-level capability flags | `scripts/checks/check_f54_flag_both_branch_tested.py`; mechanically prevents rollback-becomes-fiction failure mode |

Canonical reference for the pattern + cutover protocol + wave plan
integration: [`feature-flag-architecture.md`](feature-flag-architecture.md).

---

### F55 / F57 / F58 / F61 — connector / collection / scope topology

Landed in Wave A of the connector / collection / scope topology ADR
(see [`connector-scope-topology/ADR.md`](connector-scope-topology/ADR.md))
to arm the gate **before** Wave C runtime code grows into the gap. All
four are vacuous-green or carry a single grandfathered entry today;
Waves C–F shrink baselines to zero as the production code lands. Per
the gap analysis Table B + `10-test-architecture.md` §"New F-rules
required".

| Rule | Locks | Mechanism |
|---|---|---|
| **F55** | Every `Chunker` plugin under `kairix/chunkers/<name>/` declares a module-level `version: str`; every `Chunk(...)` constructor call passes `chunker_version=` (mirrors F40 for extractors). Without the version surfaced at the write site, re-chunk sweeps become whole-corpus rebuilds. | `scripts/checks/check_f55_chunker_version.py`; AST walk over `kairix/**/*.py`. Baseline: `.architecture/baseline/f55-files.txt` (today: `kairix/core/connectors/silver.py` grandfathered until Wave C threads `ChunkerRegistry` through Silver) |
| **F57** | Every SQL `UPDATE topology_cc_pairs ... SET status = ?` lives in a module that also declares a top-level `_ALLOWED_TRANSITIONS: dict[CCPairStatus, frozenset[CCPairStatus]]` dispatch dict. Ad-hoc updates bypass the state-machine; ADR v2 §3 defines the only legal transitions (`SCHEDULED → INITIAL_INDEXING → ACTIVE ↔ PAUSED / DELETING / INVALID`). | `scripts/checks/check_f57_ccpair_lifecycle_integrity.py`; string-literal scan + module-level AST attribute check. Baseline: `.architecture/baseline/f57-files.txt` (empty; vacuous-green pre-Wave-C) |
| **F58** | When a `HierarchyConnector` class exists in production code, at least one test under `tests/contracts/` must have a function name matching `test_*hierarchy*parent_before_child*` AND reference `HierarchyConnector`. Every `HierarchyNode` emission must have `raw_parent_id` either None (root) or referencing a previously-emitted node within the same `iter_containers()` call. | `scripts/checks/check_f58_hierarchy_parent_before_child.py`; test-collecting gate (mandatory only once the Protocol exists). Baseline: `.architecture/baseline/f58-files.txt` (empty; vacuous-green pre-Wave-E) |
| **F61** | Bare `_SqliteChunkWriter(db, collection=...)` construction lives only under `kairix/core/connectors/` (the framework owns the writer). Everywhere else flows through `CollectionRouter`. Extends F38 with the per-collection routing layer. | `scripts/checks/check_f61_collection_router_singleton.py`; AST scan. Baseline: `.architecture/baseline/f61-files.txt` (today: `kairix/worker.py` grandfathered until Wave C rewires `_run_one_connector_batch` through `CollectionRouter`) |

Canonical reference for the wave plan + Protocol roster + capability
mix-ins these rules protect: [`connector-scope-topology/ADR.md`](connector-scope-topology/ADR.md).

---

### F50 — net-new files cannot accrete F-rule baseline debt

Closes the per-file-shrink-only loophole identified by the 2026-05-22
cross-repo audit. Every F-rule baseline under
`.architecture/baseline/*-files.txt` is per-file shrink-only — pre-existing
violators are grandfathered until F49 forces them out at release time.
The loophole: a brand-new file under `kairix/**` (or `tests/**`) can
land with arbitrary violations because the baseline doesn't yet know it
exists, so the shrink-diff sees nothing.

F50 closes that. Net-new files (added in the staged diff at commit-time,
or added since the previous release tag at CI-time) may not appear in
any per-file F-rule baseline. Pre-existing entries are unaffected —
this rule only blocks fresh accretion.

#### Mechanism

`scripts/checks/check_f50_net_new_file_violations.py` runs in two modes:

- **Staged mode** (default; pre-commit hook + `safe-commit.sh`):
  `git diff --cached --name-only --diff-filter=A` returns the set of
  files added in the current staged commit. The check asserts none
  appear in any baseline.
- **Full-tree mode** (CI Stage 0): `git diff --name-only --diff-filter=A
  <prev-tag>..HEAD` returns every file added since the last release
  tag. Catches the case where pre-commit was skipped locally and the
  violation only surfaces in CI for a release PR.

For every match, the failure text names the violating baseline file
and the offending added paths, plus the F21 `fix:`/`next:`/`run:`
trailer pointing at the canonical paydown patterns.

#### Why it isn't redundant with F49

F49 enforces *paydown rate* on the existing baselines (each release
shrinks; never grows). It doesn't catch the case where a baseline
grows because a fresh file was added to it. F50 covers exactly that
case at commit time, before the baseline edit ever lands.

A clean interpretation: F49 says "baselines shrink"; F50 says "baselines
shrink, and they never grow by accretion via new files." Together they
mean: the only legal motion is downward.

#### Cross-repo provenance

Imported from a sibling repo's `net_new_file_finding_cap.py` pattern
(2026-05-22 audit). The original protected against SonarCloud findings
on new files; this variant generalises to *any* per-file F-rule baseline.

---

### F81 / F82 / F83 — fresh-install smoke + per-commit flake/gate-runner discipline

Landed via [EPIC #499](https://github.com/three-cubes/kairix/issues/499)
Phase 0 (registry integrity + first two new rules). F81 was shipped in
onboarding tranche 3 (2026-06-11) before it had a catalogue entry — the
registry would have handed the number out twice; Phase 0 registers it
properly. F82/F83 mechanise the two highest-burn escapes from the
2026-06-11 review: the #493 wall-clock flake family and the #483
silent-gate-death class.

| Rule | Locks | Mechanism |
|---|---|---|
| **F81** | A stranger's fresh install boots: clean temp dir → `docker compose up` from the shipped compose + `.env.example` → `/healthz/ready` 200 → MCP initialize + tools/list handshake → `GET /setup/` 200 with the wizard flag ON → BM25 search hit on a seeded sample doc. | The smoke itself is `scripts/checks/check-fresh-install-smoke.sh`, run by `.github/workflows/fresh-install-smoke.yml` (needs Docker + minutes — not per-commit). The per-commit leg `scripts/checks/check_f81_fresh_install_smoke.py` proves the smoke can't rot out of the pipeline: script exists, workflow exists, workflow invokes the script. No baseline. |
| **F82** | No wall-clock ceiling assertions (`assert elapsed < 0.150`, `assert time.monotonic() - t0 <= 2`) in per-commit test tiers — timing measures host scheduling, not kairix behaviour (#493 family burned three gate cycles in one day). Tests carrying a `slow` / `soak` / `load` / `pvt` marker (F8's resolution mechanism) or a `# F82-allowed: <why>` line rationale are exempt. Floors (`assert elapsed >= window`) and variable budgets are deliberately not flagged — precision over recall. | `scripts/checks/check_f82_wall_clock_ceilings.py`; AST walk over `tests/**/*.py` tracking clock-call assignments + elapsed-named values. Baseline: `.architecture/baseline/f82-files.txt` (29 pre-existing files grandfathered). Pre-commit hook `arch-f82-wall-clock-ceilings`. |
| **F83** | Shell gate scripts (`scripts/*.sh`, `scripts/checks/*.sh`) follow the post-#483 gate-runner contract: (a) no unguarded `VAR=$(...)` capture under `set -e` (the silent-death class — the script dies AT the assignment with no FAIL line); (b) `\|\| true` carries a trailing rationale comment; (c) shellcheck-clean at error severity; (d) `safe-commit.sh` stages announced with `echo -n "  <stage>... "` emit both `OK${NC}` and `FAIL${NC}`/`gate_died` verdicts, and every `run-all.sh` check invocation carries `\|\| overall=1`; (e) output probes under `pipefail` use `grep -q PATTERN <<< "$OUTPUT"`, avoiding false failures when an early-exiting quiet grep gives the producer SIGPIPE. | `scripts/checks/check_f83_gate_runner_contract.py`; logical-line shell scan + batched shellcheck. Baseline: `.architecture/baseline/f83-files.txt` (7 pre-existing scripts grandfathered; a listed file masks all sub-rules until paid down). Pre-commit hook `arch-f83-gate-runner-contract`. |

Note on F49 (same Phase 0): the shrink-gate's hand-listed baseline
paths had drifted (`F46-files.txt` / `F47-files.txt` never matched the
git-tracked `f46-files.txt` / `f47-integration-factory-files.txt`), so
two of its three legs were vacuous on the Linux release runner.
`check_baseline_shrinking.py` now derives the governed paths from the
rule catalogue (`_rule_catalogue.py` gate names), making a future
rename a loud `KeyError` instead of a silent always-pass.

---

### F84 — config write/read round-trip

Landed via [EPIC #499](https://github.com/three-cubes/kairix/issues/499)
Phase 1. The caught class is the #492 overlay split-brain (H1): the
setup wizard wrote `topology` through the overlay writer
(`write_config_updates`) while the worker read its config through a
different, non-overlay resolver — the flagship feature was silently
inert in Docker with every suite green. A composed test that writes
through the production writer and reads the value back through the
canonical layered reader would have failed immediately.

| Rule | Locks | Mechanism |
|---|---|---|
| **F84** | Every production config-write site — a public `def` in `kairix/**` whose name compounds a write verb (`write`/`update`/`save`/`persist`) with `config` (`write_config_updates`, `update_config_file`, `write_config_yaml`), or any def with that naming convention containing a stream-form `yaml.dump`/`yaml.safe_dump` — has a composed write→read round-trip test. Coverage convention: a test module references BOTH the writer name AND a canonical-reader name (`load_merged_mapping` / `load_config` / `load_top_level_config` / `feature_flag_config_overlay`); OR carries a `# F84-round-trip: <writer>` registry tag (for tests driving the writer through a CLI/web surface); coverage propagates from a covered writer to the writers its body calls (one round-trip proves the delegation chain). `# F84-allowed: <why>` on the def line exempts writer-named functions that don't write operator config. Deliberately NOT caught (precision over recall): config writes in non-writer-named functions, arbitrary callers of the writer family, same-test-function pairing of write and read, non-YAML config writes. | `scripts/checks/check_f84_config_round_trip.py`; AST harvest of writer defs over `kairix/**` + referenced-name scan over `tests/**` + delegation fixed-point. Baseline: `.architecture/baseline/f84-files.txt` (empty at landing — the #492 fix's exemplar `tests/integration/test_wizard_config_overlay_split_brain.py` covers the whole tree). Pre-commit hook `arch-f84-config-round-trip`. |

### F85 — cross-tier contract vocabularies single-sourced

Landed via [EPIC #499](https://github.com/three-cubes/kairix/issues/499)
Phase 1. The caught class is session escape 8: the source-sign-in
`phase` strings (`idle` … `failed`) were encoded three times — in the
backend, in the wizard routes, and as raw strings in the
`source_auth_status.html` template — so a single rename broke the
HX-Redirect choreography while every suite stayed green. The M11 fix
single-sourced that vocabulary (and the azure provider-name grouping)
into `kairix/platform/setup/service.py`: the backend imports the
`PHASE_*` symbols, the routes layer republishes them through
`env.globals`, and the template branches on the rendered symbol
(`{% if status.phase == PHASE_CONSENT %}`), never the raw string. F85
makes that end-state structural and prevents the next vocabulary from
regressing.

| Rule | Locks | Mechanism |
|---|---|---|
| **F85** | Each registered cross-tier contract vocabulary has exactly one owning module; a member literal re-declared in another tier fails. The DECLARED registry lives inside the check — `VOCABULARIES = {name: (owning_module, (member, ...))}` — seeded with the source-auth `PHASE_*` strings and the azure provider-name set, both owned by `kairix/platform/setup/service.py`. A violation is a member appearing in a *vocabulary-definition shape* — a const-assignment RHS, or an element of a set/frozenset/tuple/list/dict-key literal — in a non-owning setup-tier module, OR a raw member string quoted in a setup-tier template (the contract is to branch on the `env.globals` symbol). Imports from the owning module are the desired pattern and never flagged; `# F85-allowed: <why>` exempts a line. Deliberately NOT caught (precision over recall): incidental uses that are not vocabulary definitions (an OAuth `prompt=consent` dict *value*, a `getattr(obj, "failed")` attribute name); members outside the setup tier (provider plugins legitimately own `PROVIDER_NAME = "azure_foundry"`; phase words appear as English prose repo-wide); auto-discovery of un-registered shared constants; substrings of running prose. | `scripts/checks/check_f85_contract_vocabulary_singularity.py`; AST walk over `kairix/platform/setup/**/*.py` (definition-shape constants) + raw-literal scan over `kairix/platform/setup/web/templates/**/*.html`. Baseline: `.architecture/baseline/f85-files.txt` (2 pre-existing files — `backends.py` + `wizard.py` mirror the azure grouping instead of importing `AZURE_PROVIDER_NAMES`; the phase vocabulary is already single-sourced). Dispatched by the catalogue-driven runner (`arch-fitness-catalogue` pre-commit hook). |

---

## Harness architecture

### Shared engine — kairix consumes `tc_fitness` (EPIC #499)

The runner machinery is no longer local to kairix. As of [EPIC #499](https://github.com/three-cubes/kairix/issues/499)
(common-process convergence), kairix is a **pure consumer** of the shared
[`three-cubes-fitness`](https://github.com/three-cubes/tc-fitness) package
(`tc_fitness`, pinned in `pyproject.toml`). The split is **shared
machinery, per-repo domain**:

| Concern | Lives where |
|---|---|
| In-process + subprocess dispatch, the named verdict ledger, the `--all` / `--gate` / `--staged` modes, parse-once `CheckContext`, staged-selection logic, the ratcheting `gate()` primitive, `python_files`/`repo_relative`, the `RuleEntry` schema | **shared** — the installed `tc_fitness` package |
| The F-numbered catalogue rows, every `check_*.{py,sh}` implementation, the `.architecture/baseline/` files, and the domain config kairix feeds the engine's declarative factories | **kairix** — `scripts/checks/` + `.architecture/baseline/` |

`scripts/checks/run_checks.py` is the consumer shim: it dispatches through
`tc_fitness.runner` (the shared `run` / `main_cli` engine). From v0.4.x the
engine absorbs the four legacy seams as **declarative factories** fed kairix's
domain config — `run_checks.py` no longer hand-writes them. `scope_resolver` is
built by `tc_fitness.staged.make_module_roots_resolver`; `enumeration_narrower`
by `tc_fitness.staged.make_binding_narrower`; `conditional_check` (F7/F9
coverage-XML gating) by `tc_fitness.runner.make_env_path_conditional_check`; and
`--skip-coverage` is threaded through the engine's `main_cli` via `extra_flags` +
`post_parse` (no forked argparse). kairix supplies only the domain config (its
module roots, the extra enumeration method, the `KAIRIX_COVERAGE_XML` env path);
the engine owns the machinery. Behaviour stays **byte-identical** to the
pre-migration local runner. `_rule_catalogue.py`
imports `RuleEntry` from `tc_fitness.catalogue` and keeps kairix's own rows +
closed `Category`/`Scope`/`Status` `Literal`s. The schema is **id-agnostic** —
kairix uses F-numbers; a sibling repo (also migrated onto the
shared runner) uses descriptive names; both dispatch through the same engine.
The local `_check_context.py` / `_staged_selection.py` / `_arch_lib.py` modules
were **deleted** — their `CheckContext`, staged-selection, and `gate()` /
`python_files()` / `repo_relative()` surfaces now come from `tc_fitness.context`
/ `tc_fitness.staged` / `tc_fitness`. The migration was verified byte-identical
(same verdicts + ledger text). The runner evolves via shared learning across
consuming repos — the shared layer is `tc_fitness` (lib + ratchet + catalogue
schema + context + staged + runner) plus `three-cubes/tc-pipelines` (the
`setup-uv-cached` composite + `python-quality-gate.yml` reusable workflow). The
EPIC #499 convergence narrative is captured inline in the F81–F85 rule sections
above. Both consuming repos (kairix and the sibling repo) are pure
consumers of the shared engine via these declarative factories — there are no
remaining local injection seams. The schema stays **id-agnostic**: kairix uses
F-numbers, the sibling uses descriptive names, both dispatch through the same
`main_cli`. **Forward backlog:** reusable mutation / Sonar / Docker workflows
converge next.

### File layout

```
scripts/checks/
├── run_checks.py                         # Pure consumer of tc_fitness.runner (catalogue dispatch + declarative-factory domain config)
├── _rule_catalogue.py                    # kairix's F-numbered RuleEntry rows (RuleEntry schema from tc_fitness.catalogue)
├── _fitness_rule.py                      # FitnessRule ABC — 3-line check subclasses over tc_fitness.gate()
├── generate_catalogue_docs.py            # Regenerates the F-CATALOGUE doc regions (F92 currency gate)
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

**The Python helpers now come from the shared `tc_fitness` package** (the
local `_arch_lib.py` was deleted in EPIC #499 — see "Shared engine" above).
Python checks `from tc_fitness import gate, python_files, repo_relative`:
- `gate(name, current_set, remediation_str) -> int` — same baseline-ratchet
  semantics as the shell helper, for Python checks.
- `python_files(*roots)` — yields all `.py` files under given roots,
  skipping `__pycache__`.
- `repo_relative(path)` — converts an absolute path to repo-relative.

`scripts/checks/_fitness_rule.py` is kairix's local convenience layer over
those primitives: a `FitnessRule` ABC that lets a check declare itself as a
~3-line subclass (`name`, `remediation`, `roots`, `file_has_violation`) with
baseline-load / enumerate / scope / gate inherited from `tc_fitness.gate`.

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
    - name: Architecture fitness functions (tc-fitness run — full catalogue)
      run: uv run tc-fitness run
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
    - e2e-composed-path
    - union-coverage
    - security
    - docker
```

A failing `arch-fitness` job sets `needs.arch-fitness.result` to
`failure`. The gate's `for result in $RESULT_*; do ...` loop fails the
gate. Branch protection rejects the merge.

### Branch protection

The `main` ruleset requires **both** status checks — **`CI gate`** (the `check`
fan-in in `ci.yml`) and **`PR compliance check`** (`integration.yml`) — plus a
**code-owner review** on any diff that touches a control-plane path in
[`.github/CODEOWNERS`](../../.github/CODEOWNERS). It has **zero bypass actors**
(no `--admin` rescue). A green gate **auto-merges the PR** (`auto-merge.yml` arms
`gh pr merge --auto` as the App); control-plane PRs hold for a human. No
additional configuration is needed for fitness functions — they're transitively
enforced via `CI gate`. Shared canon:
[tc-pipelines `governance/STANDARDS.md`](https://github.com/three-cubes/tc-pipelines/blob/main/governance/STANDARDS.md).

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

**Decide where the rule belongs first.** A **kairix-domain** rule — one that
encodes a kairix-specific invariant (a package boundary, a repo path convention,
a retrieval-layer contract) — stays **local**, added as an F-numbered row via the
playbook below. A **generically-useful** gate — one any Golden-Path repo would
want — belongs as a **CORE check in the shared `tc-fitness` engine**, not forked
here: author it there (`src/tc_fitness/core_checks/<name>` + a contract/unit test),
release an additive immutable tag, then repin `three-cubes-fitness` in
`pyproject.toml` and bind it via `[tool.tc_fitness.core_checks.<name>]`. Never fork
a parallel gate in the consumer. See
[how-to-improve-a-fitness-gate-or-pipeline](../development/how-to-improve-a-fitness-gate-or-pipeline.md).

For a kairix-domain rule the runner is catalogue-driven and shared (see "Shared
engine" above): a new rule is **one `RuleEntry` row + one check + one
baseline**, not five hand-edited files. The playbook:

1. **Decide the rule shape.** Write a one-sentence statement
   ("MUST NOT…") and a one-paragraph "why."
2. **Choose the detection mechanism.** Line-pattern → shell + grep.
   Structural → Python AST. Coverage / report → Python + parser.
3. **Implement the check** under `scripts/checks/`. Follow the
   existing scripts as templates. Shell checks use `arch_gate` (from
   `_lib.sh`); Python checks `from tc_fitness import gate, python_files,
   repo_relative` — or subclass the local `FitnessRule` ABC in
   `_fitness_rule.py` for the 3-line shape. (`gate`/`python_files`/
   `repo_relative` come from the shared `tc_fitness` package now, not the
   deleted local `_arch_lib.py`.)
4. **Seed the baseline.** Run the check; pipe its violation list
   to `.architecture/baseline/<rule-name>-files.txt`. This makes the
   current state pass.
5. **Sabotage-test.** Plant a fake violation in a new file; confirm
   the check fails with the expected message; remove and re-run for
   clean exit.
6. **Add the catalogue row.** Append a `RuleEntry` to
   `scripts/checks/_rule_catalogue.py` (id, category, scope, status,
   summary, check module, baseline). The catalogue is the single source
   of truth — the shared `run_checks.py` runner dispatches every row, and
   F92 fails the build if a `check_*` has no row (or a row no check).
7. **Wire into pre-commit.** Add an entry to `.pre-commit-config.yaml`
   under the `Architecture fitness functions` section (catalogue-dispatched
   rules ride the `arch-fitness-catalogue` hook; standalone checks get
   their own hook).
8. **Wire into safe-commit / CI.** No explicit step for catalogue rules —
   `run_checks.py --all` (invoked by `run-all.sh` in safe-commit + CI
   Stage 0) picks the row up. Add a separate CI step only if the check has
   special dependencies (like F7 needing `coverage.xml`). Verify with
   `bash scripts/safe-commit.sh "test"` against a no-op staged change.
9. **Document in this file.** Add a section to "The rules in detail"
   following the existing template (statement, why, detection,
   examples, fix pattern). Then regenerate the machine-derived catalogue
   table — `python3 scripts/checks/generate_catalogue_docs.py` — never
   hand-edit the `<!-- F-CATALOGUE -->` regions; F92 fails on drift.
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

- **Un-exercised reusable-workflow callers.** When CI uses
  change-detection path filters to gate a `uses:` reusable-workflow
  (`workflow_call`) job ON only for relevant changes, a workflow-only PR
  can land with that job gated OFF — so a broken caller↔reusable-workflow
  input contract reaches `main` and fails CI at *startup* on the next
  triggering change. F93 gates that the job is in the aggregator's
  `needs:` closure; it does not model the path filter, so it can't tell
  that the caller never actually ran on this PR. Mechanically detecting
  this would require modelling each job's path filter against the diff AND
  the reusable-workflow input contract — fragile and not worth the cost.
  The discipline is review-time: when a PR edits a `uses:`
  reusable-workflow caller or its inputs, force a triggering change in the
  SAME PR (e.g. a no-op source touch matching the job's path filter) so
  the caller actually runs before merge. See
  [`docs/development/how-to-consume-a-shared-reusable-workflow.md`](../development/how-to-consume-a-shared-reusable-workflow.md).

- **Probe/scratch files written outside `tmp_path`.** A test that writes
  scratch or probe files into the live source tree (instead of pytest's
  `tmp_path`) can leave orphaned files that whole-tree scanners —
  fitness-function detectors, coverage walks — later pick up, producing
  flaky failures on unrelated runs. Statically distinguishing a write that
  targets the source tree from one that targets `tmp_path` needs data-flow
  analysis that isn't worth the cost; this is review-time discipline (see
  CONSTRAINTS.md > Testing patterns). The complementary mechanical lever
  is on the detector side: narrow whole-tree scans to the staged set so
  stray in-tree debris can't contaminate an unrelated run.

---

## Cross-references

- **[tc-pipelines `governance/STANDARDS.md`](https://github.com/three-cubes/tc-pipelines/blob/main/governance/STANDARDS.md)** — the canonical engineering-standards index the harness references (`harness_canon_reference` gate).
- **[`three-cubes-fitness` (`tc-fitness`)](https://github.com/three-cubes/tc-fitness)** — the shared gate engine kairix consumes; CORE checks live here, not forked locally.
- **[`how-to-improve-a-fitness-gate-or-pipeline.md`](../development/how-to-improve-a-fitness-gate-or-pipeline.md)** — converge a gate/pipeline change UP into `tc-fitness` / `tc-pipelines`.
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
