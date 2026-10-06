"""Fitness function catalogue — the single source of truth for kairix's F-rules.

Every ``scripts/checks/check_*.py`` has a corresponding entry here.
Every entry references a real check. The bidirectional consistency is
proven by ``tests/checks/test_rule_catalogue.py``.

Rationale (ADR-026 follow-up)
-----------------------------
F1...F73 grew organically over 18 months. Numbers are stable for
commit / ADR / runbook traceability (CWE pattern), but the flat
numeric space lost conceptual cohesion. The catalogue adds two
orthogonal dimensions every rule carries:

* **category** — what concern the rule protects. Drives CLAUDE.md
  grouping and future "which rules govern plugin contracts?" queries.
* **scope** — what shape the rule fires on (per-file, per-plugin,
  per-flag, per-table, cross-cutting). Drives the FitnessRule
  abstraction choice (default enumeration vs custom override).

Numbers stay flat and global; categories carry the cohesion. The
CWE approach: never renumber, never reuse — the rule's ID is a
permanent ship tag, the category is mutable metadata.

Status vocabulary
-----------------
* ``shipped`` — fully enforced; baseline grandfathers existing
  offenders; net-new violations block at pre-commit / CI.
* ``vacuous`` — shipped detector but no current violations because
  the relevant tree doesn't exist yet (e.g. ``kairix/chunkers/``).
  Fires the moment Wave N lands the tree.
* ``proxy`` — structural approximation of a concern that ideally
  needs runtime instrumentation. Acknowledged limitation.
* ``proposed`` — designed and documented; not yet implemented.
  Deferred to a future ADR with prerequisites called out.
* ``superseded`` — replaced by another rule; entry kept for
  traceability of historical references.
"""

from __future__ import annotations

from typing import Literal

# The RuleEntry schema is now sourced from the shared three-cubes-fitness
# package (EPIC #499 common-process) — kairix is a THIN CONSUMER of the
# catalogue-driven runner, so the row schema is repo-agnostic and lives in
# ``tc_fitness.catalogue``. kairix keeps its OWN F-number rows (below) and its
# OWN closed ``Category`` / ``Scope`` / ``Status`` vocabularies (validated in
# ``tests/checks/test_rule_catalogue.py``); the package's ``RuleEntry`` uses
# open ``str`` for those dimensions so each repo curates its own taxonomy.
from tc_fitness.catalogue import (  # noqa: F401  # StagedClass re-exported for the staged-selection consumers
    RuleEntry,
    StagedClass,
)

Category = Literal[
    "layering",
    "test-discipline",
    "plugin-contract",
    "production-safety",
    "schema-integrity",
    "feature-flag",
    "agent-affordance",
    "repo-hygiene",
    "observability",
    "go-discipline",
    "coverage",
    "process",
]

Scope = Literal[
    "per-file",
    "per-class",
    "per-method",
    "per-plugin",
    "per-flag",
    "per-table",
    "per-test",
    "per-protocol-method",
    "per-commit",
    "cross-cutting",
]

Status = Literal[
    "shipped",
    "vacuous",
    "proxy",
    "proposed",
    "superseded",
]

# ── staged-selection class (#499 Phase 2 stage 4b) ──────────────────────
#
# How ``run_checks.py --staged`` decides whether — and over WHAT — to run a
# rule given the staged file set. Three classes, ordered by how much the
# runner may safely narrow:
#
# * ``"file-local"`` — a violation is determinable from a single file in
#   isolation (every import-boundary / location / marker / regex rule:
#   F26 / F8 / F76 / …). A staged change can only NEWLY violate the rule if a
#   staged file is in the rule's path-scope, AND only the staged files need
#   re-checking — the non-staged files were clean at the previous commit and
#   their content is unchanged, so the baseline-diff verdict for them is
#   unchanged. Deleting a file can only REMOVE a file-local violation, never
#   add one. → run over ``staged ∩ scope``; skip when that intersection is
#   empty. This is the default residue.
#
# * ``"relational"`` — a violation depends on cross-file state: a code
#   surface in tree A paired with a test / spec / route artefact in tree B
#   (F30 CLI↔outcome-test, F45 capability↔BDD-feature, F54 flag↔both-branch,
#   F90 template↔route, …). A staged change anywhere in the rule's broader
#   scope — INCLUDING a deletion of the paired artefact, or a NEW surface
#   file — can break the invariant even when the obviously-"in-scope" file
#   isn't itself staged. → if any staged path is within the rule's scope,
#   run the rule over its FULL scope (not just the staged files).
#
# * ``"always-run"`` — the trigger is "any change at all": net-new-file
#   detection (F50), catalogue currency (F92), README / path-naming
#   invariants that fire on any new tracked path. → always run.
#
# ``StagedClass`` is now imported from ``tc_fitness.catalogue`` (above) — the
# shared schema owns the closed three-member vocabulary; kairix consumes it.

# ── paved-road task-type vocabulary (#499 Phase 2) ──────────────────────
#
# A CLOSED set of agent-facing "I'm about to do X" tasks. ``rules.py
# --task <t>`` answers "what pattern do I follow for X?" by listing every
# RuleEntry tagged with that task. The set is intentionally small and
# capability-shaped — the tasks an agent hits when BUILDING something, not
# every concern a rule protects. A member that isn't in this tuple is a
# typo; ``tests/architecture/test_rules_query.py`` guards that forever.
TASK_TYPES: tuple[str, ...] = (
    "adding-a-cli-subcommand",
    "adding-an-mcp-tool",
    "adding-a-connector",
    "adding-an-extractor",
    "adding-a-provider",
    "adding-a-feature-flag",
    "writing-a-test",
    "writing-a-bdd-feature",
    "a-schema-change",
    "editing-a-gate-script",
)
"""The closed task-type vocabulary for ``RuleEntry.task_type``.

Frozen membership: every ``task_type`` tag on every entry MUST be a
member. New tasks are added here deliberately (and only here)."""

_TASK_TYPES_FROZEN: frozenset[str] = frozenset(TASK_TYPES)


# ``RuleEntry`` is imported from ``tc_fitness.catalogue`` (top of module).
#
# It is the repo-agnostic row schema the shared runner reads. The schema carries
# EVERY field kairix's rows below use — ``id`` / ``gate`` / ``check`` /
# ``category`` / ``scope`` / ``summary`` / ``adr_origin`` / ``status`` / ``tags``
# / ``script`` / ``run_all`` / ``exemplar`` / ``task_type`` / ``staged_class`` /
# ``staged_scope`` — plus two OPTIONAL runtime-arg fields the conditional
# coverage check uses (``subprocess_arg_env`` / ``subprocess_arg_default``).
#
# kairix's rows type-check against the package schema's open ``str`` category /
# scope / status; kairix's OWN closed ``Category`` / ``Scope`` / ``Status``
# Literals (above) stay the kairix-side taxonomy, validated at runtime by
# ``tests/checks/test_rule_catalogue.py``. The runner's dispatch resolution,
# paved-road affordance, and staged-selection semantics are documented in
# ``tc_fitness.catalogue.RuleEntry`` and ``tc_fitness.staged``.


