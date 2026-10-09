"""``kairix secrets`` — operator surface for the canonical credential map.

Two subcommands:

* ``kairix secrets verify`` — for every registered credential identity,
  try to resolve the secret via :class:`SecretsLoader`. Prints a per-row
  status table (``present`` / ``MISSING``). Exits non-zero if any
  required secret is missing — suitable for a pre-deploy gate.
* ``kairix secrets set <name>`` — persist one canonical secret into the
  resolved operator bundle file (#473). The value arrives via stdin by
  default so it never lands in shell history; ``--value`` exists for
  non-sensitive values. Delegates to
  :func:`kairix.secrets.store.set_secret` — the same use-case function
  the setup wizard calls.

Both subcommands accept ``--json`` for envelope-shape output so
agent-facing tooling can consume the same data the human surface shows.

The legacy alias chain (``LEGACY_ALIASES`` + ``migrate-list``) was
retired in #369; operators with pre-canonical env-var names must
rotate to the ``KAIRIX_<SCOPE>_<AREA>[_<INSTANCE>]_<LEAF>`` form.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, MutableMapping
from dataclasses import asdict, dataclass
from pathlib import Path

from kairix.secrets.loader import SecretsLoader, SecretsResolver
from kairix.secrets.naming import (
    Scope,
    canonical_secret_name,
)

_VERIFY = "verify"
_SET = "set"

# Hoisted next-step affordance — quoted by the set success line and the
# JSON envelope (F17).
_NEXT_VERIFY = "kairix secrets verify"

# Hoisted leaf constant — F17 (no string literal ≥10 chars duplicated
# ≥3 times). Shared across M365, Slack, Gmail rows that all carry an
# OAuth-style client secret with the same leaf name.
_LEAF_CLIENT_SECRET = "client-secret"  # noqa: S105 — secret-SLOT name (the leaf identifier), not a value  # pragma: allowlist secret


# Canonical credential identities registered for verification. Each
# tuple is ``(scope, area, instance, leaf)`` — the same identity the
# loader resolves. Operators add a row here when shipping a new
# credential surface; the verify CLI then reports its status.
_REGISTERED_IDENTITIES: tuple[tuple[Scope, str, str | None, str], ...] = (
    # ── Infrastructure: Neo4j ──────────────────────────────────────
    ("infra", "neo4j", None, "password"),
    ("infra", "neo4j", None, "uri"),
    ("infra", "neo4j", None, "user"),
    # ── Providers: LLM (chat) ──────────────────────────────────────
    ("provider", "llm", None, "api-key"),
    ("provider", "llm", None, "endpoint"),
    ("provider", "llm", None, "model"),
    # ── Providers: embeddings ──────────────────────────────────────
    ("provider", "embed", None, "api-key"),
    ("provider", "embed", None, "endpoint"),
    ("provider", "embed", None, "model"),
    # ── Connectors: M365 ───────────────────────────────────────────
    ("connector", "m365", None, "tenant-id"),
    ("connector", "m365", None, "client-id"),
    ("connector", "m365", None, _LEAF_CLIENT_SECRET),
    # ── Connectors: Slack ──────────────────────────────────────────
    ("connector", "slack", None, "bot-token"),
    ("connector", "slack", None, "app-token"),
    ("connector", "slack", None, "client-id"),
    ("connector", "slack", None, _LEAF_CLIENT_SECRET),
    # ── Connectors: GitHub ─────────────────────────────────────────
    ("connector", "github", None, "pat"),
    ("connector", "github", None, "app-id"),
    ("connector", "github", None, "installation-id"),
    ("connector", "github", None, "app-private-key"),
    ("connector", "github", None, "webhook-secret"),
    # ── Connectors: Notion ─────────────────────────────────────────
    ("connector", "notion", None, "token"),
    # ── Connectors: Google Drive ───────────────────────────────────
    ("connector", "google-drive", None, "access-token"),
    # ── Connectors: Gmail ──────────────────────────────────────────
    ("connector", "gmail", None, "client-id"),
    ("connector", "gmail", None, _LEAF_CLIENT_SECRET),
    ("connector", "gmail", None, "refresh-token"),
    ("connector", "gmail", None, "access-token"),
    # ── Connectors: Apple CalDAV ───────────────────────────────────
    ("connector", "apple-caldav", None, "username"),
    ("connector", "apple-caldav", None, "access"),
    # ── Connectors: Dex CRM ────────────────────────────────────────
    ("connector", "dex", None, "api-key"),
)


@dataclass(frozen=True)
class _VerifyRow:
    """One row in the ``secrets verify`` output."""

    scope: Scope
    area: str
    instance: str
    leaf: str
    status: str  # "present" | "MISSING"
    canonical_kv: str


def _row(
    scope: Scope,
    area: str,
    instance: str | None,
    leaf: str,
    loader: SecretsResolver,
) -> _VerifyRow:
    """Build one verify row by asking the loader to resolve."""
    canonical_kv = canonical_secret_name(scope, area, instance, leaf)
    value = loader.get(scope, area, instance, leaf)
    status = "MISSING" if value is None else "present"
    return _VerifyRow(
        scope=scope,
        area=area,
        instance=instance or "-",
        leaf=leaf,
        status=status,
        canonical_kv=canonical_kv,
    )


def _format_verify_table(rows: Iterable[_VerifyRow]) -> str:
    """Render the verify table as a fixed-width text block."""
    header = f"{'STATUS':<10}{'SCOPE':<12}{'AREA':<18}{'INSTANCE':<11}{'LEAF':<22}{'CANONICAL'}"
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row.status:<10}{row.scope:<12}{row.area:<18}{row.instance:<11}{row.leaf:<22}{row.canonical_kv}",
        )
    return "\n".join(lines)


def _format_verify_json(rows: Iterable[_VerifyRow]) -> str:
    """Render the verify table as a JSON envelope."""
    payload = {"secrets": [asdict(row) for row in rows]}
    return json.dumps(payload, indent=2, sort_keys=True)


def _default_identities_provider() -> tuple[tuple[Scope, str, str | None, str], ...]:
    """Default identity provider — every registered canonical credential.

    Production callers leave the seam untouched. Tests pass a synthetic
    tuple to scope the verify walk to a known subset.
    """
    return _REGISTERED_IDENTITIES


def _default_loader_factory() -> SecretsResolver:
    """Default loader factory — fresh :class:`SecretsLoader` reading os.environ."""
    return SecretsLoader()


def _ensure_bundle_loaded(bundle_path: Path | None = None, env: MutableMapping[str, str] | None = None) -> None:
    """Hydrate the secrets bundle via the canonical bootstrap_secrets.

    Thin shim — kept for backwards-compat with existing tests that
    call it directly. New code should call
    ``kairix.secrets.bootstrap.bootstrap_secrets`` (or just let the
    CLI dispatcher's bootstrap call cover it).
    """
    from kairix.secrets.bootstrap import bootstrap_secrets

    bootstrap_secrets(bundle_path=bundle_path, force=True, env=env)


def _run_verify(
    *,
    emit_json: bool,
    loader_factory: Callable[[], SecretsResolver],
    identities_provider: Callable[[], tuple[tuple[Scope, str, str | None, str], ...]],
    bundle_path: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, int]:
    """Build the verify table + return (rendered_output, exit_code).

    Exit code is 1 if any row is MISSING, 0 otherwise.

    ``bundle_path`` is the test seam for the bundle-hydration step;
    production callers leave it as None (load_secrets reads
    ``$KAIRIX_SECRETS_FILE`` / the default path).
    """
    _ensure_bundle_loaded(bundle_path, environ)
    loader = loader_factory()
    identities = identities_provider()
    rows = [_row(scope, area, instance, leaf, loader) for scope, area, instance, leaf in identities]

    rendered = _format_verify_json(rows) if emit_json else _format_verify_table(rows)
    exit_code = 1 if any(row.status == "MISSING" for row in rows) else 0
    return rendered, exit_code


def _default_value_reader() -> str:
    """Read the secret value from stdin — the leak-safe default for ``set``."""
    return sys.stdin.read()


def _run_set(
    *,
    name: str,
    inline_value: str | None,
    emit_json: bool,
    bundle_path: Path | None,
    value_reader: Callable[[], str],
) -> tuple[str, int]:
    """Persist one canonical secret; return (rendered_output, exit_code).

    Exit code 0 on success, 2 on a rejected name/value. The rendered
    output never contains the secret value (F15) — success names the
    secret SLOT and the destination path only.
    """
    from kairix.secrets.store import set_secret

    raw = inline_value if inline_value is not None else value_reader()
    # Strip the trailing newline that `echo` / heredocs append; values
    # with interior newlines (a pasted PEM key) are accepted —
    # set_secret encodes them to one newline-safe bundle line.
    value = raw.rstrip("\r\n") if raw else ""
    try:
        path = set_secret(name, value, bundle_path=bundle_path)
    except ValueError as exc:
        if emit_json:
            return json.dumps({"error": str(exc), "name": name}, indent=2, sort_keys=True), 2
        return str(exc), 2
    if emit_json:
        envelope = {"stored": name, "path": str(path), "mode": "0600", "next": _NEXT_VERIFY}
        return json.dumps(envelope, indent=2, sort_keys=True), 0
    return f"Stored {name} in {path} (0600). next: {_NEXT_VERIFY}", 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for ``kairix secrets``."""
    parser = argparse.ArgumentParser(
        prog="kairix secrets",
        description="Inspect and persist the canonical credential surface.",
    )
    sub = parser.add_subparsers(dest="action", required=True, metavar="ACTION")

    verify = sub.add_parser(
        _VERIFY,
        help="Resolve every registered credential via SecretsLoader; non-zero exit on any miss.",
    )
    verify.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Emit a JSON envelope instead of the human-readable table.",
    )

    set_parser = sub.add_parser(
        _SET,
        help="Persist one canonical secret into the operator bundle file (value from stdin by default).",
        description=(
            "Write or update one canonical secret in the resolved bundle file "
            "($KAIRIX_SECRETS_FILE, else /run/secrets/kairix.env in containers, "
            "else ~/.config/kairix/secrets/kairix.env for pip installs). "
            "The value is read from stdin by default — "
            "printf '%s' '<the-value>' | kairix secrets set <name> — "
            "so it never lands in your shell history. The file is created "
            "with mode 0600; existing entries are replaced in place and "
            "unrelated lines are preserved."
        ),
    )
    set_parser.add_argument(
        "name",
        help="Canonical secret name: kairix-<scope>-<area>[-<instance>]-<leaf>, e.g. kairix-provider-llm-api-key",
    )
    set_parser.add_argument(
        "--value",
        default=None,
        help=(
            "Inline value for NON-sensitive entries only (endpoints, model names). "
            "For API keys and tokens, pipe via stdin instead — --value leaks into shell history."
        ),
    )
    set_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Emit a JSON envelope instead of the human-readable confirmation.",
    )

    return parser


