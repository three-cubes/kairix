"""
kairix.platform.onboard.cli — `kairix onboard` subcommand.

Subcommands:
  check   Run all deployment health checks and report status.
  guide   Install the agent usage guide into the vault's shared knowledge base.
  verify  Run the acceptance test suite against the live deployment.

Usage:
  kairix onboard check
  kairix onboard check --json
  kairix onboard check --env-file /opt/kairix/service.env
  kairix onboard guide --document-root /data/documents
  kairix onboard verify --agent builder
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kairix.platform.onboard.check import CheckResult

# Canonical filename for the agent usage guide installed into the shared
# knowledge base by `kairix onboard guide`.
_AGENT_USAGE_GUIDE_FILENAME = "kairix-usage.md"

# ---------------------------------------------------------------------------
# Env self-loader (ERR-003 fix)
# ---------------------------------------------------------------------------

_KNOWN_ENV_PATHS = (
    "/run/secrets/kairix.env",
    "/opt/kairix/service.env",
    "/opt/kairix/secrets.env",
)


def _load_env_file(path: str) -> list[str]:
    """
    Load KEY=VALUE pairs from *path* into os.environ.

    Only sets keys that are not already present (does not override).
    Returns list of keys that were loaded.
    Silently ignores missing files and malformed lines.
    """
    loaded: list[str] = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    except OSError:
        pass
    return loaded


def _self_load_env(
    explicit_path: str | None,
    *,
    env_file_override_fn: Callable[[], str | None] | None = None,
    known_env_paths: tuple[str, ...] | None = None,
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
    """
    if explicit_path:
        loaded = _load_env_file(explicit_path)
        return (explicit_path, loaded)

    if env_file_override_fn is None:
        from kairix.paths import env_file_override as env_file_override_fn  # type: ignore[no-redef]

    env_var_path = env_file_override_fn() or ""
    if env_var_path:
        loaded = _load_env_file(env_var_path)
        return (env_var_path, loaded)

    probes = known_env_paths if known_env_paths is not None else _KNOWN_ENV_PATHS
    for probe in probes:
        if Path(probe).exists():
            loaded = _load_env_file(probe)
            return (probe, loaded)

    return (None, [])


# ---------------------------------------------------------------------------
# check subcommand
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    # Self-load env files so check results are context-independent (ERR-003)
    env_source, env_keys = _self_load_env(
        getattr(args, "env_file", None),
        env_file_override_fn=getattr(args, "_env_file_override_fn", None),
        known_env_paths=getattr(args, "_known_env_paths", None),
    )

    run_all_checks_fn = getattr(args, "_run_all_checks_fn", None)
    if args.json:
        return _render_check_json(env_source, run_all_checks_fn=run_all_checks_fn)
    return _render_check_human(env_source, env_keys, run_all_checks_fn=run_all_checks_fn)


def _render_check_json(
    env_source: str | None,
    *,
    run_all_checks_fn: Callable[..., Any] | None = None,
) -> int:
    """Emit the structured JSON surface and return the exit code.

    Shape: ``{passed, total, fully_passed, failures: [...], env_source}``.
    ``env_source`` is operator metadata, not part of ``OnboardResult``,
    surfaced here so an admin running ``--json`` sees which env file was
    loaded.

    ``run_all_checks_fn`` overrides the production check runner — tests
    pass a fake that returns a controlled list of ``CheckResult``.
    """
    from dataclasses import asdict

    from kairix.platform.onboard.check import (
        CheckFailure,
        OnboardResult,
        _remediation_for,
        run_onboard_check,
    )

    if run_all_checks_fn is not None:
        # Test seam: build the OnboardResult directly from injected
        # CheckResult instances rather than re-invoking the production
        # registry. Preserves the OnboardResult shape (passed / total /
        # fully_passed / failures with canonical remediation).
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
    else:
        outcome = run_onboard_check()
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
    run_all_checks_fn: Callable[..., Any] | None = None,
) -> int:
    """Emit the human-readable surface and return the exit code.

    Renders from ``CheckResult`` (which carries the multi-line fix
    guidance); JSON renders from ``OnboardResult`` (one-line remediation).
    """
    from kairix.platform.onboard.check import run_all_checks

    results = run_all_checks_fn() if run_all_checks_fn is not None else run_all_checks()
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
    print("  Common first fix: run scripts/deploy-vm.sh to install the wrapper")
    print("  and ensure kairix is on PATH for agent exec contexts.")


# ---------------------------------------------------------------------------
# guide subcommand
# ---------------------------------------------------------------------------


