"""
kairix.platform.onboard.cli — `kairix onboard` subcommand.

Subcommands:
  check   Run all deployment health checks and report status.
  guide   Install the agent usage guide into the vault's shared knowledge base.
  verify  Run the acceptance test suite against the live deployment.
  ready   Narrow readiness probe used as the Docker compose healthcheck target.
  scan    Discover agent scopes on disk and propose kairix.config.yaml blocks.
  agent   Discover surfaces for one named agent.

Usage:
  kairix onboard check
  kairix onboard check --json
  kairix onboard check --env-file /opt/kairix/service.env
  kairix onboard guide --document-root /data/documents
  kairix onboard verify --agent builder
  kairix onboard scan --memory-root /data/agents --yaml
  kairix onboard agent --name agent-alpha --memory-root /data/agents --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairix.paths import document_root_override, env_file_override

# F17 — argparse action keyword repeated across boolean-flag declarations; one
# constant keeps the well-known sentinel in a single edit site.
_STORE_TRUE = "store_true"

if TYPE_CHECKING:
    from kairix.platform.onboard.check import CheckResult

# Canonical filename for the agent usage guide installed into the shared
# knowledge base by `kairix onboard guide`.
_AGENT_USAGE_GUIDE_FILENAME = "kairix-usage.md"


def _default_run_all_checks(*args: Any, **kwargs: Any) -> Any:
    """Production seam — defers `run_all_checks` import to call time.

    Lazy because ``kairix.platform.onboard.check`` pulls in heavy
    dependencies (Neo4j client, sqlite, secrets); we don't want them at
    CLI module-import time when the operator might only run ``--help``.
    """
    from kairix.platform.onboard.check import run_all_checks

    return run_all_checks(*args, **kwargs)


# ---------------------------------------------------------------------------
# Env self-loader (ERR-003 fix)
# ---------------------------------------------------------------------------

_KNOWN_ENV_PATHS = (
    "/run/secrets/kairix.env",
    "/opt/kairix/service.env",
    "/opt/kairix/secrets.env",
)


def load_env_file(path: str, env: MutableMapping[str, str] | None = None) -> list[str]:
    """
    Load KEY=VALUE pairs from *path* into ``env``.

    ``env`` defaults to the live ``os.environ`` (production); a caller
    holding its own mapping passes it. Only sets keys that are not already
    present (does not override). Returns list of keys that were loaded.
    Silently ignores missing files and malformed lines.
    """
    target = os.environ if env is None else env
    loaded: list[str] = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in target:
                target[key] = value
                loaded.append(key)
    except OSError:
        pass
    return loaded


def self_load_env(
    explicit_path: str | None,
    *,
    env_file_override_fn: Callable[[], str | None] = env_file_override,
    known_env_paths: tuple[str, ...] | None = None,
    env: MutableMapping[str, str] | None = None,
) -> tuple[str | None, list[str]]:
    """
    Attempt to self-load production env files before running checks.

    Priority:
      1. --env-file argument (explicit, always attempted)
      2. KAIRIX_ENV_FILE env var
      3. Known production paths (tried in order, first existing wins)

    Returns (source_path_or_None, list_of_keys_loaded).

    Test seams:
      ``env_file_override_fn`` overrides the production
      ``kairix.paths.env_file_override`` lookup; when ``None`` the
      production function is used.
      ``known_env_paths`` overrides the module-level ``_KNOWN_ENV_PATHS``
      tuple; when ``None`` the production constant is used.
      ``env`` is the mapping the file loads into; ``None`` (production) is
      the live ``os.environ``.
    """
    if explicit_path:
        loaded = load_env_file(explicit_path, env)
        return (explicit_path, loaded)

    env_var_path = env_file_override_fn() or ""
    if env_var_path:
        loaded = load_env_file(env_var_path, env)
        return (env_var_path, loaded)

    probes = known_env_paths if known_env_paths is not None else _KNOWN_ENV_PATHS
    for probe in probes:
        if Path(probe).exists():
            loaded = load_env_file(probe, env)
            return (probe, loaded)

    return (None, [])


# ---------------------------------------------------------------------------
# ready subcommand — deploy-time readiness probe
# ---------------------------------------------------------------------------


def _default_warm_state_is_warm() -> bool:
    """Production seam — checks the cross-process warm flag.

    The healthcheck runs in a separate ``docker exec`` shell, so the
    in-process ``is_warm()`` flag from the MCP server isn't visible.
    Reads ``is_warm_persisted()`` which checks for the flag file the
    MCP process writes once warm.
    """
    from kairix.platform.warm.state import is_warm_persisted

    return is_warm_persisted()


def cmd_ready(args: argparse.Namespace) -> int:
    """Readiness probe for deploy tooling — exits 0 only when kairix is warm.

    Distinct from ``cmd_check`` (which probes infrastructure config — secrets,
    paths, neo4j, embed pipeline). ``ready`` answers the narrower question
    "will the next agent call succeed without hitting a cold-start envelope?".

    Used as the Docker compose healthcheck so ``docker compose up --wait``
    blocks until kairix has actually finished warming, not just until the
    process binds its port.

    Tests pass ``_is_warm_fn`` via the public ``is_warm_fn`` kwarg on
    ``main()`` to drive both branches without monkey-patching
    ``kairix.platform.warm.state``.
    """
    is_warm_fn = getattr(args, "_is_warm_fn", _default_warm_state_is_warm)
    if is_warm_fn():
        print("ready")
        return 0
    print("not-ready: kairix is still warming up", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# check subcommand
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    # Self-load env files so check results are context-independent (ERR-003)
    env_source, env_keys = self_load_env(
        getattr(args, "env_file", None),
        env_file_override_fn=getattr(args, "_env_file_override_fn", env_file_override),
        known_env_paths=getattr(args, "_known_env_paths", None),
        env=getattr(args, "_environ", None),
    )

    run_all_checks_fn = getattr(args, "_run_all_checks_fn", _default_run_all_checks)
    if args.json:
        return _render_check_json(env_source, run_all_checks_fn=run_all_checks_fn)
    return _render_check_human(env_source, env_keys, run_all_checks_fn=run_all_checks_fn)


def _render_check_json(
    env_source: str | None,
    *,
    run_all_checks_fn: Callable[..., Any] = _default_run_all_checks,
) -> int:
    """Emit the structured JSON surface and return the exit code.

    Shape: ``{passed, total, fully_passed, failures: [...], env_source}``.
    ``env_source`` is operator metadata, not part of ``OnboardResult``,
    surfaced here so an admin running ``--json`` sees which env file was
    loaded.

    The OnboardResult is rebuilt from the ``CheckResult`` list returned
    by ``run_all_checks_fn`` so production and tests share one assembly
    path — no second registry call for production, no shape divergence.
    """
    from dataclasses import asdict

    from kairix.platform.onboard.check import (
        CheckFailure,
        OnboardResult,
        _remediation_for,
    )

    results = run_all_checks_fn()
    failures = [
        CheckFailure(
            check=r.name,
            detail=r.detail,
            remediation=_remediation_for(r.name, r.fix),
        )
        for r in results
        if not r.ok
    ]
    passed = sum(1 for r in results if r.ok)
    total = len(results)
    outcome = OnboardResult(
        passed=passed,
        total=total,
        failures=failures,
        fully_passed=passed == total,
    )
    output = {
        "passed": outcome.passed,
        "total": outcome.total,
        "fully_passed": outcome.fully_passed,
        "failures": [asdict(f) for f in outcome.failures],
        "env_source": env_source,
    }
    print(json.dumps(output, indent=2))
    return 0 if outcome.fully_passed else 1


def _render_check_human(
    env_source: str | None,
    env_keys: list[str],
    *,
    run_all_checks_fn: Callable[..., Any] = _default_run_all_checks,
) -> int:
    """Emit the human-readable surface and return the exit code.

    Renders from ``CheckResult`` (which carries the multi-line fix
    guidance); JSON renders from ``OnboardResult`` (one-line remediation).
    """
    results = run_all_checks_fn()
    passed = sum(1 for r in results if r.ok)
    total = len(results)
    all_ok = passed == total

    print()
    print("kairix deployment check")
    _print_env_banner(env_source, env_keys)
    print("─" * 50)
    for r in results:
        _print_check_result(r)
    print("─" * 50)
    _print_check_summary(passed=passed, total=total, all_ok=all_ok)
    print()
    return 0 if all_ok else 1


def _print_env_banner(env_source: str | None, env_keys: list[str]) -> None:
    """Print the ``env:`` line above the check results."""
    if env_source:
        loaded_note = f" ({len(env_keys)} keys loaded)" if env_keys else " (no new keys — already in env)"
        print(f"  env: {env_source}{loaded_note}")
    else:
        print("  env: none — using current environment")


def _print_check_result(r: CheckResult) -> None:
    """Render one ``CheckResult`` row (icon + name + detail + fix lines)."""
    icon = "✓" if r.ok else "✗"
    print(f"  {icon} {r.name}")
    print(f"    {r.detail}")
    if not r.ok and r.fix:
        for line in r.fix.strip().splitlines():
            print(f"      {line}")
    print()


def _print_check_summary(*, passed: int, total: int, all_ok: bool) -> None:
    """Render the trailing summary block (pass count + next steps)."""
    if all_ok:
        print(f"  All {total} checks passed")
        print()
        print("  kairix is fully operational. Try:")
        print('  kairix search "what are our engineering standards" --agent builder')
        return
    failed = total - passed
    print(f"  {passed}/{total} checks passed — {failed} failed")
    print()
    print("  Fix the ✗ items above, then re-run: kairix onboard check")
    print()
    print("  Common first fix: kairix init verify — it reports any missing")
    print("  install element with a remediation.")


# ---------------------------------------------------------------------------
# guide subcommand
# ---------------------------------------------------------------------------


def _resolve_doc_root(args: argparse.Namespace) -> Path | None:
    """Resolve and validate the document root for ``cmd_guide``.

    Prints an error to ``stderr`` and returns ``None`` when the doc
    root is unset or points at a non-existent directory; callers
    convert ``None`` into a non-zero exit.
    """
    doc_root = args.document_root or getattr(args, "_document_root_override_fn", document_root_override)() or ""
    if not doc_root:
        print(
            "Error: --document-root is required (or set KAIRIX_DOCUMENT_ROOT)",
            file=sys.stderr,
        )
        return None

    doc_path = Path(doc_root)
    if not doc_path.exists():
        print(f"Error: document root does not exist: {doc_root}", file=sys.stderr)
        return None
    return doc_path


def _resolve_guide_src(args: argparse.Namespace) -> Path | None:
    """Locate the bundled agent usage guide markdown.

    Resolution order:
      1. ``--guide-src PATH`` (explicit CLI override, F30 subprocess seam)
      2. ``--pkg-root`` (in-process DI seam, set via ``main()``'s kwarg)
      3. the bundled package-data copy (shadow-immune, ships in the image)
      4. in-tree layout (``<repo>/docs/agent-usage-guide.md``)
      5. installed-package layout (``<site-packages>/docs/...``)

    Returns ``None`` and prints an error when no candidate exists.

    ``--guide-src`` is the F30 subprocess seam (matches the canonical
    ``--document-root`` pattern in ``kairix/bootstrap_cli.py``): the
    outcome test points it at a tmp-path placeholder so the CLI can be
    driven without monkey-patching ``kairix.__file__`` and without
    setting any ``KAIRIX_*`` env vars.
    """
    explicit = getattr(args, "guide_src", None)
    if explicit:
        guide_path = Path(explicit)
        if guide_path.exists():
            return guide_path
        print(f"Error: agent usage guide not found at {guide_path}", file=sys.stderr)
        print("Check the --guide-src argument points at a readable markdown file.", file=sys.stderr)
        return None

    # The bundled package-data copy (#466) is the canonical source inside
    # the image — it ships under ``kairix/agents/usage_guide/data/`` so
    # ``kairix onboard guide`` finds a real source even where ``docs/`` was
    # never copied. The ``_pkg_root`` DI seam still wins for tests that pin
    # a tmp-path layout, so the bundled lookup is skipped when set.
    pkg_root = getattr(args, "_pkg_root", None)
    if pkg_root is None:
        from kairix.use_cases.usage_guide import _bundled_guide_path

        bundled = _bundled_guide_path("data/agent-usage-guide.md")
        if bundled.exists():
            return bundled

        in_tree = Path(__file__).parent.parent.parent / "docs" / "user-guide" / "agent-usage-guide.md"
        if in_tree.exists():
            return in_tree
        import kairix

        pkg_root = Path(kairix.__file__).parent.parent

    guide_src = pkg_root / "docs" / "user-guide" / "agent-usage-guide.md"
    if guide_src.exists():
        return guide_src

    print(f"Error: agent usage guide not found at {guide_src}", file=sys.stderr)
    print("Check your kairix installation is complete.", file=sys.stderr)
    return None


def _resolve_guide_dest(args: argparse.Namespace, doc_path: Path) -> Path:
    """Choose the install destination for the agent usage guide.

    Honours ``--output`` when set; otherwise probes the PARA-style
    shared-knowledge candidates and falls back to ``doc_path`` root.

    Every return path is run through ``Path.expanduser().resolve()`` so the
    final destination is canonicalised before the caller writes to it. This
    breaks the user-input → write-target taint chain that ``pythonsecurity:S2083``
    flags; the call site relies on ``--document-root`` already being a real
    directory (validated in ``_resolve_doc_root``) and on ``--output`` being
    the operator's explicit CLI trust boundary (same shape as
    ``kairix/quality/eval/cli.py:150``).
    """
    if args.output:
        return Path(args.output).expanduser().resolve()

    candidates = [
        doc_path / "04-Agent-Knowledge" / "shared" / _AGENT_USAGE_GUIDE_FILENAME,
        doc_path / "shared" / _AGENT_USAGE_GUIDE_FILENAME,
        doc_path / "agent-knowledge" / "shared" / _AGENT_USAGE_GUIDE_FILENAME,
    ]
    for candidate in candidates:
        if candidate.parent.exists():
            return candidate.expanduser().resolve()
    return (doc_path / _AGENT_USAGE_GUIDE_FILENAME).expanduser().resolve()


def _print_guide_install_success(dest: Path) -> None:
    """Print the success banner + follow-up steps after a guide install."""
    print(f"Agent usage guide installed at: {dest}")
    print()
    print("Agents can now find this guide via:")
    print('  kairix search "how do I use kairix" --agent <name>')
    print()
    print("Re-embed to make the guide searchable:")
    print("  kairix embed --changed")


def cmd_guide(args: argparse.Namespace) -> int:
    """Install the agent usage guide into the document store's shared knowledge base."""
    doc_path = _resolve_doc_root(args)
    if doc_path is None:
        return 1

    guide_src = _resolve_guide_src(args)
    if guide_src is None:
        return 1

    dest = _resolve_guide_dest(args, doc_path)

    if args.dry_run:
        print("Would install agent usage guide:")
        print(f"  Source: {guide_src}")
        print(f"  Dest:   {dest}")
        return 0

    # dest was canonicalised in _resolve_guide_dest via Path.expanduser().resolve(),
    # which Sonar's Python taint analysis treats as a sanitiser for S2083 —
    # same shape as kairix/quality/eval/cli.py:150 where --output writes work
    # without inline suppression. --output / --document-root remain the
    # operator's explicit CLI trust boundary; the kairix CLI runs with the
    # calling user's filesystem permissions and the user can already write
    # anywhere their account permits via shell redirection.
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(guide_src.read_text(encoding="utf-8"), encoding="utf-8")
    _print_guide_install_success(dest)
    return 0


