# Feature flag architecture — cutover pattern with mechanical retirement

> **Status**: Implemented. Feature flags are a first-class architectural component for every cutover that swaps production behaviour — connector swaps, ranker swaps, schema migrations, ingest-pipeline changes. Recurring need; one-off flags per cutover is the antipattern.
>
> **Shipped surface (this spec is the canonical reference for it):**
> - `kairix/core/features/` — `registry.py` (the populated `REGISTRY` + `FeatureFlag` value object), `resolver.py` (env → config → default resolution), `observability.py` (first-activation logging), `cli.py` (`kairix features status`), `__init__.py` (the `flag(name)` surface). Plus `capability.py` and `topology_status.py` for the diagnostics variants.
> - `tool_features_status` MCP tool in `kairix/agents/mcp/server.py`.
> - Fitness functions **F51** (retirement deadline), **F52** (call-site reference integrity), **F53** (operator surface required), and **F54** (both-branch tested) are all live in `scripts/checks/` and enforced per-commit + in CI Stage 0 over the full tree, with no baseline files.
> - Cutover tooling: `scripts/cutover/capture_baseline.py` + `scripts/cutover/diff_baseline.py`.
>
> Companion to: `connector-ingestion-architecture.md` (the connector waves use this pattern), `test-discipline-hardening.md` (the both-branch-tested requirement extends the F46/F47/F48 principles).

## 1. Why this exists

Every wave in this codebase eventually hits a moment where new production behaviour replaces old. The original trigger was IM-6 (Obsidian connector replaces `DocumentScanner`); the connector waves (M365, Dex, SharePoint, and the rest), vision-enhanced extraction, and every ranker / chunker / schema-version change face the same structural question: *how do we cut over without breaking what's working today?*

Hand-rolling a one-off boolean per cutover ("`obsidian_connector_primary: true` in `kairix.config.yaml`") buys the immediate deploy but creates the predictable failure modes:

- The flag becomes permanent scaffolding because nobody owns retirement
- Both branches of the flag aren't tested (the OFF branch keeps working because nothing changed; the ON branch is tested only at deploy time)
- The flag's name, default, owner, and retirement date drift across PRs and chat threads instead of being captured anywhere mechanical
- Future contributors can't see what flags exist without grepping the codebase for `config.get`-style calls

Feature flags as a first-class architectural component close all four gaps mechanically. The pattern is small, observable, and self-retiring.

## 2. Three principles

### 2.1 Default-safe

Every flag defaults to the *safe* value — the behaviour that's already validated in production. The new behaviour is opt-in until the cutover protocol (§4) signs off. A merge of new flag-gated code is structurally equivalent to a no-op in terms of operator behaviour; the cutover is a separate, deliberate action.

### 2.2 Both-branch tested

Every flag has tests for *both* branches. The OFF branch (legacy behaviour) and ON branch (new behaviour) each have:

- A BDD scenario in the feature file
- An integration test that exercises real composition
- For top-level capability flags, an E2E test in `tests/e2e/`

This is mechanically enforced by **F54**. Without it, a flag becomes a one-way door — the OFF branch silently rots because nobody exercises it after introduction, so rollback becomes a fiction.

### 2.3 Mechanical retirement

Every flag has a `target_retire_in: str` version. Past that deadline, **F51** fires at pre-commit and CI Stage 0. The flag must either be retired (deleted from the registry, old code path removed) OR have its `target_retire_in` explicitly extended with a rationale comment. This stops "flag becomes permanent fixture" — the well-documented worst outcome of feature-flag systems.

## 3. Architecture

### 3.1 Layout

