#!/usr/bin/env python3
"""Catalogue-driven fitness-function runner — kairix's pure-consumer entrypoint.

kairix is a PURE CONSUMER of the shared ``tc_fitness`` engine (EPIC #499
common-process). From v0.4.0 the engine absorbs the four injection seams kairix
used to hand-code into DECLARATIVE FACTORIES — kairix now supplies only its
*domain config* (attribute names, its ``FitnessRule`` ABC, its location-engine
branch, its exact coverage skip-text) and the engine machinery does the rest:

* ``scope_resolver`` — built by :func:`tc_fitness.staged.make_module_roots_resolver`
  from kairix's domain config (``boundary_rule_attr="RULE"``, ``abc_type=FitnessRule``,
  the location/singleton-engine ``location_marker``, and the ``("kairix",)`` walk).
  Derives a file-local rule's staged scope from its check module's own detector.
* ``enumeration_narrower`` — built by :func:`tc_fitness.staged.make_binding_narrower`
  with kairix's one residual surface (``extra_method=(FitnessRule, "enumerate_files")``).
  Narrows the per-check ``python_files`` bindings + the ABC method to the staged set.
* ``conditional_check`` — built by
  :func:`tc_fitness.runner.make_env_path_conditional_check` reading the
  Cobertura-XML path from ``KAIRIX_COVERAGE_XML`` (else repo-root ``coverage.xml``),
  skipped when ``--skip-coverage`` is passed OR the report is absent, with kairix's
  EXACT per-entry ``skip [F7]`` / ``skip [F9]`` wording via the
  ``force_skip_line_fn`` / ``absent_skip_line_fn`` callables.
* ``--skip-coverage`` — threaded through the engine's ``main_cli`` via
  ``extra_flags`` + ``post_parse`` (no forked argparse), so ``run-all.sh`` /
  ``safe-commit.sh`` keep passing it for the F7/F9 coverage stage.

The kairix-domain constants below (``_BOUNDARY_RULE_ATTR``,
``_LOCATION_ENGINE_MODULE``, ``_LOCATION_FALLBACK_ROOTS``, the coverage skip
text) STAY here and are passed as config — they must NOT move into the engine.

``run-all.sh`` and ``.pre-commit-config.yaml`` invoke this module exactly as
before: ``run_checks.py --all [--skip-coverage]`` / ``run_checks.py --staged
[--skip-coverage]`` / ``run_checks.py --gate <id>``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The shared catalogue-driven engine + the v0.4.0 declarative factories. kairix
# supplies only its domain config to these — no hand-written seam bodies remain.
from _fitness_rule import FitnessRule
from _rule_catalogue import ALL_ENTRIES
from tc_fitness.catalogue import (  # noqa: F401  # is_dispatchable re-exported for the runner tests
    RuleEntry,
    is_dispatchable,
)
from tc_fitness.context import CheckContext
from tc_fitness.gate_config import load_core_check_configs
from tc_fitness.runner import (
    ConditionalCheck,
    RunnerConfig,
    main_cli,
    make_env_path_conditional_check,
    resolve_script,
    run,
)
from tc_fitness.runner import (
    dispatches_in_process as _pkg_dispatches_in_process,
)
from tc_fitness.runner import (
    _load_check_main as _pkg_load_check_main,
)
from tc_fitness.runner import (
    _run_one_inprocess as _pkg_run_one_inprocess,
)
from tc_fitness.runner import (
    _run_one_subprocess as _pkg_run_one_subprocess,
)
from tc_fitness.runner import (
    select_all as _pkg_select_all,
)
from tc_fitness.runner import (
    select_gate as _pkg_select_gate,
)
from tc_fitness.staged import (
    StagedDecision,
    make_binding_narrower,
    make_module_roots_resolver,
    staged_in_scope,  # noqa: F401  # re-exported unchanged for the staged-selection tests
)
from tc_fitness.staged import decide as _pkg_decide
from tc_fitness.staged import resolve_staged_scope as _pkg_resolve_staged_scope

#: kairix's catalogue — the ``tuple[RuleEntry, ...]`` the shared runner reads.
RULES: tuple[RuleEntry, ...] = ALL_ENTRIES

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHECKS_DIR = REPO_ROOT / "scripts" / "checks"

_YELLOW = "\033[0;33m"
_RESET = "\033[0m"


# ── kairix-domain config for the scope-resolver factory ──────────────────────
#
# These constants are kairix's DOMAIN — they describe how kairix's check modules
# advertise their scan roots. They are passed AS CONFIG into the engine's
# ``make_module_roots_resolver`` factory; the engine bakes in none of them.

# The boundary engine shims expose a module-level ``RULE`` carrying ``roots``.
_BOUNDARY_RULE_ATTR = "RULE"
# The location/singleton engine (F29 / F38 / F61) always walks ``kairix/``
# regardless of each rule's ``allowed_roots`` allow-list, so a check that imports
# it is scoped to the production package.
_LOCATION_ENGINE_MODULE = "_location_engine"
#: The scan roots a location/singleton-engine check is scoped to.
_LOCATION_FALLBACK_ROOTS: tuple[str, ...] = ("kairix",)


def _location_marker(module: object) -> tuple[str, ...] | None:
    """kairix's :data:`~tc_fitness.staged.LocationMarker`: a location/singleton
    -engine check (F29 / F38 / F61) is scoped to the production package.

    The shims all ``from _location_engine import LocationRule``, so the imported
    ``LocationRule`` name resolving to the engine's class is the distinguishing
    signal. ``None`` when the module is not a location-engine shim (the import
    -boundary shims import ``ImportBoundaryRule`` instead and are handled by the
    factory's ``boundary_rule_attr`` branch upstream of this marker).
    """
    location_rule = getattr(module, "LocationRule", None)
    if location_rule is not None and getattr(location_rule, "__module__", "") == _LOCATION_ENGINE_MODULE:
        return _LOCATION_FALLBACK_ROOTS
    return None


#: kairix's scope resolver — the engine factory built from kairix's domain config.
#: Replaces the hand-written ``_kairix_scope_resolver`` / ``_roots_from_module``:
#: reads, in order of specificity, a module-level ``RULE.roots`` (import-boundary
#: shims), the check's OWN ``FitnessRule`` subclass's ``roots`` (the ABC checks),
#: then the location-engine ``("kairix",)`` branch; ``None`` for a ``.sh``
#: detector or an unresolvable module (→ fail-safe run).
_scope_resolver = make_module_roots_resolver(
    boundary_rule_attr=_BOUNDARY_RULE_ATTR,
    abc_type=FitnessRule,
    location_marker=_location_marker,
    fallback_roots=None,
    checks_dir=CHECKS_DIR,
)


# ── kairix-resolver-bound staged-selection back-compat shims ─────────────────
#
# tests/checks/test_staged_selection.py drives the kairix runner through these
# module-level symbols. They delegate to the shared package, binding kairix's
# factory-built ``_scope_resolver`` as the default so the tests keep asserting
# kairix's exact scope derivation without threading the resolver through.


def decide(
    entry: RuleEntry,
    script: str,
    staged: list[str],
    resolver: Any = _scope_resolver,
) -> StagedDecision:
    """Decide whether — and over what — to run ``entry`` given ``staged``,
    binding kairix's scope resolver by default (the shared ``decide`` semantics)."""
    return _pkg_decide(entry, script, staged, resolver)


def resolve_staged_scope(
    entry: RuleEntry,
    script: str,
    resolver: Any = _scope_resolver,
) -> tuple[str, ...] | None:
    """The repo-relative path-prefix scope for ``entry`` under ``script``,
    binding kairix's scope resolver by default."""
    return _pkg_resolve_staged_scope(entry, script, resolver)


# ── kairix-domain config for the enumeration-narrower factory ────────────────
#
# The engine's ``make_binding_narrower`` narrows every already-imported
# ``check_*`` module's by-value ``python_files`` binding; kairix's one residual
# surface is the ``FitnessRule.enumerate_files`` ABC method the ~25 FitnessRule
# subclasses inherit. The ``(type, method_name)`` pair is CONFIG.
_enumeration_narrower = make_binding_narrower(extra_method=(FitnessRule, "enumerate_files"))


# ── kairix-domain config for the conditional-check factory ───────────────────
#
# The F7/F9 coverage check declares ``subprocess_arg_env="KAIRIX_COVERAGE_XML"``,
# so the engine dispatches it as a guarded subprocess with the resolved
# Cobertura-XML path appended — UNLESS this hook says skip. kairix's EXACT
# per-entry skip text is reproduced via the per-entry ``*_skip_line_fn``
# callables (so F7 and F9, both ``check_per_file_coverage.py``, emit distinct
# ``skip [F7]`` / ``skip [F9]`` ledgers).

_COVERAGE_ENV_VAR = "KAIRIX_COVERAGE_XML"
_COVERAGE_DEFAULT_REL = "coverage.xml"


def _force_skip_lines(entry: RuleEntry) -> tuple[str, ...]:
    """The ``--skip-coverage`` skip text for ``entry`` (the run-all.sh /
    safe-commit.sh inner-loop path)."""
    return (f"{_YELLOW}skip [{entry.id}]{_RESET} {resolve_script(entry)} — --skip-coverage",)


def _absent_skip_lines(entry: RuleEntry) -> tuple[str, ...]:
    """The "coverage report not found" skip text for ``entry`` + the run hint."""
    return (
        f"{_YELLOW}skip [{entry.id}]{_RESET} {resolve_script(entry)} — coverage report not found",
        "   run: pytest --cov=kairix --cov-report=xml first, then re-run this check.",
    )


def _make_conditional_check(*, skip_coverage: bool) -> ConditionalCheck:
    """Build the F7/F9 coverage ``ConditionalCheck`` via the engine factory,
    bound to ``--skip-coverage`` and reproducing kairix's exact per-entry skip
    lines. ``KAIRIX_COVERAGE_XML`` wins (safe-commit's per-invocation artifact);
    else the repo-root ``coverage.xml``; skip if neither exists."""
    return make_env_path_conditional_check(
        env_var=_COVERAGE_ENV_VAR,
        default_rel=_COVERAGE_DEFAULT_REL,
        repo_root=REPO_ROOT,
        force_skip=lambda: skip_coverage,
        force_skip_line_fn=_force_skip_lines,
        absent_skip_line_fn=_absent_skip_lines,
    )


# ── paved-road footer (the affordance line under a FAIL) ─────────────────────


def _paved_road_footer(entry: RuleEntry) -> str | None:
    """The affordance line the shared runner prints under a FAIL when the rule
    carries a curated ``exemplar`` — points the agent at the query surface."""
    if entry.exemplar:
        return f"  paved-road: python3 scripts/checks/rules.py --rule {entry.id}"
    return None


# ── shared injection-seam kwargs (one place; threaded into every dispatch) ───


# ── CORE-check config (the [tool.tc_fitness.core_checks.*] blocks) ───────────
#
# A ``core:<module>`` catalogue row resolves to an engine CORE check that scans
# kairix's tree with kairix's CONFIG (roots / extensions / thresholds), read
# from pyproject.toml's [tool.tc_fitness.core_checks.<module>] block (mirrored in
# scripts/checks/_core_bindings.py). Threading this through every dispatch keeps
# ``run_checks.py`` IDENTICAL to ``tc-fitness run`` — without it a core: row would
# fall back to its class-default roots () and scan the WHOLE repo instead of the
# configured subtree.
_CORE_CHECK_CONFIGS = load_core_check_configs(REPO_ROOT)


def _seam_kwargs(*, skip_coverage: bool) -> dict[str, Any]:
    """The injection seams the shared runner consumes, as a kwargs dict — every
    one now an engine-built factory fed kairix's domain config."""
    return {
        "repo_root": REPO_ROOT,
        "checks_dir": CHECKS_DIR,
        "scope_resolver": _scope_resolver,
        "enumeration_narrower": _enumeration_narrower,
        "paved_road_footer": _paved_road_footer,
        "conditional_check": _make_conditional_check(skip_coverage=skip_coverage),
        "core_check_configs": _CORE_CHECK_CONFIGS,
    }


# ── back-compat surface for the runner / staged-selection unit tests ─────────
#
# tests/checks/test_catalogue_runner.py + tests/checks/test_staged_selection.py
# + tests/architecture/test_rules_query.py drive the kairix runner through these
# module-level symbols. They delegate to the shared package (binding kairix's
# scope_resolver where the package signature takes one), so the tests keep
# asserting kairix's exact selection + dispatch behaviour. These shims are
# repointed off in a separate Wave 3 task; they do not affect the ledger output.


def _select_all() -> list[RuleEntry]:
    """In-scope rules for ``--all``: dispatchable AND ``run_all``."""
    return _pkg_select_all(RULES)


def _select_gate(gate_id: str) -> list[RuleEntry]:
    """Rules whose catalogue ``id`` matches ``gate_id`` (case-insensitive)."""
    return _pkg_select_gate(RULES, gate_id)


def _dispatches_in_process(entry: RuleEntry) -> bool:
    """True iff ``entry``'s check runs in-process (pure-python, no runtime arg)."""
    return _pkg_dispatches_in_process(entry)


def _load_check_main(script: str) -> Any:
    """Import ``script``'s check module and return a zero-arg ``main`` invoker."""
    return _pkg_load_check_main(script)


def _footer_config() -> RunnerConfig:
    """A minimal ``RunnerConfig`` carrying the paved-road footer hook + the
    coverage conditional-check seam — used by the per-rule back-compat shims so
    a FAILING rule with an exemplar still prints the affordance line."""
    return RunnerConfig(
        repo_root=REPO_ROOT,
        checks_dir=CHECKS_DIR,
        paved_road_footer=_paved_road_footer,
        conditional_check=_make_conditional_check(skip_coverage=True),
    )


def _run_one_inprocess(entry: RuleEntry, ctx: CheckContext) -> int:
    """Dispatch ``entry``'s pure-python check IN-PROCESS (the shared impl).

    The shared ``_run_one_inprocess`` takes the resolved ``RunnerConfig`` (for
    the paved-road footer hook), not a bare context — the parse cache is shared
    via the surrounding ``ctx.install()`` the caller wraps this in (the ``ctx``
    argument is kept for the existing test signature)."""
    return _pkg_run_one_inprocess(entry, _footer_config())


def _run_one(entry: RuleEntry, *, skip_coverage: bool) -> int | None:
    """Back-compat: dispatch ``entry`` once and print its named verdict +
    paved-road footer — the per-rule output-contract surface
    tests/architecture/test_rules_query.py drives. Routes through the shared
    runner's in-process or subprocess path depending on the rule's shape;
    returns 0 pass / 1 fail / ``None`` skip."""
    cfg = RunnerConfig(
        repo_root=REPO_ROOT,
        checks_dir=CHECKS_DIR,
        paved_road_footer=_paved_road_footer,
        conditional_check=_make_conditional_check(skip_coverage=skip_coverage),
    )
    if _pkg_dispatches_in_process(entry):
        ctx = CheckContext(repo_root=REPO_ROOT)
        with ctx.install():
            return _pkg_run_one_inprocess(entry, cfg)
    return _pkg_run_one_subprocess(entry, cfg)


def _staged_decisions(staged: list[str]) -> list[tuple[RuleEntry, StagedDecision]]:
    """Per-rule staged decision for every ``--all`` entry, in catalogue order,
    deduped by resolved script — binding kairix's scope resolver."""
    out: list[tuple[RuleEntry, StagedDecision]] = []
    seen_scripts: set[str] = set()
    for entry in _select_all():
        script = resolve_script(entry)
        if script in seen_scripts:
            continue
        seen_scripts.add(script)
        out.append((entry, decide(entry, script, staged, _scope_resolver)))
    return out


def _rule_touches_staged(entry: RuleEntry, staged: list[str]) -> bool:
    """Back-compat shim: True iff ``entry`` would be DISPATCHED for ``staged``."""
    return decide(entry, resolve_script(entry), staged, _scope_resolver).run


def _dispatch_staged(staged: list[str], *, skip_coverage: bool) -> int:
    """Precise staged dispatch — delegates to the shared runner's staged mode
    with kairix's seams. Returns the process exit code (0 clean, 1 any fail).

    Kept as a kairix-internal entry point for tests/checks/test_staged_selection.py,
    which drives the real staged dispatch over probe files."""
    return run(RULES, mode="staged", staged_files=staged, **_seam_kwargs(skip_coverage=skip_coverage)).exit_code


# ── CLI entrypoint ───────────────────────────────────────────────────────────


def _post_parse(args: argparse.Namespace) -> dict[str, Any]:
    """Map the parsed namespace to the EXTRA ``run()`` kwargs the engine threads
    into dispatch: build the coverage conditional-check seam from the
    kairix-specific ``--skip-coverage`` flag."""
    return {"conditional_check": _make_conditional_check(skip_coverage=args.skip_coverage)}


def main(argv: list[str] | None = None) -> int:
    """Parse ``--all`` / ``--staged`` / ``--gate`` + ``--skip-coverage`` and
    dispatch through the shared engine's ``main_cli`` with kairix's seams.

    ``--skip-coverage`` is a kairix-specific flag the engine threads in via
    ``extra_flags`` + ``post_parse`` (no forked argparse): ``run-all.sh`` /
    ``safe-commit.sh`` pass it for the F7/F9 coverage stage."""
    return main_cli(
        RULES,
        argv,
        repo_root=REPO_ROOT,
        checks_dir=CHECKS_DIR,
        scope_resolver=_scope_resolver,
        enumeration_narrower=_enumeration_narrower,
        paved_road_footer=_paved_road_footer,
        core_check_configs=_CORE_CHECK_CONFIGS,
        extra_flags=[("--skip-coverage", {"action": "store_true"})],
        post_parse=_post_parse,
    )


if __name__ == "__main__":
    sys.exit(main())
