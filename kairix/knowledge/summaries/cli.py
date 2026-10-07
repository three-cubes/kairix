"""
CLI for kairix summarise subcommand.

Usage:
  kairix summarise --all               Generate L0 for all vault docs
  kairix summarise --stale             Regenerate only stale/missing
  kairix summarise --path FILE         Single file
  kairix summarise --all --include-l1  Generate both L0 + L1
  kairix summarise --status            Show coverage stats
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# F17 — argparse action keyword repeated across boolean-flag declarations; one
# constant keeps the well-known sentinel in a single edit site.
_STORE_TRUE = "store_true"


def default_get_credentials(kind: str) -> Any:
    from kairix.credentials import get_credentials

    return get_credentials(kind)


def default_write_summary(result: Any, db: sqlite3.Connection) -> None:
    from kairix.knowledge.summaries.staleness import write_summary

    write_summary(result, db)


def default_get_stale_paths(all_paths: list[str], db: sqlite3.Connection) -> list[str]:
    from kairix.knowledge.summaries.staleness import get_stale_paths

    return get_stale_paths(all_paths, db)


def default_generate_summaries(**kw: Any) -> list[Any]:
    from kairix.knowledge.summaries.generate import generate_summaries

    return generate_summaries(**kw)


def default_document_root_path(*, env: Mapping[str, str] | None = None) -> Path:
    """Production ``document_root_fn`` — resolves via :func:`kairix.paths.document_root`.

    ``env`` is the F2-clean seam forwarded to ``document_root``: ``None``
    (production) reads the live process env; tests pass a mapping instead
    of mutating ``os.environ``.
    """
    from kairix.paths import document_root

    return document_root(env=env)


def default_summaries_db_path_fn() -> Path:
    from kairix.paths import summaries_db_path

    return summaries_db_path()


@dataclass(frozen=True)
class SummariesCliDeps:
    """Injectable dependencies for the ``kairix summarise`` CLI.

    Mirrors ``CliDeps`` / ``StoreCliDeps``: every callable defaults via
    ``default_factory`` to the production helper. Tests construct
    ``SummariesCliDeps(get_credentials_fn=..., ...)`` and pass ``deps=``
    to :func:`main` to drive the CLI without monkey-patching
    ``kairix.credentials`` / ``kairix.knowledge.summaries.staleness`` /
    ``kairix.knowledge.summaries.generate`` / ``kairix.paths``.
    """

    get_credentials_fn: Callable[[str], Any] = field(default_factory=lambda: default_get_credentials)
    write_summary_fn: Callable[[Any, sqlite3.Connection], None] = field(default_factory=lambda: default_write_summary)
    get_stale_paths_fn: Callable[[list[str], sqlite3.Connection], list[str]] = field(
        default_factory=lambda: default_get_stale_paths
    )
    generate_summaries_fn: Callable[..., list[Any]] = field(default_factory=lambda: default_generate_summaries)
    document_root_fn: Callable[[], Path] = field(default_factory=lambda: default_document_root_path)
    summaries_db_path_fn: Callable[[], Path] = field(default_factory=lambda: default_summaries_db_path_fn)


# ---------------------------------------------------------------------------
# Credential helper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Vault doc discovery
# ---------------------------------------------------------------------------


def _discover_vault_docs(document_root: Path) -> list[str]:
    """Return absolute paths for all .md files under ``document_root``."""
    return [str(p) for p in document_root.rglob("*.md") if p.is_file()]


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------


def _open_db(db_path: Path) -> sqlite3.Connection:
    from kairix.paths import agent_cli_roots, confine_to_roots, summaries_db_path

    # ``--summaries-cache`` is a CLI-controllable path, so confine the SQLite
    # target to the standard agent-CLI roots plus the configured summaries dir
    # before connecting. ``confine_to_roots`` resolves + allow-lists and returns
    # the path (raising PathTraversalError on a ``../`` escape before any DB
    # file is created), which also clears the pythonsecurity:S8706 taint.
    safe_db_path = confine_to_roots(db_path, agent_cli_roots(summaries_db_path().parent))
    safe_db_path.parent.mkdir(parents=True, exist_ok=True)
    # F77-allow: operator CLI subcommand (summarise); per-invocation summary DB writer.
    conn = sqlite3.connect(str(safe_db_path))
    from kairix.knowledge.summaries.staleness import init_summaries_db

    init_summaries_db(conn)
    return conn


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_status(db: sqlite3.Connection, document_root: Path) -> None:
    """Print coverage stats."""
    total_row = db.execute("SELECT COUNT(*) FROM summaries").fetchone()
    l0_row = db.execute("SELECT COUNT(*) FROM summaries WHERE l0 IS NOT NULL AND l0 != ''").fetchone()
    l1_row = db.execute("SELECT COUNT(*) FROM summaries WHERE l1 IS NOT NULL AND l1 != ''").fetchone()

    total = total_row[0] if total_row else 0
    l0 = l0_row[0] if l0_row else 0
    l1 = l1_row[0] if l1_row else 0

    vault_count = len(_discover_vault_docs(document_root))

    print(f"Vault docs:     {vault_count}")
    print(f"With L0:        {l0} / {total} stored")
    print(f"With L1:        {l1} / {total} stored")
    stale_count = max(0, vault_count - l0)
    print(f"Approx stale:   {stale_count}")


def _run_generate(
    paths: list[str],
    include_l1: bool,
    api_key: str,
    endpoint: str,
    deployment: str,
    db: sqlite3.Connection,
    deps: SummariesCliDeps,
) -> None:
    """Generate summaries for paths and persist to DB."""
    print(f"Generating summaries for {len(paths)} file(s) (include_l1={include_l1})...")
    results = deps.generate_summaries_fn(
        paths=paths,
        api_key=api_key,
        endpoint=endpoint,
        deployment=deployment,
        include_l1=include_l1,
        batch_size=10,
        sleep_ms=100,
    )

    for result in results:
        deps.write_summary_fn(result, db)

    print(f"Done: {len(results)} / {len(paths)} succeeded.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _resolve_paths(
    args: argparse.Namespace,
    document_root: Path | None,
    db_path: Path | None,
    deps: SummariesCliDeps,
) -> tuple[Path, Path]:
    """Pick the effective ``document_root`` and ``db_path`` for this run.

    Precedence (highest wins): in-process kwarg → ``--document-root`` /
    ``--summaries-cache`` argparse flag → ``deps.document_root_fn`` /
    ``deps.summaries_db_path_fn`` (default: ``kairix.paths``).
    Extracted from ``main`` to keep its cognitive complexity below F16's
    ceiling.
    """
    if args.document_root is not None and document_root is None:
        document_root = Path(args.document_root)
    if args.summaries_cache is not None and db_path is None:
        db_path = Path(args.summaries_cache)

    if document_root is None:
        document_root = deps.document_root_fn()
    if db_path is None:
        db_path = deps.summaries_db_path_fn()
    return document_root, db_path


def main(
    argv: list[str] | None = None,
    *,
    document_root: Path | None = None,
    db_path: Path | None = None,
    deps: SummariesCliDeps | None = None,
) -> None:
    """Entry point for `kairix summarise`.

    ``document_root`` and ``db_path`` are DI seams for tests; production
    callers leave them ``None`` and the CLI resolves them from the
    environment via ``kairix.paths``.

    ``deps`` is the F1-clean DI seam: tests construct
    ``SummariesCliDeps(get_credentials_fn=..., write_summary_fn=..., ...)``
    and pass it here to drive the CLI without monkey-patching
    ``kairix.credentials``, ``kairix.knowledge.summaries.staleness``,
    ``kairix.knowledge.summaries.generate``, or ``kairix.paths``.
    Production callers leave it ``None``.
    """
    d = deps if deps is not None else SummariesCliDeps()
    parser = argparse.ArgumentParser(
        prog="kairix summarise",
        description="Generate L0/L1 tiered summaries for vault documents.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action=_STORE_TRUE, help="Generate for all vault docs")
    group.add_argument("--stale", action=_STORE_TRUE, help="Generate only stale/missing")
    group.add_argument("--path", metavar="FILE", help="Single file to summarise")
    group.add_argument("--status", action=_STORE_TRUE, help="Show coverage stats")

    parser.add_argument(
        "--include-l1",
        action=_STORE_TRUE,
        default=False,
        help="Also generate L1 structured overview (slower, more tokens)",
    )
    parser.add_argument(
        "--deployment",
        default="gpt-4o-mini",
        help="Azure OpenAI deployment name (default: gpt-4o-mini)",
    )
    parser.add_argument(
        "--document-root",
        default=None,
        help=(
            "Override the document root for this invocation. When omitted, "
            "the default resolution chain (KAIRIX_DOCUMENT_ROOT env / "
            "kairix.config.yaml / platform default) runs. Matches the "
            "canonical pattern in ``kairix store crawl --document-root``; "
            "enables F30 subprocess outcome tests to drive a tmp document "
            "root without touching the process environment."
        ),
    )
    parser.add_argument(
        "--summaries-cache",
        default=None,
        help=(
            "Override the summaries SQLite cache path. When omitted the "
            "default resolution chain runs. Subprocess seam for F30 "
            "outcome tests; production callers leave this unset."
        ),
    )

    args = parser.parse_args(argv if argv is not None else sys.argv[2:])

    document_root, db_path = _resolve_paths(args, document_root, db_path, d)

    db = _open_db(db_path)

    if args.status:
        _cmd_status(db, document_root)
        db.close()
        return

    # Fetch credentials (only needed for generation)
    try:
        llm_creds = d.get_credentials_fn("llm")
        api_key = llm_creds.api_key
        endpoint = llm_creds.endpoint
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.all:
        paths = _discover_vault_docs(document_root)
        if not paths:
            print("No vault docs found.", file=sys.stderr)
            sys.exit(1)
        _run_generate(paths, args.include_l1, api_key, endpoint, args.deployment, db, d)

    elif args.stale:
        all_paths = _discover_vault_docs(document_root)
        paths = d.get_stale_paths_fn(all_paths, db)
        print(f"Stale/missing: {len(paths)} of {len(all_paths)}")
        if not paths:
            print("Nothing to do.")
            db.close()
            return
        _run_generate(paths, args.include_l1, api_key, endpoint, args.deployment, db, d)

    elif args.path:
        p = Path(args.path)
        if not p.exists():
            print(f"File not found: {args.path}", file=sys.stderr)
            sys.exit(1)
        _run_generate([str(p)], args.include_l1, api_key, endpoint, args.deployment, db, d)

    db.close()


if __name__ == "__main__":
    main()