```
kairix/
  core/
    features/
      __init__.py         # public surface: flag(name) → bool
      registry.py         # FeatureFlag dataclass + REGISTRY dict
      resolver.py         # config-yaml + env-var → bool resolution; cached per-process
      observability.py    # first-activation logging; metrics emit; status-envelope shape
      cli.py              # `kairix features status` / `kairix features list`
  core/protocols.py       # adds FeatureFlagResolver Protocol so tests inject fakes

tests/
  bdd/features/
    feature_flag_<name>.feature   # one per flag in registry; two scenarios minimum
  bdd/steps/
    feature_flag_<name>_steps.py
  integration/
    test_feature_flag_<name>.py   # both branches exercised
  e2e/
    test_composed_<flag>_path.py  # for flags that gate top-level capabilities

scripts/checks/
  check_f51_flag_retirement.py
  check_f52_flag_call_sites.py
  check_f53_features_status_surface.py
  check_f54_flag_both_branch_tested.py
```

### 3.2 The `FeatureFlag` value object

```python
# kairix/core/features/registry.py
@dataclass(frozen=True)
class FeatureFlag:
    """One feature flag declaration. Frozen; the registry IS the schema."""

    name: str                          # snake_case; e.g. "obsidian_connector_primary"
    default: bool                      # the safe value (almost always False at introduce stage)
    description: str                   # one-line, operator-facing
    stage: Literal["introduce", "cutover", "retire"]
    introduced_in: str                 # version, e.g. "v2026.5.23"
    target_retire_in: str              # version; F51 fires past this
    owner: str                         # team/squad; "connector-framework" or similar
    related_spec: str | None = None    # path to spec doc or tracking issue
```

The registry is a single Python dict in `registry.py`. Example entry shape (the live registry holds the connector / search-pipeline / onboarding flags — read `kairix/core/features/registry.py` for the current set):

```python
REGISTRY: dict[str, FeatureFlag] = {
    "connector_dex_crm": FeatureFlag(
        name="connector_dex_crm",
        default=False,
        description="Enable the Dex CRM connector — pulls Person/Org entity signals from the Dex API into the entity_signals staging table.",
        stage="introduce",
        introduced_in="v2026.5.23",
        target_retire_in="v2026.7.23",  # F51 enforces the SCM+6mo ceiling
        owner="connector-framework",
        related_spec="docs/architecture/connector-ingestion-architecture.md",
    ),
    # ... the rest of the live registry
}
```

> The original running example in this spec — `obsidian_connector_primary`, plus the `topology_*` family — has since completed its lifecycle and been **retired** post-cutover (the gated call sites were inlined to the post-cutover behaviour and the OFF-branch shims removed). It survives in this doc only as an illustration of the introduce → cutover → retire arc; do not expect to find it in `REGISTRY`.

### 3.3 Consumer surface — `flag(name)`

Single call surface; the registry is the schema:

```python
# Inside any kairix module that needs to branch on a flag
from kairix.core.features import flag

if flag("obsidian_connector_primary"):
    return _run_via_connector_pipeline(...)
return _run_via_legacy_scanner(...)
```

Behaviour:

1. First call per process: resolver consults env var → config-yaml → registry default. Result is cached for process lifetime.
2. Logs activation at INFO (once per flag per process; not on every call).
3. Emits a counter via the existing search-logger telemetry hook so the `probe-config` health envelope reports which flags are active.
4. `FeatureFlagResolver` Protocol exposes the seam tests use; `FakeFeatureFlagResolver` from `tests/fakes.py` lets unit tests pin specific flag states without touching the global registry.

### 3.4 Resolution order

Matches the existing config-layering pattern from `feedback_deployed_config_path`:

1. `KAIRIX_FEATURE_<UPPERCASE_NAME>=1|0` env var (highest priority; tests, debug runs)
2. `kairix.config.yaml` `features:` section (operator overlay; the canonical production path)
3. `FeatureFlag.default` from the registry (the safe fallback)

Operator config example:

```yaml
# kairix.config.yaml
features:
  connector_dex_crm: true            # production instance opts in for UAT
  connector_m365_calendar: false     # not yet wired
```

### 3.5 Operator surface

Two CLI commands + one MCP tool, all exercising the same status envelope so the operator never has to learn two shapes:

