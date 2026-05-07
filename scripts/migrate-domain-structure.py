#!/usr/bin/env python3
"""
Domain restructure migration script.

Moves 22 flat packages under kairix/ into 5 domain groups:
  core/       — search engine (search, embed, db, temporal, classify)
  knowledge/  — knowledge management (entities, graph, store, wikilinks, reflib, summaries, contradict)
  agents/     — agent capabilities (mcp, briefing, curator, research)
  eval/       — evaluation (benchmark, eval, contracts)
  platform/   — deployment (setup, onboard, llm)

Usage:
  python3 scripts/migrate-domain-structure.py --dry-run   # preview changes
  python3 scripts/migrate-domain-structure.py             # execute
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PACKAGE_ROOT = REPO_ROOT / "kairix"

# Old package → new domain/package mapping
MIGRATION_MAP = {
    # core/ — the search engine
    "search": "core/search",
    "embed": "core/embed",
    "db": "core/db",
    "temporal": "core/temporal",
    "classify": "core/classify",
    # knowledge/ — knowledge management
    "entities": "knowledge/entities",
    "graph": "knowledge/graph",
    "store": "knowledge/store",
    "wikilinks": "knowledge/wikilinks",
    "reflib": "knowledge/reflib",
    "summaries": "knowledge/summaries",
    "contradict": "knowledge/contradict",
    # agents/ — agent-facing capabilities
    "mcp": "agents/mcp",
    "briefing": "agents/briefing",
    "curator": "agents/curator",
    "research": "agents/research",
    # quality/ — evaluation and benchmarking
    "benchmark": "quality/benchmark",
    "eval": "quality/eval",
    "contracts": "quality/contracts",
    # platform/ — deployment and onboarding
    "setup": "platform/setup",
    "onboard": "platform/onboard",
    "llm": "platform/llm",
}

# Build import rewrite rules: "kairix.old" → "kairix.domain.old"
IMPORT_REWRITES = {}
for old, new in MIGRATION_MAP.items():
    IMPORT_REWRITES[f"kairix.{old}"] = f"kairix.{new.replace('/', '.')}"


def rewrite_imports_in_file(filepath: Path, dry_run: bool = False) -> list[str]:
    """Rewrite kairix imports in a single file. Returns list of changes made."""
    if not filepath.resolve().is_relative_to(REPO_ROOT.resolve()):
        return []

    try:
        content = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    changes = []
    new_content = content

    # Sort by longest first to avoid partial replacements
    # e.g. "kairix.quality.eval" before "kairix.quality.eval" (but "kairix.quality.eval.benchmark" doesn't exist as old)
    sorted_rewrites = sorted(IMPORT_REWRITES.items(), key=lambda x: -len(x[0]))

    for old_import, new_import in sorted_rewrites:
        # Match: from kairix.old.X import Y  or  import kairix.old.X
        # Also match string literals like "kairix.old.X" in pytest_plugins lists
        pattern = re.escape(old_import)

        # Only replace when followed by a word boundary (., space, newline, quote, etc.)
        # This prevents "kairix.quality.eval" from matching inside "kairix.eval_something"
        for match in re.finditer(rf'{pattern}(?=[.\s"\',\)\]])', new_content):
            pass  # just checking if any matches exist

        if old_import in new_content:
            # Careful: only replace full module paths, not partial matches
            # Use word boundary after the module name
            updated = re.sub(
                rf'(?<![a-zA-Z0-9_.]){re.escape(old_import)}(?=[.\s"\',\)\]:;]|$)',
                new_import,
                new_content,
            )
            if updated != new_content:
                count = len(
                    re.findall(
                        rf'(?<![a-zA-Z0-9_.]){re.escape(old_import)}(?=[.\s"\',\)\]:;]|$)',
                        new_content,
                    )
                )
                changes.append(f"  {old_import} → {new_import} ({count} occurrences)")
                new_content = updated

    if changes and not dry_run:
        # Internal one-shot migration script; filepath is from rglob over the
        # package root, never user input. NOSONAR placed on the write_text
        # statement where S2083 actually fires.
        filepath.write_text(
            new_content, encoding="utf-8"
        )  # NOSONAR(python:S2083) — internal migration, rglob source not user input

    return changes


def create_domain_init_files(dry_run: bool = False) -> list[str]:
    """Create __init__.py files for new domain packages."""
    domains = ["core", "knowledge", "agents", "quality", "platform"]
    created = []
    for domain in domains:
        init_path = PACKAGE_ROOT / domain / "__init__.py"
        if not init_path.exists():
            if not dry_run:
                init_path.parent.mkdir(parents=True, exist_ok=True)
                init_path.write_text("", encoding="utf-8")
            created.append(str(init_path.relative_to(REPO_ROOT)))
    return created


def move_packages(dry_run: bool = False) -> list[str]:
    """Move packages to new domain structure using git mv."""
    moves = []
    for old, new in MIGRATION_MAP.items():
        old_path = PACKAGE_ROOT / old
        new_path = PACKAGE_ROOT / new

        if not old_path.exists():
            print(f"  SKIP: {old_path} does not exist")
            continue

        if new_path.exists():
            print(f"  SKIP: {new_path} already exists")
            continue

        moves.append(f"  kairix/{old}/ → kairix/{new}/")

        if not dry_run:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "mv", str(old_path), str(new_path)],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
            )

    return moves


def rewrite_all_imports(dry_run: bool = False) -> dict[str, list[str]]:
    """Rewrite imports in all Python files."""
    all_changes = {}

    # Process all .py files in kairix/ and tests/
    for search_dir in [PACKAGE_ROOT, REPO_ROOT / "tests", REPO_ROOT / "scripts"]:
        for filepath in search_dir.rglob("*.py"):
            if "__pycache__" in str(filepath) or ".egg-info" in str(filepath):
                continue
            changes = rewrite_imports_in_file(filepath, dry_run)
            if changes:
                rel_path = str(filepath.relative_to(REPO_ROOT))
                all_changes[rel_path] = changes

    # Also process conftest.py at repo root level
    conftest = REPO_ROOT / "tests" / "conftest.py"
    if conftest.exists():
        changes = rewrite_imports_in_file(conftest, dry_run)
        if changes:
            all_changes[str(conftest.relative_to(REPO_ROOT))] = changes

    return all_changes


def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("=== DRY RUN — no changes will be made ===\n")
    else:
        print("=== EXECUTING DOMAIN RESTRUCTURE ===\n")

    # Step 1: Create domain __init__.py files
    print("Step 1: Create domain __init__.py files")
    created = create_domain_init_files(dry_run)
    for f in created:
        print(f"  CREATE {f}")
    print(f"  → {len(created)} files\n")

    # Step 2: Move packages
    print("Step 2: Move packages to domain structure")
    moves = move_packages(dry_run)
    for m in moves:
        print(m)
    print(f"  → {len(moves)} packages moved\n")

    # Step 3: Rewrite imports
    print("Step 3: Rewrite imports in all Python files")
    changes = rewrite_all_imports(dry_run)
    for filepath, file_changes in sorted(changes.items()):
        print(f"  {filepath}:")
        for c in file_changes:
            print(f"    {c}")
    total_files = len(changes)
    total_rewrites = sum(len(c) for c in changes.values())
    print(f"  → {total_rewrites} import rewrites across {total_files} files\n")

    if dry_run:
        print("=== DRY RUN COMPLETE — run without --dry-run to execute ===")
    else:
        print("=== RESTRUCTURE COMPLETE ===")
        print("Next: run ruff check + tests to verify")


if __name__ == "__main__":
    main()