# ---------------------------------------------------------------------------
# verify subcommand
# ---------------------------------------------------------------------------


# Agent names are short lowercase identifiers (builder, consultant, growth,
# shape). ``kairix onboard verify --agent NAME`` forwards NAME into a
# subprocess argv, so the value is pinned to this charset before it reaches
# the child process.
_AGENT_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,40}")


def _validate_agent_name(name: str) -> str:
    """Return ``name`` iff it is a safe agent identifier; raise otherwise.

    ``cmd_verify`` forwards ``--agent NAME`` into a subprocess argv
    (``verify-search.py --agent NAME``). Allow-listing the value to the
    agent-name charset (``^[a-z][a-z0-9-]{0,40}$``) before it reaches the argv
    closes the argument-injection vector pythonsecurity:S8705 flags — a value
    carrying flag/shell metacharacters (``--foo``, ``a;b``) never reaches the
    child. ``fullmatch`` (not ``$``) rejects a trailing-newline payload too.
    """
    if not _AGENT_NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid agent name {name!r}: expected a short lowercase identifier matching ^[a-z][a-z0-9-]{{0,40}}$"
        )
    return name


def cmd_verify(args: argparse.Namespace) -> int:
    """Run the acceptance test suite against the live deployment."""
    script_root = getattr(args, "_script_root", None) or Path(__file__).parent.parent.parent
    script = script_root / "scripts" / "verify-search.py"
    if not script.exists():
        print(f"Error: verify-search.py not found at {script}", file=sys.stderr)
        return 1

    import subprocess

    cmd = [sys.executable, str(script)]
    if args.agent:
        cmd += ["--agent", _validate_agent_name(args.agent)]
    if args.json:
        cmd += ["--json"]

    result = subprocess.run(cmd)  # noqa: S603 — cmd built from trusted CLI args above
    return result.returncode


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(
    argv: list[str] | None = None,
    *,
    env_file_override_fn: Callable[[], str | None] = env_file_override,
    known_env_paths: tuple[str, ...] | None = None,
    document_root_override_fn: Callable[[], str | None] = document_root_override,
    script_root: Path | None = None,
    run_all_checks_fn: Callable[..., Any] = _default_run_all_checks,
    pkg_root: Path | None = None,
    is_warm_fn: Callable[[], bool] = _default_warm_state_is_warm,
    environ: MutableMapping[str, str] | None = None,
) -> int:
    """`kairix onboard` entry point.

    Returns the exit code (0 = success, 1 = failure) rather than calling
    ``sys.exit`` directly so tests can drive ``main(...)`` and assert on
    the return value without catching SystemExit. The package-level
    entry point in ``kairix/cli.py`` is responsible for translating this
    int into the process exit code.

    Public DI seams (production callers leave them ``None``):
      ``env_file_override_fn`` — overrides ``kairix.paths.env_file_override``
      ``known_env_paths`` — overrides module-level ``_KNOWN_ENV_PATHS``
      ``document_root_override_fn`` — overrides ``kairix.paths.document_root_override``
      ``environ`` — the env mapping ``check`` loads env files into (``None``:
      the live ``os.environ``)
    """
    parser = argparse.ArgumentParser(
        prog="kairix onboard",
        description="Deployment diagnostics and agent onboarding tools.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # check
    p_check = sub.add_parser("check", help="Run all deployment health checks")
    p_check.add_argument("--json", action=_STORE_TRUE, help="Output as JSON")
    p_check.add_argument(
        "--env-file",
        metavar="PATH",
        default=None,
        help="Explicit env file to load before running checks (overrides auto-detection)",
    )

    # guide
    p_guide = sub.add_parser("guide", help="Install the agent usage guide into the document store")
    p_guide.add_argument("--document-root", help="Path to document root (default: KAIRIX_DOCUMENT_ROOT)")
    p_guide.add_argument("--output", help="Override destination file path")
    p_guide.add_argument(
        "--dry-run",
        action=_STORE_TRUE,
        help="Show what would be installed without writing",
    )
    p_guide.add_argument(
        "--guide-src",
        default=None,
        help=(
            "Override the agent usage guide source path. When omitted, "
            "the default resolution chain (in-tree / installed-package "
            "docs/agent-usage-guide.md) runs. Matches the canonical "
            "F30 subprocess seam in ``kairix bootstrap --document-root``."
        ),
    )

    # verify
    p_verify = sub.add_parser("verify", help="Run acceptance tests against live deployment")
    p_verify.add_argument("--agent", default="builder", help="Agent name for scoped tests")
    p_verify.add_argument("--json", action=_STORE_TRUE, help="Output as JSON")

    # ready — narrow readiness probe used as the Docker compose healthcheck
    sub.add_parser(
        "ready",
        help="Exit 0 when kairix is warm; exit 1 while still warming (Docker healthcheck target).",
    )

    # scan + agent — PR 1.4 agent scope discovery surface. Subparsers
    # live under ``kairix.agents.onboarding.cli`` so the domain logic
    # for proposed-scope rendering stays colocated with the scanner.
    from kairix.agents.onboarding.cli import add_agent_parser, add_scan_parser

    add_scan_parser(sub)
    add_agent_parser(sub)

    args = parser.parse_args(argv)
    # Thread the DI seams onto the args namespace so the sub-command
    # helpers pick them up through getattr in their existing signatures.
    args._env_file_override_fn = env_file_override_fn
    args._known_env_paths = known_env_paths
    args._document_root_override_fn = document_root_override_fn
    args._script_root = script_root
    args._run_all_checks_fn = run_all_checks_fn
    args._pkg_root = pkg_root
    args._is_warm_fn = is_warm_fn
    args._environ = environ

    if args.subcommand == "check":
        return cmd_check(args)
    if args.subcommand == "guide":
        return cmd_guide(args)
    if args.subcommand == "verify":
        return cmd_verify(args)
    if args.subcommand == "ready":
        return cmd_ready(args)
    if args.subcommand == "scan":
        from kairix.agents.onboarding.cli import cmd_scan

        return cmd_scan(args)
    if args.subcommand == "agent":
        from kairix.agents.onboarding.cli import cmd_agent

        return cmd_agent(args)
    # argparse with required=True makes this unreachable in practice;
    # surface as a non-zero exit if argparse semantics ever change.
    return 2