```bash
$ kairix features status
NAME                              DEFAULT  EFFECTIVE  STAGE       RETIRE-BY
connector_dex_crm                 false    true       introduce   v2026.7.23
connector_m365_email_headers      false    false      introduce   v2026.7.23
connector_m365_calendar           false    false      introduce   v2026.7.23

$ kairix features status --json
{
  "flags": [
    {"name": "connector_dex_crm", "default": false, "effective": true, "stage": "introduce", ...},
    ...
  ]
}
```

The MCP tool `tool_features_status` returns the same JSON envelope so agents can self-introspect what's enabled.

Per F30, both surfaces have outcome tests that assert on envelope content (not exit code alone).

## 4. Lifecycle stages + cutover protocol

### 4.1 The three stages

| Stage | Default | Operator can override | Code |
|---|---|---|---|
| **introduce** | `False` | yes (opts in for UAT) | Both paths present; new path runs only when flag enabled |
| **cutover** | `True` | yes (opts out as escape hatch) | Both paths present; old path is the rollback affordance |
| **retire** | — | — | Flag removed from registry; old path deleted; commit references the flag |

Transitions are PRs, not config changes. Moving from introduce → cutover changes `default=True` and `stage="cutover"` in the registry, ships through CI, lands on main. Same for cutover → retire (which also deletes the legacy code path and the flag entry).

### 4.2 Cutover protocol — the eval-and-monitor pattern

Every flag flip from `introduce` → effective-on (whether via operator overlay or registry default change) follows the same protocol:

#### Step 1 — Pre-flip baseline capture

Mechanical script: `scripts/cutover/capture_baseline.py --flag <name> --out /tmp/baseline-<flag>.json`. Captures:

- **State digest**: per the affected surface (documents table snapshot for ingest flags; usearch index size for embed flags; etc).
- **Eval scores**: the eval-suite scores that touch the affected surface (`reflib`, `LoCoMo`, whatever the spec entry's `related_spec` says).
- **Performance**: probe latency P50/P95 against the affected surface.
- **Sample journey**: 10 canonical queries / workflows the production agents use; capture top-N results.

The script's output is a structured baseline JSON that the post-flip script consumes.

#### Step 2 — Flip the flag

Operator-side change. For the production instance: edit `kairix.config.yaml` overlay, restart the worker stack. For registry default change: PR.

#### Step 3 — Soak window

Minimum 24h. Maximum per the spec entry (typically a week for low-risk flags; 4 weeks for high-risk like IM-6's scanner swap).

#### Step 4 — Post-flip captures + deltas

Same script: `scripts/cutover/capture_baseline.py --flag <name> --out /tmp/post-<flag>.json`. Then `scripts/cutover/diff_baseline.py --pre /tmp/baseline-<flag>.json --post /tmp/post-<flag>.json --json`. Output is a structured delta with pass/fail per category.

#### Step 5 — Hard gates

- **State delta**: within the flag's declared tolerance (default ±2% of state-digest counts)
- **Eval delta**: scores within ±2 percentage points (for recall metrics) or ±3 (for synthesis quality)
- **Latency delta**: P95 within ±20%
- **Sample-journey parity**: ≥80% of canonical queries return the same top-3 documents (some reordering tolerated; major drift is the canary)

Any hard gate fails → flip the flag back, document the regression, don't promote to `cutover` stage.

#### Step 6 — Promote

All gates green → registry PR changes `default=True`, `stage="cutover"`. Operator overlay can be removed (the flag is now on for everyone). The escape hatch (operator setting `false`) remains until retirement.

#### Step 7 — Retire (after cutover-stage soak)

Typically 2–4 weeks of cutover-stage with no rollbacks → registry PR removes the entry, deletes the legacy code path. The flag is gone. The new behaviour is the only behaviour.

## 5. Both-branch test coverage — F54

Every flag in the registry must have:

### 5.1 BDD coverage (≥2 scenarios per flag)

```gherkin
# tests/bdd/features/feature_flag_obsidian_connector_primary.feature
Feature: Operator toggles the obsidian-connector-primary feature flag

  As an operator running a kairix container
  I want to choose between the legacy DocumentScanner and the new Obsidian connector
  So that I can validate the new path before cutting over

  Scenario: Flag OFF — legacy DocumentScanner indexes the vault
    Given the operator has the obsidian-connector-primary flag set to false
    When the worker maintenance tick runs
    Then the DocumentScanner emits indexing logs
    And the connector pipeline does not run

  Scenario: Flag ON — Obsidian connector indexes the vault
    Given the operator has the obsidian-connector-primary flag set to true
    When the worker maintenance tick runs
    Then the connector pipeline runs for the obsidian connector
    And the DocumentScanner does not run
```

### 5.2 Integration coverage (both branches exercise real composition)

```python
# tests/integration/test_feature_flag_obsidian_connector_primary.py
pytestmark = pytest.mark.integration

def test_flag_off_uses_legacy_scanner(e2e_db):
    """Off branch — DocumentScanner is invoked; connector pipeline is not."""
    paths = e2e_db.with_flag("obsidian_connector_primary", False)
    # ... real-composition assertion through the factory

def test_flag_on_uses_connector_pipeline(e2e_db):
    """On branch — connector pipeline is invoked; legacy scanner is not."""
    paths = e2e_db.with_flag("obsidian_connector_primary", True)
    # ... same shape assertions
```

### 5.3 E2E coverage (only for top-level-capability flags)

```python
# tests/e2e/test_composed_obsidian_connector_primary_path.py
@pytest.mark.e2e
def test_obsidian_connector_primary_on_full_pipeline(tmp_path):
    """Composed path: flag ON → vault → connector → silver → index → query → assertion."""
    # ... same shape as F48's test_composed_production_path.py
```

### 5.4 F54 enforcement

`scripts/checks/check_f54_flag_both_branch_tested.py`:

- For each flag in `REGISTRY`:
  - Verify `tests/bdd/features/feature_flag_<name>.feature` exists with ≥2 scenarios
  - Verify `tests/integration/test_feature_flag_<name>.py` exists with at least one test exercising each branch (heuristic: `with_flag(name, False)` and `with_flag(name, True)` calls present)
  - For flags whose `related_spec` references a top-level-capability spec doc, verify `tests/e2e/test_composed_<name>_path.py` exists

Action-marked failure per F21: `fix: add tests/bdd/features/feature_flag_<name>.feature with OFF + ON scenarios; add tests/integration/test_feature_flag_<name>.py exercising both branches. next: see docs/architecture/feature-flag-architecture.md §5.`

There is no baseline: every flag in the registry must satisfy both-branch coverage.

## 6. The other fitness functions (F51 / F52 / F53)

### F51 — Flag retirement deadline

Every `FeatureFlag.target_retire_in` must be ≤ current `<setuptools-scm version>` + 6 months. The check:

1. Reads the current version from `setuptools-scm` output.
2. Parses each flag's `target_retire_in` and compares.
3. Fails if any flag is past its deadline.

Override: explicitly bump `target_retire_in` in the registry with a rationale comment (`# retire-extension: <reason>`). The bump is itself a PR; the rationale is in the diff. F51 doesn't prevent extensions, it prevents *silent* extensions.

### F52 — Flag call-site reference integrity

AST scan over `kairix/**/*.py`. Every `flag("...")` call site must reference a `name` that exists in `REGISTRY`. Catches typos and dead-flag references after retirement.

### F53 — Operator surface required

`kairix features status` (CLI) and `tool_features_status` (MCP) must exist and emit a structured envelope per F30. The check verifies:

1. `kairix/agents/mcp/server.py` has `@server.tool()` decorating a `tool_features_status` function.
2. `kairix/cli.py:COMMANDS` has a `"features"` entry.
3. Both surfaces have F30 outcome tests.

This is the operations affordance — flags are useless if the operator can't see what's enabled.

## 7. The wave plan, recast with flags

Wave 0 through Wave 4 already landed without flags (the work was scaffolding + Protocol-level, with no user-visible cutover). Wave 5+ all run through the flag pattern.

### Wave 5 — KFEAT-005 P1 connectors (flag-gated dispatch)

Each connector lands as a separate flag-gated worktree:

| Connector | Flag name | Default | Stage at landing |
|---|---|---|---|
| dex_crm | `connector_dex_crm` | False | introduce |
| m365_email_headers | `connector_m365_email_headers` | False | introduce |
| m365_calendar | `connector_m365_calendar` | False | introduce |

Each commit lands:
- The connector plugin code
- The registry entry
- BDD feature file with ≥2 scenarios (OFF + ON; F54)
- Integration test exercising both branches (F54)
- E2E composed-path test (F48, given each is a top-level capability)
- Operator-facing docs entry referencing the flag

Operator opt-in via `kairix.config.yaml` `features: {connector_dex_crm: true}` etc. Cutover protocol (§4.2) runs per connector.

### Wave 6 — SharePoint connector (flag-gated)

`connector_sharepoint` flag. Same shape as Wave 5 entries. Sensitive because SharePoint surfaces client-confidential content (per ADR-005); the cutover protocol's `sensitivity` parity check is non-negotiable here.

### Wave E — connector fleet completion + topology per-source pilots

Landed across `v2026.5.24a1` → `v2026.5.24a3`. Each connector ships behind its own `connector_<name>` flag and gets a matching `topology_<name>` flag for the per-source-unit Container pilot (folders for Obsidian, drives for SharePoint, channels for Slack, repos for GitHub, page-trees for Notion, mailboxes / calendars / tenants for M365 / Dex). Every flag is default-off and lands with the canonical F54 both-branch coverage.

| Connector | `connector_<name>` flag | `topology_<name>` pilot | Sensitivity default |
|---|---|---|---|
| Obsidian | n/a (always loaded) | `topology_obsidian` | internal |
| Dex CRM | `connector_dex_crm` | `topology_dex_crm` | internal |
| M365 email headers | `connector_m365_email_headers` | `topology_m365_email_headers` | client-confidential |
| M365 calendar | `connector_m365_calendar` | `topology_m365_calendar` | client-confidential |
| SharePoint | `connector_sharepoint` | `topology_sharepoint` | internal |
| Slack | `connector_slack` | `topology_slack` | per-channel-kind (internal / confidential / personal) |
| GitHub | `connector_github` | `topology_github` | client-confidential |
| Notion | `connector_notion` | `topology_notion` | internal |

One operational flag also lands in Wave E: `maintenance_loop` enables the background orphan-vector cleanup worker (KFEAT-021 Phase 1). Default-off; see [`how-to-upgrade-kairix`](../operations/runbooks/how-to-upgrade-kairix.md) for the operator recipe.

### Wave 7 — Vision-enhanced extraction + Teams transcripts

`extractor_vision_enabled` (vision LLM cost-gated) and `connector_teams_transcripts`. Same shape.

### IM-6 — DocumentScanner cutover (completed)

The original IM-6 plan ("swap DocumentScanner → Obsidian connector on the production instance, prove parity, delete legacy") ran the full lifecycle through this pattern and is **done**. It is preserved here as the worked example of the arc:

1. **Introduce**: registry entry for `obsidian_connector_primary` at introduce stage (default off). The worker branched on the flag; the legacy DocumentScanner path stayed untouched.
2. **Operator opt-in**: `kairix.config.yaml` overlay set `obsidian_connector_primary: true`. UAT began; the cutover protocol (§4.2) ran — baseline capture, soak, post-flip eval/perf, hard gates.
3. **Cutover**: registry change moved the stage to `cutover` and default to `True`, escape hatch retained.
4. **Retire**: with the production worker logging `source='config' effective=True` for the whole `obsidian_connector_primary` + `topology_*` family (task #132), the flags were retired from `REGISTRY` and the gated call sites inlined to the post-cutover behaviour. The flags are gone; the connector path is the only path.

Each transition was reviewable, reversible until retirement, and mechanically gated by F51 + F54.

## 8. CLAUDE.md integration

All three additions below are live in `CLAUDE.md` today (the §"Cutover patterns" section, the F51–F54 rows in the fitness-function canon, and the docs-resolver row). They are reproduced here as the record of what this spec contributed.

### 8.1 §"Cutover patterns" section

```markdown
## Cutover patterns

Every change that swaps production behaviour goes through a feature flag.
The pattern is mandatory for connector swaps, ranker swaps, schema
migrations, and any change that's reversible-until-validated.

See [`docs/architecture/feature-flag-architecture.md`](docs/architecture/feature-flag-architecture.md)
for the canonical spec. Summary:

- **Default-safe** — every flag defaults to the validated behaviour.
- **Both-branch tested (F54)** — every flag has BDD + integration tests
  for OFF and ON. E2E for top-level capability flags.
- **Mechanical retirement (F51)** — every flag has a `target_retire_in`
  version; F51 fires past that without a rationale extension.
- **Operator surface (F53)** — `kairix features status` and the MCP tool
  `tool_features_status` are required affordances.

Cutover protocol per flag flip: baseline capture → flip → soak (24h
min) → post-flip captures → hard gates (state/eval/latency/sample-journey
deltas) → promote to cutover stage or rollback.
```

### 8.2 F51 / F52 / F53 / F54 in the F-rule canon

Carried in the F-rule enumeration alongside the rest of the catalogue (see `scripts/checks/_rule_catalogue.py` and `docs/architecture/fitness-functions.md`).

### 8.3 Docs-resolver row

```markdown
| Cut over from old behaviour to new behaviour without breaking operators | **[`docs/architecture/feature-flag-architecture.md`](docs/architecture/feature-flag-architecture.md)** — F51..F54, the cutover protocol, both-branch test requirement |
```

## 9. Implementation history (delivered)

The pattern shipped in the sequence below; all steps are on `main`:

1. **This spec** — the canonical reference for the surface.
2. **`kairix/core/features/` implementation** — registry, resolver, `flag()` surface, observability hook, MCP tool, CLI subcommand, plus the fake resolver in `tests/fakes.py`. Landed with an empty registry; the connector / search-pipeline / onboarding flags were added by their own waves.
3. **F51 + F52 + F53 + F54 fitness functions** — the four check scripts under `scripts/checks/`, with unit tests. They landed with zero violations and stay there.
4. **Cutover tooling** — `scripts/cutover/capture_baseline.py` + `scripts/cutover/diff_baseline.py` + the report shape per §4.2.
5. **CLAUDE.md edits** (§8) — the Cutover patterns section, the F51–F54 canon rows, and the docs-resolver row.
6. **IM-6 cutover** — the `obsidian_connector_primary` flag ran the full introduce → cutover → retire arc (§7) and is now retired.
7. **Connector waves** — each connector (Dex CRM, M365 email-headers / calendar, SharePoint, Notion, GitHub, Slack, Linear, skills, Gmail) lands behind its own `connector_<name>` flag with BDD + integration + E2E coverage; see the live `REGISTRY` for the current set and stages.

## 10. References

- `docs/architecture/connector-ingestion-architecture.md` — wave plan that consumes this pattern
- `docs/architecture/test-discipline-hardening.md` — F45–F48 + the composition / real-path / new-capability principles that F54 extends
- `docs/architecture/fitness-functions.md` — F-rule canon; F51–F54 are catalogued here
- Two-scope architecture: per-container scope means one flag set per container; LaunchDarkly-style multi-tenant rollout doesn't apply
- `feedback_deployed_config_path` memory — config-layering pattern this resolver reuses
