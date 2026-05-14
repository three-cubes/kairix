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
from pathlib import Path

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


def _self_load_env(explicit_path: str | None) -> tuple[str | None, list[str]]:
    """
    Attempt to self-load production env files before running checks.

    Priority:
      1. --env-file argument (explicit, always attempted)
      2. KAIRIX_ENV_FILE env var
      3. Known production paths (tried in order, first existing wins)

    Returns (source_path_or_None, list_of_keys_loaded).
    """
    if explicit_path:
        loaded = _load_env_file(explicit_path)
        return (explicit_path, loaded)

    from kairix.paths import env_file_override

    env_var_path = env_file_override() or ""
    if env_var_path:
        loaded = _load_env_file(env_var_path)
        return (env_var_path, loaded)

    for probe in _KNOWN_ENV_PATHS:
        if Path(probe).exists():
            loaded = _load_env_file(probe)
            return (probe, loaded)

    return (None, [])


# ---------------------------------------------------------------------------
# check subcommand
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from kairix.platform.onboard.check import run_all_checks, run_onboard_check

    # Self-load env files so check results are context-independent (ERR-003)
    env_source, env_keys = _self_load_env(getattr(args, "env_file", None))

    if args.json:
        # Structured JSON output — canonical machine-readable surface.
        # Shape: {passed, total, fully_passed, failures: [{check, detail, remediation}]}.
        outcome = run_onboard_check()
        output = {
            "passed": outcome.passed,
            "total": outcome.total,
            "fully_passed": outcome.fully_passed,
            "failures": [asdict(f) for f in outcome.failures],
            # env_source is operator metadata, not part of the OnboardResult shape;
            # surfaced here so an admin running --json sees which env file was loaded.
            "env_source": env_source,
        }
        print(json.dumps(output, indent=2))
        return 0 if outcome.fully_passed else 1

    # Human-readable output — preserves the existing CLI surface for humans.
    # We render from CheckResult (which carries the multi-line fix guidance);
    # JSON renders from OnboardResult (which carries the one-line remediation).
    results = run_all_checks()
    passed = sum(1 for r in results if r.ok)
    total = len(results)
    all_ok = passed == total

    print()
    print("kairix deployment check")
    if env_source:
        loaded_note = f" ({len(env_keys)} keys loaded)" if env_keys else " (no new keys — already in env)"
        print(f"  env: {env_source}{loaded_note}")
    else:
        print("  env: none — using current environment")
    print("─" * 50)
    for r in results:
        icon = "✓" if r.ok else "✗"
        print(f"  {icon} {r.name}")
        print(f"    {r.detail}")
        if not r.ok and r.fix:
            for line in r.fix.strip().splitlines():
                print(f"      {line}")
        print()

    print("─" * 50)
    if all_ok:
        print(f"  All {total} checks passed")
        print()
        print("  kairix is fully operational. Try:")
        print('  kairix search "what are our engineering standards" --agent builder')
    else:
        failed = total - passed
        print(f"  {passed}/{total} checks passed — {failed} failed")
        print()
        print("  Fix the ✗ items above, then re-run: kairix onboard check")
        print()
        print("  Common first fix: run scripts/deploy-vm.sh to install the wrapper")
        print("  and ensure kairix is on PATH for agent exec contexts.")
    print()

    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# guide subcommand
# ---------------------------------------------------------------------------


def cmd_guide(args: argparse.Namespace) -> int:
    """Install the agent usage guide into the document store's shared knowledge base."""
    from kairix.paths import document_root_override

    doc_root = args.document_root or document_root_override() or ""
    if not doc_root:
        print(
            "Error: --document-root is required (or set KAIRIX_DOCUMENT_ROOT)",
            file=sys.stderr,
        )
        return 1

    doc_path = Path(doc_root)
    if not doc_path.exists():
        print(f"Error: document root does not exist: {doc_root}", file=sys.stderr)
        return 1

    # Find the guide source in the kairix package
    guide_src = Path(__file__).parent.parent.parent / "docs" / "agent-usage-guide.md"
    if not guide_src.exists():
        # Fallback: look relative to the installed package
        import kairix

        pkg_root = Path(kairix.__file__).parent.parent
        guide_src = pkg_root / "docs" / "agent-usage-guide.md"

    if not guide_src.exists():
        print(f"Error: agent usage guide not found at {guide_src}", file=sys.stderr)
        print("Check your kairix installation is complete.", file=sys.stderr)
        return 1

    # Target: vault/04-Agent-Knowledge/shared/kairix-usage.md (standard PARA path)
    # Allow override via --output
    if args.output:
        dest = Path(args.output)
    else:
        # Try to find the shared knowledge directory
        candidates = [
            doc_path / "04-Agent-Knowledge" / "shared" / "kairix-usage.md",
            doc_path / "shared" / "kairix-usage.md",
            doc_path / "agent-knowledge" / "shared" / "kairix-usage.md",
        ]
        dest_or_none: Path | None = None
        for c in candidates:
            if c.parent.exists():
                dest_or_none = c
                break
        dest = dest_or_none if dest_or_none is not None else doc_path / "kairix-usage.md"

    if args.dry_run:
        print("Would install agent usage guide:")
        print(f"  Source: {guide_src}")
        print(f"  Dest:   {dest}")
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(guide_src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Agent usage guide installed at: {dest}")
    print()
    print("Agents can now find this guide via:")
    print('  kairix search "how do I use kairix" --agent <name>')
    print()
    print("Re-embed to make the guide searchable:")
    print("  kairix embed --changed")

    return 0


# ---------------------------------------------------------------------------
# verify subcommand
# ---------------------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    """Run the acceptance test suite against the live deployment."""
    script = Path(__file__).parent.parent.parent / "scripts" / "verify-search.py"
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


def main(argv: list[str] | None = None) -> int:
    """`kairix onboard` entry point.

    Returns the exit code (0 = success, 1 = failure) rather than calling
    ``sys.exit`` directly so tests can drive ``main(...)`` and assert on
    the return value without catching SystemExit. The package-level
    entry point in ``kairix/cli.py`` is responsible for translating this
    int into the process exit code.
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

    if args.subcommand == "check":
        return cmd_check(args)
    if args.subcommand == "guide":
        return cmd_guide(args)
    if args.subcommand == "verify":
        return cmd_verify(args)
    # argparse with required=True makes this unreachable in practice;
    # surface as a non-zero exit if argparse semantics ever change.
    return 2