_ENTRIES: tuple[RuleEntry, ...] = (
    # ----- layering --------------------------------------------------------
    RuleEntry(
        id="F26",
        gate="f26",
        check="provider_layer_imports",
        category="layering",
        scope="per-file",
        summary="kairix/core/** may not import kairix/providers/** or kairix/transport/**",
        adr_origin="docs/architecture/provider-plugin-architecture.md",
    ),
    RuleEntry(
        id="F27",
        gate="f27",
        check="no_cross_provider",
        category="layering",
        scope="per-file",
        summary="kairix/providers/<a>/ may not import another provider — plugins ship independently",
    ),
    RuleEntry(
        id="F34",
        gate="f34",
        check="f34_core_connector_layer_imports",
        category="layering",
        scope="per-file",
        summary="kairix/core/connectors/** may not import kairix/connectors/** or kairix/extractors/**",
        adr_origin="docs/architecture/connector-ingestion-architecture.md",
        exemplar="kairix/connectors/obsidian/connector.py",
        task_type=("adding-a-connector",),
    ),
    RuleEntry(
        id="F35",
        gate="f35",
        check="f35_no_cross_connector",
        category="layering",
        scope="per-file",
        summary="kairix/connectors/<a>/ may not import another connector or any extractor",
        exemplar="kairix/connectors/obsidian/connector.py",
        task_type=("adding-a-connector",),
    ),
    RuleEntry(
        id="F37",
        gate="f37",
        check="f37_singular_sync",
        category="layering",
        scope="per-file",
        summary="change-detection / sync code only under kairix/connectors or kairix/core/connectors",
        tags=("singularity",),
    ),
    RuleEntry(
        id="F38",
        gate="f38",
        check="f38_silver_singleton",
        category="layering",
        scope="per-file",
        summary="Silver processing (chunking + signal extraction) only in kairix/core/connectors/silver.py",
        tags=("singularity",),
    ),
    RuleEntry(
        id="F44",
        gate="f44",
        check="f44_engagement_firm_boundary",
        category="layering",
        scope="per-file",
        summary="engagement-scope code may not import firm-scope storage clients (psycopg etc.)",
    ),
    RuleEntry(
        id="F61",
        gate="f61",
        check="f61_collection_router_singleton",
        category="layering",
        scope="per-file",
        summary="bare _SqliteChunkWriter(db, collection=...) construction only under kairix/core/connectors/",
        adr_origin="docs/architecture/connector-scope-topology/ADR.md",
        tags=("singularity",),
    ),
    # ----- test-discipline -------------------------------------------------
    RuleEntry(
        id="F1",
        gate="no-internal-patches",
        check="no_internal_patches",
        category="test-discipline",
        scope="per-file",
        summary="no @patch / monkeypatch on kairix internals — inject Fake* through a seam",
        script="check-no-internal-patches.sh",
        # Shell detector greps tests/ for @patch on kairix.* targets. File-local
        # (per-test-file), but runs as a subprocess so it can't narrow to staged
        # files — it runs its full tests/ grep when a tests/ path is staged.
        staged_scope=("tests",),
    ),
    RuleEntry(
        id="F2",
        gate="no-env-monkeypatch",
        check="no_env_monkeypatch",
        category="test-discipline",
        scope="per-file",
        summary='no monkeypatch.setenv("KAIRIX_*") — pass deps as kwargs instead',
        script="check-no-env-monkeypatch.sh",
        # Shell detector greps tests/ for monkeypatch.setenv("KAIRIX_*").
        staged_scope=("tests",),
    ),
    RuleEntry(
        id="F5",
        gate="no-internal-test-imports",
        check="no_internal_imports",
        category="test-discipline",
        scope="per-file",
        summary="no internal-name imports in tests — use public surface only",
    ),
    RuleEntry(
        id="F6",
        gate="no-test-only-kwargs",
        check="no_test_only_kwargs",
        category="test-discipline",
        scope="per-method",
        summary="no *_fn=None test-only kwargs in production",
        # File-local: scans kairix/ for *_fn=None test-only kwargs in prod.
        staged_scope=("kairix",),
    ),
    RuleEntry(
        id="F7",
        gate="per-file-coverage-floor",
        check="per_file_coverage",
        category="coverage",
        scope="per-file",
        summary="per-file coverage ≥ 90% (unit) — Stage 2 floor",
        # The coverage check takes a runtime Cobertura-XML argument read from an
        # env var (safe-commit's per-invocation artifact path), and is SKIPPED
        # when the report is absent. The shared runner dispatches it as a guarded
        # subprocess with the resolved path appended (never in-process), via the
        # conditional-check seam ``run_checks.py`` wires for the exact skip text.
        subprocess_arg_env="KAIRIX_COVERAGE_XML",
        subprocess_arg_default="coverage.xml",
    ),
    RuleEntry(
        id="F8",
        gate="test-markers",
        check="test_markers",
        category="test-discipline",
        scope="per-test",
        summary="every test_* carries a category marker (unit/bdd/contract/integration/e2e/slow/soak/invariant)",
        exemplar="tests/test_credentials.py",
        task_type=("writing-a-test",),
    ),
    RuleEntry(
        id="F9",
        gate="per-file-coverage-floor-union",
        check="per_file_coverage",
        category="coverage",
        scope="per-file",
        summary="per-file coverage ≥ 90% on union of unit + integration (Stage 5)",
        run_all=False,
        # Same conditional Cobertura-XML runtime arg as F7 (shares the script).
        subprocess_arg_env="KAIRIX_COVERAGE_XML",
        subprocess_arg_default="coverage.xml",
    ),
    RuleEntry(
        id="F11",
        gate="test-skip-rationale",
        check="core:test_skip_rationale",
        category="test-discipline",
        scope="per-test",
        summary="every pytest.mark.skip/skipif/xfail/importorskip has a rationale comment",
    ),
    RuleEntry(
        id="F12",
        gate="bdd-no-implementation-leaks",
        check="bdd_happy_path",
        category="test-discipline",
        scope="per-file",
        summary="every BDD feature has a happy-path scenario",
        tags=("bdd",),
        exemplar="tests/bdd/features/bootstrap.feature",
        task_type=("writing-a-bdd-feature",),
    ),
    RuleEntry(
        id="F13",
        gate="bdd-no-implementation-leaks",
        check="bdd_no_implementation_leaks",
        category="test-discipline",
        scope="per-file",
        summary="BDD scenarios reject implementation symbols (Mock, kairix.<pkg>.<symbol>)",
        tags=("bdd",),
        exemplar="tests/bdd/features/cli_connect_github_app.feature",
        task_type=("writing-a-bdd-feature",),
    ),
    RuleEntry(
        id="F45",
        gate="f45",
        check="f45_new_capability_bdd",
        category="test-discipline",
        scope="per-commit",
        summary="every new CLI/MCP/provider/connector/extractor adds a BDD feature in the same commit",
        adr_origin="docs/architecture/test-discipline-hardening.md",
        exemplar="tests/bdd/features/bootstrap.feature",
        task_type=(
            "adding-a-cli-subcommand",
            "adding-an-mcp-tool",
            "adding-a-connector",
            "adding-an-extractor",
            "adding-a-provider",
            "writing-a-bdd-feature",
        ),
        # Relational: a new capability surface (CLI/MCP/provider/connector/
        # extractor) under kairix/, OR a deleted feature file, breaks the
        # "ships a BDD feature in the same commit" invariant.
        staged_class="relational",
        staged_scope=("kairix", "tests/bdd/features"),
    ),
    RuleEntry(
        id="F46",
        gate="f46",
        check="f46_bdd_step_composition",
        category="test-discipline",
        scope="per-file",
        summary="BDD step impls compose via CLI/MCP/factory — no direct *Pipeline(...) construction",
        exemplar="tests/integration/test_vec_index_lifecycle.py",
        task_type=("writing-a-bdd-feature", "writing-a-test"),
        # Relational, not file-local: collect_violations reads the cross-file
        # source kairix/agents/mcp/server.py (via _discover_mcp_tool_names) to
        # decide whether a step's bare call routes through an MCP tool — so a
        # staged server.py edit (removing a @server.tool() a step relies on) can
        # newly violate an UN-staged step file. Scope spans both sides.
        staged_class="relational",
        staged_scope=("tests/bdd/steps", "kairix/agents/mcp/server.py"),
    ),
    RuleEntry(
        id="F47",
        gate="f47-integration-factory",
        check="f47_integration_factory",
        category="test-discipline",
        scope="per-file",
        summary="integration tests construct multi-component pipelines via kairix.core.factory.build_*",
        exemplar="tests/integration/test_vec_index_lifecycle.py",
        task_type=("writing-a-test",),
    ),
    RuleEntry(
        id="F48",
        gate="f48",
        check="f48_e2e_present",
        category="test-discipline",
        scope="cross-cutting",
        summary="tests/e2e/test_composed_production_path.py exists, runs in CI Stage 4.5",
        exemplar="tests/e2e/test_composed_production_path.py",
        task_type=("writing-a-test",),
        # Relational: presence/shape of the composed-path E2E file. Deleting or
        # gutting it under tests/e2e breaks the invariant.
        staged_class="relational",
        staged_scope=("tests/e2e",),
    ),
    RuleEntry(
        id="F54",
        gate="f54",
        check="f54_flag_both_branch_tested",
        category="feature-flag",
        scope="per-flag",
        summary=(
            "every flag has OFF + ON BDD scenarios, integration tests, and (for top-level) an E2E composed-path test"
        ),
        tags=("test-discipline",),
        exemplar="tests/bdd/features/feature_flag_connector_github.feature",
        task_type=("adding-a-feature-flag",),
        # Relational: a flag added to REGISTRY, OR a deleted both-branch
        # BDD/integration/e2e artefact, breaks coverage parity.
        staged_class="relational",
        staged_scope=(
            "kairix/core/features",
            "tests/bdd/features",
            "tests/integration",
            "tests/e2e",
        ),
    ),
    RuleEntry(
        id="F62",
        gate="f62-stateful-multi-tick",
        check="f62_stateful_multi_tick",
        category="test-discipline",
        scope="per-class",
        summary="every stateful tick/run_batch component has a multi-tick advance/idempotency test",
        adr_origin="2026-05 production cursor-write incident",
        # Relational: a new tick/run_batch class under kairix/core/connectors |
        # maintenance, OR a deleted multi-tick test, breaks the pairing.
        staged_class="relational",
        staged_scope=(
            "kairix/core/connectors",
            "kairix/core/maintenance",
            "tests/integration",
            "tests/contracts",
        ),
    ),
    RuleEntry(
        id="F68",
        gate="f68-protocol-failure-modes",
        check="f68_protocol_failure_modes",
        category="test-discipline",
        scope="per-protocol-method",
        summary="every Protocol method has a failure-injection contract test",
        adr_origin="docs/architecture/ADR-024-test-pyramid-redesign.md §F68",
        # Relational: a Protocol declared anywhere under kairix/, OR a deleted
        # failure-mode contract test, breaks the pairing.
        staged_class="relational",
        staged_scope=("kairix", "tests/contracts"),
    ),
    RuleEntry(
        id="F69",
        gate="f69-scale-bound-tests",
        check="f69_scale_bound_tests",
        category="test-discipline",
        scope="per-test",
        summary="every integration test with .fetchall()/list_changes has a ≥10K-row variant",
        adr_origin="docs/architecture/ADR-024-test-pyramid-redesign.md §F69",
        # File-local: scans tests/integration/ for fetchall/list_changes tests
        # lacking a scale variant; each test file is judged in isolation.
        staged_scope=("tests/integration",),
    ),
    RuleEntry(
        id="F72",
        gate="f72-integrity-invariants",
        check="f72_integrity_invariants",
        category="test-discipline",
        scope="cross-cutting",
        summary="every cross-layer integrity invariant has a fixture-scale AND soak-scale test",
        adr_origin="docs/architecture/ADR-024-test-pyramid-redesign.md §F72",
        # Relational: pairs a fixture-scale invariant test with its soak-scale
        # sibling; deleting either (anywhere under tests/) breaks the pair.
        staged_class="relational",
        staged_scope=("tests",),
    ),
    RuleEntry(
        id="F81",
        gate="f81-fresh-install-smoke",
        check="f81_fresh_install_smoke",
        category="test-discipline",
        scope="cross-cutting",
        summary=(
            "CI fresh-install smoke — clean dir → compose boot → healthz → MCP handshake → "
            "wizard 200 → wizard choreography (POST scan partial + key form→redirect) → BM25 search "
            "hit (scripts/checks/check-fresh-install-smoke.sh via "
            ".github/workflows/fresh-install-smoke.yml; per-commit leg checks the wiring)"
        ),
        adr_origin="onboarding tranche 3, 2026-06-11 — registered via EPIC #499 Phase 0; "
        "choreography stage added EPIC #499 Phase 3",
        # Relational wiring check: the smoke script and the workflow that
        # invokes it live in different trees; deleting either (or breaking the
        # invocation) trips it.
        staged_class="relational",
        staged_scope=("scripts/checks/check-fresh-install-smoke.sh", ".github/workflows/fresh-install-smoke.yml"),
    ),
    RuleEntry(
        id="F82",
        gate="f82",
        check="f82_wall_clock_ceilings",
        category="test-discipline",
        scope="per-test",
        summary=(
            "wall-clock ceiling assertions banned outside soak/probe tiers — elapsed-time vs numeric "
            "ceiling requires a slow/soak/load/pvt marker or # F82-allowed: rationale (#493 flake family)"
        ),
        adr_origin="EPIC #499 Phase 0 — #493 wall-clock flake family",
    ),
    RuleEntry(
        id="F84",
        gate="f84",
        check="f84_config_round_trip",
        category="test-discipline",
        scope="per-method",
        summary=(
            "every production config-write site (write_config_updates / update_config_file / "
            "write_config_yaml / config-writer-named yaml.dump) has a composed write→read "
            "round-trip test through the canonical layered reader (#492 overlay split-brain class)"
        ),
        adr_origin="EPIC #499 Phase 1 — #492 overlay split-brain (H1)",
        exemplar="tests/integration/test_wizard_config_overlay_split_brain.py",
        task_type=("a-schema-change", "writing-a-test"),
        # Relational: a config-write site under kairix/, OR a deleted round-trip
        # test under tests/, breaks the write→read coverage pairing.
        staged_class="relational",
        staged_scope=("kairix", "tests"),
    ),
    RuleEntry(
        id="F88",
        gate="f88",
        check="f88_docstring_raises_parity",
        category="test-discipline",
        scope="per-method",
        summary=(
            "every SetupService / KairixSetupService method documenting a concrete Raises: type "
            "is either handled (except, incl. superclass) in the wizard route that calls it or "
            "render-tested under tests/platform/setup (session-escape-5 raw-500 class)"
        ),
        adr_origin="EPIC #499 Phase 1 — session-escape-5 (save_source ValueError surfaced as 500)",
        exemplar="tests/platform/setup/test_setup_service.py",
        task_type=("writing-a-test",),
        # Relational: a documented Raises: on a SetupService method, the wizard
        # route that must handle it, OR the render test, all live in different
        # files; touching any can change the parity verdict.
        staged_class="relational",
        staged_scope=("kairix/platform/setup", "tests/platform/setup"),
    ),
    RuleEntry(
        id="F87",
        gate="f87",
        check="f87_persist_load_corpus",
        category="test-discipline",
        scope="cross-cutting",
        summary=(
            "every registered persist/load pair (set_secret/load_secrets_file, FileTokenStore/secrets "
            "read, write_config_updates/load_merged_mapping, EmbeddingCache put_many/get_many) ships an "
            "adversarial round-trip corpus — multi-line + unicode + large (>=64KiB) + escape-lookalike "
            "(the GitHub-PEM consent-failure class)"
        ),
        adr_origin="EPIC #499 Phase 1 — GitHub-PEM multi-line secret round-trip (session escape 2)",
        exemplar="tests/integration/test_secrets_pem_round_trip.py",
        task_type=("writing-a-test",),
        # Relational: a registered persist/load pair in kairix/ pairs with an
        # adversarial-corpus test under tests/; deleting either breaks it.
        staged_class="relational",
        staged_scope=("kairix", "tests"),
    ),
    RuleEntry(
        id="F86",
        gate="f86",
        check="f86_di_default_execution_floor",
        category="test-discipline",
        scope="per-method",
        summary=(
            "DI-default execution floor (static half) — every _default_* production seam in "
            "kairix/** stays visible to the coverage floor: no # pragma: no cover (escape-4 class)"
        ),
        adr_origin="EPIC #499 Phase 1 — escape 4, the terminal-wizard pragma'd embed seam",
        # File-local: scans kairix/ for _default_* seams carrying a no-cover
        # pragma; each seam is judged in its own file.
        staged_scope=("kairix",),
    ),
    RuleEntry(
        id="F86-dynamic",
        gate="f86-dynamic",
        check="f86_di_default_execution_floor",
        category="test-discipline",
        scope="per-method",
        summary=(
            "DI-default execution floor (dynamic half) — every _default_* seam body has ≥1 "
            "executed line in the union coverage report; skips clean when no report (F9 stage)"
        ),
        adr_origin="EPIC #499 Phase 1 — escape 4, the terminal-wizard pragma'd embed seam",
        run_all=False,
    ),
    # ----- plugin-contract -------------------------------------------------
    RuleEntry(
        id="F28",
        gate="f28",
        check="provider_bdd_completeness",
        category="plugin-contract",
        scope="per-plugin",
        summary="every provider plugin has matching BDD feature + Examples-table row in E2E features",
        # Relational: a provider plugin dir pairs with a per-plugin feature +
        # an Examples-row in the E2E feature; deleting either breaks parity.
        staged_class="relational",
        staged_scope=("kairix/providers", "tests/bdd/features"),
    ),
    RuleEntry(
        id="F36",
        gate="f36",
        check="f36_connector_bdd_parity",
        category="plugin-contract",
        scope="per-plugin",
        summary="every connector + extractor plugin has matching BDD feature + Examples-table row",
        exemplar="tests/bdd/features/e2e_connector_sync.feature",
        task_type=("adding-a-connector", "adding-an-extractor", "writing-a-bdd-feature"),
        # Relational: a connector/extractor plugin dir pairs with a per-plugin
        # feature + an Examples-row in e2e_connector_sync.feature.
        staged_class="relational",
        staged_scope=("kairix/connectors", "kairix/extractors", "tests/bdd/features"),
    ),
    RuleEntry(
        id="F40",
        gate="f40",
        check="f40_extractor_version",
        category="plugin-contract",
        scope="per-plugin",
        summary="every Extractor plugin declares module-level version: str + make_extractor factory",
        exemplar="kairix/extractors/docx/__init__.py",
        task_type=("adding-an-extractor",),
    ),
    RuleEntry(
        id="F41",
        gate="f41",
        check="f41_plugin_typing",
        category="plugin-contract",
        scope="per-plugin",
        summary="every plugin tree has py.typed marker + no unjustified # type: ignore",
        exemplar="kairix/connectors/obsidian/__init__.py",
        task_type=("adding-a-connector", "adding-an-extractor", "adding-a-provider"),
        # Relational: the py.typed marker is a per-plugin-tree file separate
        # from the plugin code; a new plugin dir needs it and deleting it
        # breaks coverage. Scope derives from the FitnessRule plugin-tree roots.
        staged_class="relational",
    ),
    RuleEntry(
        id="F42",
        gate="f42",
        check="f42_protocol_return_types",
        category="plugin-contract",
        scope="per-protocol-method",
        summary="Protocol methods return frozen-dc/tuple — never dict[str, Any] or bare Any",
        tags=("observability",),
        # File-local: scans the single kairix/core/protocols.py for surface
        # Protocol method return annotations.
        staged_scope=("kairix/core/protocols.py",),
    ),
    RuleEntry(
        id="F43",
        gate="f43",
        check="f43_plugin_contract_tests",
        category="plugin-contract",
        scope="per-plugin",
        summary=(
            "behavioural parity — every contract test runs ONE parametrized body over real + fake "
            "(≥2 impl fixtures), not separate real-only/fake-only assertions; plus the per-plugin "
            "contract-test presence limb"
        ),
        # Relational: a plugin dir pairs with tests/contracts/test_<name>_
        # protocol.py importing the real impl AND tests.fakes; deleting the
        # contract test or a fake breaks parity.
        staged_class="relational",
        staged_scope=(
            "kairix/connectors",
            "kairix/extractors",
            "kairix/providers",
            "tests/contracts",
            "tests/fakes.py",
        ),
    ),
    RuleEntry(
        id="F55",
        gate="f55",
        check="f55_chunker_version",
        category="plugin-contract",
        scope="per-plugin",
        summary="every Chunker plugin declares version + every Chunk(...) passes chunker_version=",
        status="vacuous",
        # File-local (vacuous today): scans kairix/chunkers/<name>/ which does
        # not yet exist.
        staged_scope=("kairix/chunkers",),
    ),
    RuleEntry(
        id="F56",
        gate="f56",
        check="f56_connector_capability_declaration",
        category="plugin-contract",
        scope="per-plugin",
        summary="every connector declares SourceConnector + at least one of {Poll, Checkpointed, Event}Connector",
        # Relational, not file-local: _capability_names_via_runtime imports
        # kairix/core/protocols.py and runtime-isinstance-checks each connector
        # against the Protocols DEFINED there, so a staged protocols.py edit
        # (removing a member from a runtime-checkable Protocol) can newly violate
        # an UN-staged connector. Scope spans the connector tree + protocols.py.
        staged_class="relational",
        staged_scope=("kairix/connectors", "kairix/core/protocols.py"),
    ),
    RuleEntry(
        id="F64",
        gate="f64-external-api-rate-limit",
        check="f64_external_api_rate_limit",
        category="plugin-contract",
        scope="per-plugin",
        summary="every plugin importing an HTTP client ships a rate-limit test (429/Retry-After)",
        # Relational: a plugin that imports an HTTP client pairs with a
        # rate-limit test under tests/integration | tests/bdd; deleting the
        # test breaks the pairing.
        staged_class="relational",
        staged_scope=(
            "kairix/connectors",
            "kairix/providers",
            "tests/integration",
            "tests/bdd/features",
        ),
    ),
    RuleEntry(
        id="F65",
        gate="f65-connector-metadata",
        check="f65_connector_metadata",
        category="plugin-contract",
        scope="per-plugin",
        summary="every connector implements metadata_for + propagation test for chunk_date/author",
        adr_origin="docs/architecture/ADR-021-per-source-metadata-normalisation.md",
        # Relational: a connector dir pairs with tests/integration/test_<name>_
        # metadata_propagation.py; deleting the test breaks the pairing.
        staged_class="relational",
        staged_scope=("kairix/connectors", "tests/integration"),
    ),
    # ----- production-safety ----------------------------------------------
    RuleEntry(
        id="F15",
        gate="no-logging-secrets",
        check="no_logging_secrets",
        category="production-safety",
        scope="per-file",
        summary="no logging of secret-named variables in plaintext outside kairix/{secrets,credentials}.py",
        tags=("security",),
    ),
    RuleEntry(
        id="F39",
        gate="f39",
        check="f39_chunk_metadata",
        category="production-safety",
        scope="per-method",
        summary="every Chunk(...) constructor call passes source_uri + source_modified_at + sensitivity explicitly",
        exemplar="kairix/core/connectors/silver.py",
        task_type=("adding-a-connector",),
    ),
    RuleEntry(
        id="F50",
        gate="net-new-baseline-additions",
        check="f50_net_new_file_violations",
        category="production-safety",
        scope="per-commit",
        summary="net-new files may not appear in any per-file F-rule baseline",
        # Any net-new file in the commit can trip this — the trigger is "a
        # file was added", not a path-scope. Always run.
        staged_class="always-run",
    ),
    RuleEntry(
        id="F63",
        gate="f63-unbounded-fetchall",
        check="f63_unbounded_fetchall",
        category="production-safety",
        scope="per-file",
        summary="every .fetchall() includes LIMIT in the query or carries a # F63-bounded: rationale",
        adr_origin="2026-05 maintenance prune disk-IO saturation",
    ),
    RuleEntry(
        id="F66",
        gate="f66-connector-tick-budget",
        check="f66_connector_tick_budget",
        category="production-safety",
        scope="per-class",
        summary="every connector + tick-driven component declares per_tick_max_items + disk_watermark_min_free_bytes",
        adr_origin="docs/architecture/ADR-020-connector-tick-budget-watermark.md",
    ),
    RuleEntry(
        id="F73",
        gate="no-private-infra-refs",
        check="no_private_infra_refs",
        category="production-safety",
        scope="per-file",
        summary="token-pattern scanner for private infra identifiers (externalised pattern source)",
        tags=("security",),
        # File-local: scans kairix/ + scripts/ + tests/ + docs/ for private
        # infra identifier patterns; each file judged in isolation.
        staged_scope=("kairix", "scripts", "tests", "docs"),
    ),
    RuleEntry(
        id="F89",
        gate="f89",
        check="f89_vendored_asset_manifest",
        category="production-safety",
        scope="per-file",
        summary=(
            "every served file under a kairix/**/web/static/ tree has a sha256-pinned ASSETS.lock "
            "manifest row (upstream version + sha256 + url + rationale); the on-disk sha256 must match "
            "the row, so a swapped/outdated htmx.min.js or pico.css fails instead of shipping untraced"
        ),
        adr_origin="EPIC #499 Phase 3 — un-pinned vendored browser-asset class",
        tags=("security",),
        # Relational within a web/static tree: a served file pairs with its
        # ASSETS.lock manifest row (sha256 + url); a new/swapped asset or a
        # deleted manifest row breaks the pinning.
        staged_class="relational",
        staged_scope=("kairix",),
    ),
    RuleEntry(
        id="F94",
        gate="f94",
        check="f94_no_system_path_writes",
        category="production-safety",
        scope="per-file",
        summary=(
            "no runtime writes to system/OS paths (/etc, /opt, /usr, ...) — production code in "
            "kairix/** persists config + state through kairix.paths (the writable data dir) and the "
            "config overlay, never a hardcoded system path, so kairix runs least-privilege on "
            "hardened / read-only-root VMs (the wizard-save overlay class, #485/#492)"
        ),
        adr_origin="ADR-017 least-privilege / hostile-environment deployment",
        tags=("security",),
        # Whole-tree literal scan over kairix/** (any production module could
        # hardcode a system-path write); the trigger isn't a single staged tree.
        staged_class="always-run",
    ),
    RuleEntry(
        id="F91",
        gate="f91",
        check="f91_browser_surface",
        category="production-safety",
        scope="per-file",
        summary=(
            "wizard browser surface — HTML responses carry nosniff + frame-denial + a "
            "Content-Security-Policy (Limb A: contract test + render-path static check), and every "
            "inline <script> in the setup templates is F91-inline rationale-tagged, ≤20 lines, and "
            "not cross-template-duplicated (Limb B)"
        ),
        adr_origin="EPIC #499 Phase 3 — browser-surface CSP/XSS/inline-JS governance",
        tags=("security",),
        # Relational: Limb A pairs the render path (routes.py) with the headers
        # it must set; Limb B's no-cross-template-duplication is inherently
        # cross-file. A staged template / route change can break either.
        staged_class="relational",
        staged_scope=("kairix/platform/setup/web",),
    ),
    # ----- schema-integrity -----------------------------------------------
    RuleEntry(
        id="F57",
        gate="f57",
        check="f57_ccpair_lifecycle_integrity",
        category="schema-integrity",
        scope="per-file",
        summary="every UPDATE topology_cc_pairs SET status=? lives next to a _ALLOWED_TRANSITIONS dispatch dict",
        adr_origin="docs/architecture/connector-scope-topology/ADR.md",
    ),
    RuleEntry(
        id="F58",
        gate="f58",
        check="f58_hierarchy_parent_before_child",
        category="schema-integrity",
        scope="cross-cutting",
        summary="HierarchyConnector impls have a parent-before-child contract test",
        status="vacuous",
        # Relational (vacuous today): a HierarchyConnector impl under kairix/
        # pairs with a parent-before-child contract test under tests/contracts.
        staged_class="relational",
        staged_scope=("kairix", "tests/contracts"),
    ),
    RuleEntry(
        id="F67",
        gate="f67-staging-drain-symmetry",
        check="f67_staging_drain_symmetry",
        category="schema-integrity",
        scope="per-table",
        summary="every pushed_to_<sink> column has a matching UPDATE site flipping 0 → 1",
        adr_origin="GH #334 — entity_signals 2.3M un-pushed rows",
        # Relational: a pushed_to_<sink> column declared in schema.py needs a
        # drain UPDATE elsewhere under kairix/; deleting the drain breaks it.
        staged_class="relational",
        staged_scope=("kairix",),
    ),
    RuleEntry(
        id="F70",
        gate="f70-schema-writer-symmetry",
        check="f70_schema_writer_symmetry",
        category="schema-integrity",
        scope="per-table",
        summary="every CREATE TABLE has at least one INSERT INTO site OR a # table-is-derived: rationale",
        adr_origin="GH #336 — documents_media 1M-chunk empty-table incident",
        # Relational: a CREATE TABLE in schema.py needs an INSERT site
        # elsewhere under kairix/ (or a derived-rationale); deleting the writer
        # breaks the symmetry.
        staged_class="relational",
        staged_scope=("kairix",),
    ),
    RuleEntry(
        id="F71",
        gate="f71-preflight-truthfulness",
        check="f71_preflight_truthfulness",
        category="schema-integrity",
        scope="per-method",
        summary="every preflight _check_* counting external state has a count-equals-ground-truth contract test",
        adr_origin="docs/architecture/ADR-024-test-pyramid-redesign.md §F71",
        # Relational: a preflight _check_* under kairix/ pairs with the
        # truthfulness contract test under tests/contracts; deleting the test
        # breaks the pairing.
        staged_class="relational",
        staged_scope=("kairix", "tests/contracts"),
    ),
    # ----- feature-flag ---------------------------------------------------
    RuleEntry(
        id="F51",
        gate="f51",
        check="f51_flag_retirement",
        category="feature-flag",
        scope="per-flag",
        summary="every FeatureFlag has target_retire_in ≤ current scm version + 6 months",
        adr_origin="docs/architecture/feature-flag-architecture.md §6",
        # File-local: reads target_retire_in deadlines from the flag REGISTRY.
        staged_scope=("kairix/core/features",),
    ),
    RuleEntry(
        id="F52",
        gate="f52",
        check="f52_flag_call_sites",
        category="feature-flag",
        scope="per-flag",
        summary='every flag("<name>") call site references a name that exists in REGISTRY',
        # Relational: a flag("<name>") call site anywhere under kairix/ is
        # validated against REGISTRY. Removing a flag from the registry can
        # make an UN-staged call site newly invalid → run full scope.
        staged_class="relational",
        staged_scope=("kairix",),
    ),
    RuleEntry(
        id="F53",
        gate="f53",
        check="f53_features_status_surface",
        category="feature-flag",
        scope="cross-cutting",
        summary="kairix features status CLI subcommand + features_status MCP tool both exist",
        # Relational: the CLI "features" entry (cli.py) and the features_status
        # MCP tool (server.py) are the two surfaces; touching either can break
        # the both-exist invariant.
        staged_class="relational",
        staged_scope=("kairix/cli.py", "kairix/agents/mcp/server.py"),
    ),
    # ----- agent-affordance -----------------------------------------------
    RuleEntry(
        id="F3",
        gate="suppressions-have-rationale",
        check="sonar_ignore_rationale",
        category="agent-affordance",
        scope="per-file",
        summary="every # noqa / # NOSONAR / # pragma / # type: ignore / # nosec has rationale text",
        script="check-suppressions-have-rationale.sh",
        # Shell detector greps kairix/ + tests/ + scripts/ for un-rationalised
        # suppressions. File-local (each suppression is judged in isolation).
        staged_scope=("kairix", "tests", "scripts"),
    ),
    RuleEntry(
        id="F10",
        gate="actionable-feedback",
        check="actionable_feedback",
        category="agent-affordance",
        scope="cross-cutting",
        summary="CI workflow silencers (continue-on-error, fail_ci_if_error: false) require rationale",
        script="check-workflow-silencers-have-rationale.sh",
        # Relational across the workflows dir: greps .github/workflows/*.yml
        # for un-rationalised silencers.
        staged_class="relational",
        staged_scope=(".github/workflows",),
    ),
    RuleEntry(
        id="F14",
        gate="sonar-ignore-rationale",
        check="sonar_ignore_rationale",
        category="agent-affordance",
        scope="cross-cutting",
        summary="every sonar.issue.ignore.multicriteria entry has a preceding rationale comment",
        # Relational to the single sonar config file: a staged edit to it can
        # introduce an un-rationalised multicriteria entry.
        staged_class="relational",
        staged_scope=("sonar-project.properties",),
    ),
    RuleEntry(
        id="F16",
        gate="cognitive-complexity",
        check="core:cognitive_complexity",
        category="agent-affordance",
        scope="per-method",
        summary="cognitive complexity ≤ 15 per function (Sonar S3776)",
    ),
    RuleEntry(
        id="F17",
        gate="no-duplicate-string",
        check="core:no_duplicate_string",
        category="agent-affordance",
        scope="per-file",
        summary="no string literal ≥10 chars duplicated ≥3 times in a module (Sonar S1192)",
    ),
    RuleEntry(
        id="new_code_coverage",
        gate="new-code-coverage",
        check="core:new_code_coverage",
        category="coverage",
        scope="per-file",
        summary="new-code coverage ≥ 80% on changed lines (Sonar new-code, local)",
    ),
    RuleEntry(
        id="F18",
        gate="no-commented-out-code",
        check="core:no_commented_out_code",
        category="agent-affordance",
        scope="per-file",
        summary="no commented-out code (Sonar S125)",
    ),
    RuleEntry(
        id="F19",
        gate="unused-params-named",
        check="core:unused_params_named",
        category="agent-affordance",
        scope="per-method",
        summary="unused function parameters must be _-prefixed (Sonar S1172)",
    ),
    RuleEntry(
        id="F20",
        gate="empty-body-intent",
        check="core:empty_body_intent",
        category="agent-affordance",
        scope="per-method",
        summary="empty function bodies require docstring or # Intentionally empty — comment (Sonar S1186)",
    ),
    RuleEntry(
        id="F21",
        gate="actionable-feedback",
        check="actionable_feedback",
        category="agent-affordance",
        scope="cross-cutting",
        summary="every check_*.py failure-output carries fix:/next:/run: action markers",
        # Relational across the checks dir: a staged check_*.{py,sh} could lack
        # action markers; the detector sweeps the whole scripts/checks tree.
        staged_class="relational",
        staged_scope=("scripts/checks",),
    ),
    RuleEntry(
        id="F23",
        gate="readme-coverage",
        check="readme_coverage",
        category="agent-affordance",
        scope="cross-cutting",
        summary="every top-level directory has a README.md resolver",
        # A new top-level directory (a new path anywhere) can leave a README
        # gap; deleting a README breaks it. The trigger is repo structure,
        # not a path-scope. Always run.
        staged_class="always-run",
    ),
    RuleEntry(
        id="F83",
        gate="f83",
        check="f83_gate_runner_contract",
        category="agent-affordance",
        scope="per-file",
        summary=(
            "gate-runner contract for shell gate scripts — no unguarded VAR=$(...) under set -e (#483 "
            "silent-death class), || true requires trailing rationale, shellcheck-clean at error "
            "severity, safe-commit.sh/run-all.sh stages emit named OK/FAIL verdicts, and quiet grep "
            "output probes cannot produce false failures under pipefail"
        ),
        adr_origin="EPIC #499 Phase 0 — #483 silent gate-death class",
        # File-local: scans the shell gate scripts under scripts/ (incl.
        # safe-commit.sh / run-all.sh) for the gate-runner contract.
        staged_scope=("scripts",),
    ),
    RuleEntry(
        id="F85",
        gate="f85",
        check="f85_contract_vocabulary_singularity",
        category="agent-affordance",
        scope="per-file",
        summary=(
            "registered cross-tier contract vocabularies single-sourced — a member of a DECLARED "
            "vocabulary (source-auth PHASE_* strings, the azure provider-name set; owned by "
            "kairix/platform/setup/service.py) re-declared as a constant/collection in another setup-tier "
            "module, or used as a raw string in a template instead of the env.globals symbol, fails "
            "(session-escape-8 phase-string class)"
        ),
        adr_origin="EPIC #499 Phase 1 — session-escape-8 cross-tier vocabulary drift",
        # Relational: a vocabulary owned by service.py is re-declared in another
        # setup-tier module or hardcoded in a template — cross-file drift.
        staged_class="relational",
        staged_scope=("kairix/platform/setup",),
    ),
    RuleEntry(
        id="F90",
        gate="f90",
        check="f90_template_route_choreography",
        category="agent-affordance",
        scope="per-file",
        summary=(
            "setup-wizard template ↔ route referential integrity (F52 applied to the browser tier) — "
            "every hx-get/hx-post/href/action /setup URL resolves to a Route/Mount in routes.py, every "
            "hx-target/hx-include #id is defined in some template, every template is rendered or extended "
            "(dangling-button class: the tour replacing first-search left no dead control)"
        ),
        adr_origin="EPIC #499 Phase 3 — dangling-button choreography class",
        # Relational: a template's hx-* URL resolves against routes.py, and a
        # template id resolves against another template — cross-file. A staged
        # change to either (or a deleted route/template) breaks referentiality.
        staged_class="relational",
        staged_scope=("kairix/platform/setup/web",),
    ),
    # ----- repo-hygiene ---------------------------------------------------
    RuleEntry(
        id="F4",
        gate="env-reads-in-paths",
        check="no_env_monkeypatch",
        category="repo-hygiene",
        scope="per-file",
        summary='no os.environ.get("KAIRIX_*") outside paths.py / secrets.py',
        script="check-env-reads-stay-in-paths.sh",
        # Shell detector greps kairix/ for os.environ KAIRIX_* reads outside the
        # paths/secrets allow-list. File-local.
        staged_scope=("kairix",),
    ),
    RuleEntry(
        id="F22",
        gate="path-naming",
        check="path_naming",
        category="repo-hygiene",
        scope="per-file",
        summary="repo paths follow per-tree naming conventions",
        # Walks `git ls-files` over the whole repo; any net-new tracked path
        # with a non-conforming name trips it, regardless of tree. Always run.
        staged_class="always-run",
    ),
    RuleEntry(
        id="F24",
        gate="no-test-imports-in-prod",
        check="core:no_test_imports_in_prod",
        category="repo-hygiene",
        scope="per-file",
        summary="no `from tests.*` / `import tests` inside kairix/**/*.py — tests not shipped in wheel",
        adr_origin="GH #266 — v2026.5.15.1 → .2 incident",
    ),
    RuleEntry(
        id="F29",
        gate="f29",
        check="perf_singleton",
        category="repo-hygiene",
        scope="per-file",
        summary="performance-measurement code only under kairix/quality/probe/",
        tags=("singularity",),
    ),
    RuleEntry(
        id="F30",
        gate="f30-operator-outcome-tests",
        check="f30_operator_outcome_tests",
        category="test-discipline",
        scope="cross-cutting",
        summary="every CLI subcommand + every MCP tool has an outcome test (subprocess or direct handler)",
        exemplar="tests/test_worker_cli_maintenance.py",
        task_type=("adding-a-cli-subcommand", "adding-an-mcp-tool", "writing-a-test"),
        # Relational: a new subcommand in cli.py / a new @server.tool() in the
        # MCP server, OR a DELETED outcome test under tests/, can break parity.
        # Run full scope when any of those trees is touched.
        staged_class="relational",
        staged_scope=("kairix/cli.py", "kairix/agents/mcp/server.py", "tests"),
    ),
    RuleEntry(
        id="F32",
        gate="no-real-names-in-fixtures",
        check="no_real_names_in_fixtures",
        category="repo-hygiene",
        scope="per-file",
        summary="no real names in test fixtures (use agent-alpha etc. + reference library)",
        # File-local: scans tests/ (.py + .feature) for embedded real names.
        staged_scope=("tests",),
    ),
    RuleEntry(
        id="F33",
        gate="shellcheck-disable-with-reason",
        check="shellcheck_disable_with_reason",
        category="repo-hygiene",
        scope="per-file",
        summary="shellcheck disable directives require rationale",
    ),
    # ----- ADR-026 cross-cutting primitives (Phase 3 — pending) -----------
    RuleEntry(
        id="F74",
        gate="f74-stage-runner-only",
        check="f74_stage_runner_only",
        category="observability",
        scope="per-class",
        summary="every Stage subclass is only invoked via a StageRunner — never direct .process() call",
        adr_origin="docs/architecture/ADR-026-cross-cutting-primitive-abstractions.md §A.5",
        status="vacuous",
    ),
    RuleEntry(
        id="F93",
        gate="f93-ci-fanin-parity",
        check="f93_ci_fanin_parity",
        category="observability",
        scope="cross-cutting",
        summary=(
            "every ci.yml job is in the 'CI gate' aggregator's needs: closure (so its failure "
            "blocks the merge) or carries a # fan-in: informational marker — a green merge can't "
            "ship with an un-gated job failing"
        ),
        adr_origin="EPIC #499 Phase 2 — CI fan-in parity (dangling-job-ships-green class)",
        # Relational to ci.yml: a new job (or a changed needs: closure) in the
        # one workflow file can leave a job outside the CI-gate fan-in.
        staged_class="relational",
        staged_scope=(".github/workflows/ci.yml",),
    ),
    RuleEntry(
        id="F75",
        gate="f75-eval-suite-parity",
        check="(proposed)",
        category="test-discipline",
        scope="cross-cutting",
        summary="every CLI subcommand + MCP tool + connector appears in at least one eval-suite question",
        adr_origin="LoCoMo 95% → 5% regression incident",
        status="proposed",
    ),
    RuleEntry(
        id="F76",
        gate="f76-pii-content-interpolation",
        check="f76_pii_content_interpolation",
        category="production-safety",
        scope="per-file",
        summary=(
            "no f-string interpolation of content-like vars (raw/body/payload/markdown/...) "
            "in log/exception/dead-letter strings"
        ),
        adr_origin="2026-05 leak audit + extends F15 to content layer",
        tags=("security",),
    ),
    RuleEntry(
        id="F77",
        gate="f77-sqlite-single-writer",
        check="f77_sqlite_single_writer",
        category="schema-integrity",
        scope="per-file",
        summary="sqlite3.connect call sites outside the allow-list (worker/factory/scripts/tests) are flagged",
        adr_origin="ADR-026 blindspot audit — concurrency/coordinator discipline",
        status="proxy",
    ),
    # ----- proposed (not yet implemented) ---------------------------------
    RuleEntry(
        id="F78",
        gate="f78-memory-bounds",
        check="(proposed)",
        category="production-safety",
        scope="per-test",
        summary="soak suite asserts RSS / peak memory ≤ budget — needs runtime instrumentation",
        adr_origin="ADR-026 blindspot audit — next-most-likely production blowup profile",
        status="proposed",
    ),
    RuleEntry(
        id="F79",
        gate="f79-migration-reversibility",
        check="(proposed)",
        category="schema-integrity",
        scope="per-commit",
        summary="every schema delta has a tested rollback path; destructive changes keep N-day grace",
        adr_origin="ADR-026 blindspot audit — needs migration framework first (kairix uses create_schema only)",
        status="proposed",
    ),
    RuleEntry(
        id="F80",
        gate="f80-cross-scope-runtime-dataflow",
        check="(proposed)",
        category="layering",
        scope="cross-cutting",
        summary="engagement-scope code may not call firm-scope APIs at runtime — extends F44 from import to request",
        adr_origin="ADR-026 blindspot audit — needs request-level instrumentation",
        status="proposed",
    ),
    # ----- coverage --------------------------------------------------------
    RuleEntry(
        id="baseline-shrinking",
        gate="baseline-shrinking",
        check="baseline_shrinking",
        category="coverage",
        scope="cross-cutting",
        summary="F49: each release tag reduces F30/F46/F47 baselines by ≥1 (or keeps at zero)",
        run_all=False,
    ),
    RuleEntry(
        id="paydown-doc-currency",
        gate="paydown-doc-currency",
        check="paydown_doc_currency",
        category="agent-affordance",
        scope="cross-cutting",
        summary="grandfathering paydown doc reflects current baseline state",
        run_all=False,
    ),
    RuleEntry(
        id="sonar-new-code",
        gate="sonar-new-code",
        check="sonar_new_code",
        category="coverage",
        scope="cross-cutting",
        summary=(
            "SonarCloud per-file count ratchet — current per-file open-issue/hotspot counts "
            "may not exceed the committed baseline (.architecture/baseline/sonar-per-file*.json); "
            "deterministic, no live leak period, no skip flag"
        ),
        adr_origin="EPIC #499 Phase 2 — escape #11 KAIRIX_SKIP_SONAR_PARITY retirement",
        run_all=False,
    ),
    RuleEntry(
        id="worktree-isolation",
        gate="worktree-isolation",
        check="worktree_isolation",
        category="process",
        scope="cross-cutting",
        summary="subagent worktree isolation — no shadow copies in primary checkout",
        adr_origin="GH #208 — upstream anthropics/claude-code#59019",
        run_all=False,
    ),
    RuleEntry(
        id="F92",
        gate="f92",
        check="catalogue_currency",
        category="process",
        scope="cross-cutting",
        summary=(
            "catalogue currency — every check_*.{py,sh} has a RuleEntry, every RuleEntry maps to an "
            "existing check, and the generated doc regions match generate_catalogue_docs.py --check "
            "(the self-hosting guard for the catalogue-driven runner)"
        ),
        adr_origin="EPIC #499 Phase 2 — catalogue-driven runner single-source-of-truth",
        # Self-hosting guard: a staged check script / catalogue / doc edit can
        # break currency, and a doc region can drift from any edit. Always run.
        staged_class="always-run",
    ),
    RuleEntry(
        id="capability-affordance",
        gate="capability-affordance",
        check="capability_affordance",
        category="agent-affordance",
        scope="cross-cutting",
        summary="agent-callable capabilities surface their affordances at the call boundary",
        # Relational: every COMMANDS entry in cli.py pairs with a tool_<cmd> in
        # the MCP server; touching either side can break parity.
        staged_class="relational",
        staged_scope=("kairix/cli.py", "kairix/agents/mcp/server.py"),
    ),
    RuleEntry(
        id="no-hardcoded-user-paths",
        gate="no-hardcoded-user-paths",
        check="no_hardcoded_user_paths",
        category="repo-hygiene",
        scope="per-file",
        summary="F31: no hardcoded /Users/ or /home/<dev>/ paths",
        # Walks `git ls-files` over the whole repo (any text file can carry a
        # hardcoded path); the trigger isn't a single tree. Always run.
        staged_class="always-run",
    ),
    RuleEntry(
        id="F95",
        gate="cypher-write-mode-chokepoint",
        check="core:pattern_chokepoint",
        category="production-safety",
        scope="per-file",
        summary=(
            "Neo4j write-mode selection lives at ONE chokepoint — only "
            "kairix/knowledge/graph/client.py may name `default_access_mode` / the "
            "`_is_write_query` derivation, so no caller re-derives read-vs-write and "
            "silently opens a READ session for a write query (the #628 silent-write class)"
        ),
        adr_origin="GH #628 — neo4j silent-write incident",
        tags=("production-safety",),
        # Literal scan over kairix/** for the forbidden write-mode selectors (the
        # chokepoint file is exempt); any module could re-introduce one. Always run.
        staged_class="always-run",
    ),
    RuleEntry(
        id="F96",
        gate="embed-discovery-state-predicate",
        check="core:integrity_state_predicate",
        category="production-safety",
        scope="per-file",
        summary=(
            "embed-discovery completeness queries that LEFT JOIN content_vectors ... "
            "IS NULL must reference a state column (model / embedded_at), never presence "
            "alone — a presence-only join counts an un-promoted placeholder as 'done' "
            "(the chunk-0 #627 class, where seq=0 placeholders were never embedded)"
        ),
        adr_origin="GH #627 — chunk-0 silent-embedding gap",
        tags=("production-safety",),
        # AST scan of SQL string literals under kairix/core/embed/**; scoped there
        # so the legitimately presence-only integrity check is excluded. Always run.
        staged_class="always-run",
    ),
    RuleEntry(
        id="F97",
        gate="f97",
        check="f97_source_ref_contract",
        category="agent-affordance",
        scope="per-class",
        summary=(
            "every agent-facing result surface embeds or returns the shared SourceRef "
            "breadcrumb — each registered result-row dataclass (search / timeline / entity / "
            "prep / research / contradict) declares a SourceRef-typed field OR a source_ref() "
            "accessor, so the canonical resolvable source_uri is surfaced uniformly and the "
            "per-surface pointer drift can't re-accrue (PLA-274)"
        ),
        adr_origin="PLA-274 — source-breadcrumb contract",
        tags=("agent-affordance",),
        # AST scan of a FIXED registry of agent-surface modules under
        # kairix/use_cases/**; any could drop the contract. Always run.
        staged_class="always-run",
    ),
    RuleEntry(
        id="F98",
        gate="f98",
        check="f98_expand_locator_contract",
        category="agent-affordance",
        scope="per-class",
        summary=(
            "every agent result-row surface exposes an expand-acceptable locator AND "
            "expand accepts a source_uri-only call — each registered surface (search / "
            "timeline / entity / prep / research / contradict / expand) exposes the "
            "resolvable source_uri (source_ref() / SourceRef field / source_uri field) "
            "an agent feeds to expand, and run_expand keeps its seq parameter optional, "
            "so a document/section-level (L2) hit whose seq is null never dead-ends at a "
            "guessed #0 (the anti-dead-end lock, PLA-297)"
        ),
        adr_origin="PLA-297 — expand-acceptable locator contract",
        tags=("agent-affordance",),
        # AST scan of a FIXED registry of agent-surface modules + the expand
        # entry point under kairix/use_cases/**; any could drop the contract.
        # Always run.
        staged_class="always-run",
    ),
    # ----- identity & attribution (CORE — Autonomous Delivery Platform SP-A) --
    # Two shared CORE checks adopted from three-cubes-fitness (v0.7.1) that make
    # machine-enforced clean authorship (decision D1) LIVE in kairix. Both are
    # guard-forward (decision D2): pre-cutover residue/history is grandfathered,
    # only NET-NEW work is gated. Their config (scan roots / allow-list / cutover)
    # lives in the CODEOWNERS-gated control plane (pyproject.toml +
    # scripts/checks/_core_bindings.py) so an agent cannot self-exempt.
    RuleEntry(
        id="SGO-156",
        gate="no-llm-attribution",
        check="core:no_llm_attribution",
        category="repo-hygiene",
        scope="per-file",
        summary=(
            "no AI/LLM self-attribution residue in first-party source/docs — no model "
            "co-author trailer, no AI-vendor no-reply author identity, no robot emoji "
            "(the metadata agent tools stamp onto commits); agent work is authored by the "
            "canonical bot/human, never advertised as model-generated (decision D1)"
        ),
        adr_origin="SGO-156 — Autonomous Delivery Platform SP-A (identity & attribution)",
        tags=("process",),
        # Literal signature scan across the configured first-party source/docs roots;
        # any authored file could carry residue. Always run (guard-forward via the
        # per-file baseline, decision D2).
        staged_class="always-run",
    ),
    RuleEntry(
        id="SGO-158",
        gate="canonical-commit-identity",
        check="core:canonical_commit_identity",
        category="process",
        scope="per-commit",
        summary=(
            "every commit author AND committer over the PR range (cutover..HEAD) carries an "
            "allow-listed identity — the canonical three-cubes-agent App, the named human "
            "maintainer, and the platform merge/bot committers — so an off-allowlist or "
            "marker-in-name identity can't slip in; guard-forward via cutover_ref (decision D2)"
        ),
        adr_origin="SGO-158 — Autonomous Delivery Platform SP-A (identity & attribution)",
        tags=("process",),
        # Range check over git log (no file surface); a staged file can't scope it.
        # Always run.
        staged_class="always-run",
    ),
    RuleEntry(
        id="SGO-270",
        gate="harness-canon-reference",
        check="core:harness_canon_reference",
        category="process",
        scope="cross-cutting",
        summary=(
            "the root harness references the central engineering canon — as a product "
            "repo, kairix carries the full root harness set (CLAUDE.md, AGENTS.md, "
            "RESOLVER.md, ETHOS.md, SCORECARD.md, CONTRIBUTING.md) and at least one "
            "harness file carries BOTH the `Canonical standards` marker AND a link to "
            "tc-pipelines governance/STANDARDS, so agents route to canon instead of "
            "re-deriving a parallel standard (Golden Path convergence)"
        ),
        adr_origin="SGO-270 Wave 2 — harness canon alignment",
        tags=("process",),
        # Inspects a FIXED set of root harness files (presence + reference); no staged
        # file scopes it. Always run. Drift detection stays OFF (no banner_path).
        staged_class="always-run",
    ),
    RuleEntry(
        id="SGO-269",
        gate="ci-consumes-shared-gate",
        check="core:ci_consumes_shared_gate",
        category="process",
        scope="cross-cutting",
        summary=(
            "kairix's CI consumes the ONE shared quality gate — Stage-0 arch-fitness "
            "runs the `tc-fitness run` engine via the python-quality-gate reusable — "
            "rather than a hand-rolled fork, so the single standard is enforced "
            "mechanically (Golden Path convergence)"
        ),
        adr_origin="SGO-269 — single-standard enforcement gate",
        tags=("process",),
        # Inspects .github/workflows (reusable consumption / engine invocation); no
        # staged file scopes it. Always run.
        staged_class="always-run",
    ),
    RuleEntry(
        id="F99",
        gate="f99",
        check="f99_usage_guide_currency",
        category="agent-affordance",
        scope="cross-cutting",
        summary=(
            "bundled usage guide stays in sync with the tool registry — every capability in "
            "tool_capabilities() (each registered MCP tool + CLI subcommand) is discoverable in "
            "kairix/agents/usage_guide/data/agent-usage-guide.md via its CLI / bare <mcp_tool> wire-name "
            "token (the guide is generated from CAPABILITIES_CATALOG with the bare names), so "
            "an agent self-training from the guide can find every shipped tool (the drift class that "
            "let `expand` fall out); flag-gated-off surfaces are excluded (PLA-299 / PLA-321)"
        ),
        adr_origin="PLA-299 — usage-guide ↔ tool-registry currency",
        tags=("agent-affordance",),
        # Relational: a new _cap(...) row in the MCP server registry OR a
        # rewrite of the bundled guide can break currency. Run when either
        # side is touched.
        staged_class="relational",
        staged_scope=(
            "kairix/agents/mcp/server.py",
            "kairix/agents/usage_guide/data/agent-usage-guide.md",
        ),
    ),
    # ----- go-discipline (active when services/*/go.mod exists) -----------
    RuleEntry(
        id="G1",
        gate="go-version-flag",
        check="go_version_flag",
        category="go-discipline",
        scope="per-file",
        summary="every Go binary exposes --version",
        staged_scope=("services",),
    ),
    RuleEntry(
        id="G6",
        gate="go-no-panic-outside-main",
        check="go_no_panic_outside_main",
        category="go-discipline",
        scope="per-file",
        summary="no panic outside main/init",
        staged_scope=("services",),
    ),
    RuleEntry(
        id="G8",
        gate="go-logging-discipline",
        check="go_logging_discipline",
        category="go-discipline",
        scope="per-file",
        summary="logging via log/slog (no log.Println or fmt.Println in service code)",
        staged_scope=("services",),
    ),
    RuleEntry(
        id="G9",
        gate="go-readme-coverage",
        check="go_readme_coverage",
        category="go-discipline",
        scope="per-plugin",
        summary="every services/<name>/ has a README.md",
        # Relational within services/: a new service dir needs a README; a
        # deleted README breaks coverage.
        staged_class="relational",
        staged_scope=("services",),
    ),
    RuleEntry(
        id="G10",
        gate="go-dependency-rationale",
        check="go_dependency_rationale",
        category="go-discipline",
        scope="per-plugin",
        summary="dependency-rationale registry per services/<name>/DEPENDENCIES.md",
        # Relational within services/: a new service / new dep needs a
        # DEPENDENCIES.md entry; a deleted registry breaks it.
        staged_class="relational",
        staged_scope=("services",),
    ),
)