def main(
    argv: list[str] | None = None,
    *,
    loader_factory: Callable[[], SecretsResolver] = _default_loader_factory,
    identities_provider: Callable[
        [],
        tuple[tuple[Scope, str, str | None, str], ...],
    ] = _default_identities_provider,
    bundle_path: Path | None = None,
    value_reader: Callable[[], str] = _default_value_reader,
    environ: MutableMapping[str, str] | None = None,
) -> int:
    """Entry point for ``kairix secrets``.

    Thin adapter — parse argv, route to the verify or set branch. The
    keyword-only seams (``loader_factory``, ``identities_provider``,
    ``bundle_path``, ``value_reader``) are the DI surface for tests;
    production callers leave them at their defaults. ``bundle_path``
    names the operator bundle file for both branches: the hydration
    source for ``verify``, the write target for ``set``. ``environ`` is
    the env mapping ``verify`` hydrates the bundle into (``None``: the live
    ``os.environ``); pair it with a ``loader_factory`` reading the same map.
    """
    args = build_parser().parse_args(argv if argv is not None else sys.argv[2:])

    if args.action == _SET:
        rendered, exit_code = _run_set(
            name=args.name,
            inline_value=args.value,
            emit_json=args.emit_json,
            bundle_path=bundle_path,
            value_reader=value_reader,
        )
        print(rendered, file=sys.stdout if exit_code == 0 else sys.stderr)
        return exit_code

    rendered, exit_code = _run_verify(
        emit_json=args.emit_json,
        loader_factory=loader_factory,
        identities_provider=identities_provider,
        bundle_path=bundle_path,
        environ=environ,
    )

    print(rendered)
    return exit_code


__all__ = ["build_parser", "main"]
