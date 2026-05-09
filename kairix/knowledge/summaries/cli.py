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
from pathlib import Path

# ---------------------------------------------------------------------------
# Credential helper
# ---------------------------------------------------------------------------


def _get_cred(secret_name: str) -> str:
    from kairix.secrets import get_secret

    value = get_secret(secret_name, required=True)
    assert value is not None  # get_secret raises if required and missing
    return value


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
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
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
) -> None:
    """Generate summaries for paths and persist to DB."""
    from kairix.knowledge.summaries.generate import generate_summaries
    from kairix.knowledge.summaries.staleness import write_summary

    print(f"Generating summaries for {len(paths)} file(s) (include_l1={include_l1})...")
    results = generate_summaries(
        paths=paths,
        api_key=api_key,
        endpoint=endpoint,
        deployment=deployment,
        include_l1=include_l1,
        batch_size=10,
        sleep_ms=100,
    )

    for result in results:
        write_summary(result, db)

    print(f"Done: {len(results)} / {len(paths)} succeeded.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(
    argv: list[str] | None = None,
    *,
    document_root: Path | None = None,
    db_path: Path | None = None,
) -> None:
    """Entry point for `kairix summarise`.

    ``document_root`` and ``db_path`` are DI seams for tests; production
    callers leave them ``None`` and the CLI resolves them from the
    environment via ``kairix.paths``.
    """
    parser = argparse.ArgumentParser(
        prog="kairix summarise",
        description="Generate L0/L1 tiered summaries for vault documents.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Generate for all vault docs")
    group.add_argument("--stale", action="store_true", help="Generate only stale/missing")
    group.add_argument("--path", metavar="FILE", help="Single file to summarise")
    group.add_argument("--status", action="store_true", help="Show coverage stats")

    parser.add_argument(
        "--include-l1",
        action="store_true",
        default=False,
        help="Also generate L1 structured overview (slower, more tokens)",
    )
    parser.add_argument(
        "--deployment",
        default="gpt-4o-mini",
        help="Azure OpenAI deployment name (default: gpt-4o-mini)",
    )

    args = parser.parse_args(argv if argv is not None else sys.argv[2:])

    if document_root is None:
        from kairix.paths import document_root as _resolve_document_root

        document_root = _resolve_document_root()
    if db_path is None:
        from kairix.paths import summaries_db_path

        db_path = summaries_db_path()

    db = _open_db(db_path)

    if args.status:
        _cmd_status(db, document_root)
        db.close()
        return

    # Fetch credentials (only needed for generation)
    try:
        from kairix.credentials import get_credentials

        llm_creds = get_credentials("llm")
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
        _run_generate(paths, args.include_l1, api_key, endpoint, args.deployment, db)

    elif args.stale:
        all_paths = _discover_vault_docs(document_root)
        from kairix.knowledge.summaries.staleness import get_stale_paths

        paths = get_stale_paths(all_paths, db)
        print(f"Stale/missing: {len(paths)} of {len(all_paths)}")
        if not paths:
            print("Nothing to do.")
            db.close()
            return
        _run_generate(paths, args.include_l1, api_key, endpoint, args.deployment, db)

    elif args.path:
        p = Path(args.path)
        if not p.exists():
            print(f"File not found: {args.path}", file=sys.stderr)
            sys.exit(1)
        _run_generate([str(p)], args.include_l1, api_key, endpoint, args.deployment, db)

    db.close()


if __name__ == "__main__":
    main()