CATALOGUE: dict[str, RuleEntry] = {entry.gate: entry for entry in _ENTRIES}
"""Indexed by gate name — the stable baseline-filename identifier.

Note: a few catalogue entries share the same ``gate`` deliberately
(e.g. F12 + F13 both surface through ``bdd-no-implementation-leaks``).
The dict keeps the last-wins entry per gate; callers needing all
entries should iterate :data:`ALL_ENTRIES`.
"""

ALL_ENTRIES: tuple[RuleEntry, ...] = _ENTRIES
"""Full ordered tuple of every entry — preserves duplicates by gate."""


def by_category(category: Category) -> tuple[RuleEntry, ...]:
    """Return every entry whose category matches."""
    return tuple(entry for entry in ALL_ENTRIES if entry.category == category)


def by_status(status: Status) -> tuple[RuleEntry, ...]:
    """Return every entry whose status matches."""
    return tuple(entry for entry in ALL_ENTRIES if entry.status == status)


def categories_in_use() -> tuple[Category, ...]:
    """Return every distinct category referenced by an entry, in
    declaration order."""
    seen: set[Category] = set()
    out: list[Category] = []
    for entry in ALL_ENTRIES:
        if entry.category not in seen:
            seen.add(entry.category)
            out.append(entry.category)
    return tuple(out)