def _resolve_doc_root(args: argparse.Namespace) -> Path | None:
    """Resolve and validate the document root for ``cmd_guide``.

    Prints an error to ``stderr`` and returns ``None`` when the doc
    root is unset or points at a non-existent directory; callers
    convert ``None`` into a non-zero exit.
    """
    from kairix.paths import document_root_override as _doc_root_override

    doc_root = (
        args.document_root
        or getattr(args, "_document_root_override_fn", _doc_root_override)()
        or ""
    )
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

    Tries the in-tree source layout first, then falls back to the
    installed-package layout. Returns ``None`` and prints an error
    when neither candidate exists.
    """
    # The in-tree source layout (``<repo>/docs/agent-usage-guide.md``)
    # and the installed-package layout (``<site-packages>/docs/...``)
    # both terminate at ``Path(kairix.__file__).parent.parent``. Threading
    # ``pkg_root`` through ``args`` (set by ``main()``'s public DI seam)
    # lets tests pin a tmp-path layout without monkey-patching kairix.__file__.
    pkg_root = getattr(args, "_pkg_root", None)
    if pkg_root is None:
        in_tree = Path(__file__).parent.parent.parent / "docs" / "agent-usage-guide.md"
        if in_tree.exists():
            return in_tree
        import kairix

        pkg_root = Path(kairix.__file__).parent.parent

    guide_src = pkg_root / "docs" / "agent-usage-guide.md"
    if guide_src.exists():
        return guide_src

    print(f"Error: agent usage guide not found at {guide_src}", file=sys.stderr)
    print("Check your kairix installation is complete.", file=sys.stderr)
    return None


def _resolve_guide_dest(args: argparse.Namespace, doc_path: Path) -> Path:
    """Choose the install destination for the agent usage guide.

    Honours ``--output`` when set; otherwise probes the PARA-style
    shared-knowledge candidates and falls back to ``doc_path`` root.
    """
    if args.output:
        return Path(args.output)

    candidates = [
        doc_path / "04-Agent-Knowledge" / "shared" / _AGENT_USAGE_GUIDE_FILENAME,
        doc_path / "shared" / _AGENT_USAGE_GUIDE_FILENAME,
        doc_path / "agent-knowledge" / "shared" / _AGENT_USAGE_GUIDE_FILENAME,
    ]
    for candidate in candidates:
        if candidate.parent.exists():
            return candidate
    return doc_path / _AGENT_USAGE_GUIDE_FILENAME


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

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(guide_src.read_text(encoding="utf-8"), encoding="utf-8")
    _print_guide_install_success(dest)
    return 0


# ---------------------------------------------------------------------------
# verify subcommand
# ---------------------------------------------------------------------------


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
        cmd += ["--agent", args.agent]
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
    env_file_override_fn: Callable[[], str | None] | None = None,
    known_env_paths: tuple[str, ...] | None = None,
    document_root_override_fn: Callable[[], str | None] | None = None,
    script_root: Path | None = None,
    run_all_checks_fn: Callable[..., Any] | None = None,
    pkg_root: Path | None = None,
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
    """
    parser = argparse.ArgumentParser(
        prog="kairix onboard",
        description="Deployment diagnostics and agent onboarding tools.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # check
    p_check = sub.add_parser("check", help="Run all deployment health checks")
    p_check.add_argument("--json", action="store_true", help="Output as JSON")
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
        action="store_true",
        help="Show what would be installed without writing",
    )

    # verify
    p_verify = sub.add_parser("verify", help="Run acceptance tests against live deployment")
    p_verify.add_argument("--agent", default="builder", help="Agent name for scoped tests")
    p_verify.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args(argv)
    # Thread the DI seams onto the args namespace so the sub-command
    # helpers pick them up through getattr in their existing signatures.
    args._env_file_override_fn = env_file_override_fn  # type: ignore[attr-defined]
    args._known_env_paths = known_env_paths  # type: ignore[attr-defined]
    args._document_root_override_fn = document_root_override_fn  # type: ignore[attr-defined]
    args._script_root = script_root  # type: ignore[attr-defined]
    args._run_all_checks_fn = run_all_checks_fn  # type: ignore[attr-defined]
    args._pkg_root = pkg_root  # type: ignore[attr-defined]

    if args.subcommand == "check":
        return cmd_check(args)
    if args.subcommand == "guide":
        return cmd_guide(args)
    if args.subcommand == "verify":
        return cmd_verify(args)
    # argparse with required=True makes this unreachable in practice;
    # surface as a non-zero exit if argparse semantics ever change.
    return 2
