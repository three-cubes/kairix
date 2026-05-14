#!/usr/bin/env python3
"""
audit-date-formats.py — Vault date format audit for TMP-5.

Scans all .md files in the vault and classifies frontmatter date fields as:
  - ISO (YYYY-MM-DD) — date_extract.py will extract these
  - datetime (YYYY-MM-DDTHH:MM or YYYY-MM-DD HH:MM) — date_extract.py extracts date part
  - non-ISO — date_extract.py cannot extract; chunk_date will be NULL
  - absent — no date field in frontmatter

Outputs a report to stdout and optional JSON summary via --output.

Usage:
    python3 audit-date-formats.py --vault-root /data/obsidian-vault
    python3 audit-date-formats.py --vault-root /path/to/vault --output /tmp/audit.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Date format patterns (mirrors date_extract.py logic)
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_FRONTMATTER_FIELD_RE = re.compile(  # NOSONAR — line-anchored MULTILINE; capture bounded; fixed alternation.
    r'^(?:date|created|updated|created_at)\s*:\s*"?([^"\n]+)"?\s*$',
    re.MULTILINE | re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_date_value(value: str) -> str:
    """Classify a frontmatter date value string."""
    v = value.strip().strip('"').strip("'")
    if _ISO_RE.match(v):
        return "iso"
    if _DATETIME_RE.match(v):
        return "datetime"
    if v:
        return "non_iso"
    return "absent"


def audit_file(path: Path) -> tuple[str, str]:
    """
    Audit a single .md file.

    Returns (classification, raw_value):
      classification: 'iso' | 'datetime' | 'non_iso' | 'absent'
      raw_value: the raw date field value, or '' if absent
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return "error", ""

    if not text.startswith("---"):
        return "absent", ""

    # Find end of frontmatter
    end = text.find("\n---", 3)
    if end == -1:
        return "absent", ""
    frontmatter = text[: end + 4]

    m = _FRONTMATTER_FIELD_RE.search(frontmatter)
    if not m:
        return "absent", ""

    raw_value = m.group(1).strip()
    return classify_date_value(raw_value), raw_value


# ---------------------------------------------------------------------------
# Directory categorisation
# ---------------------------------------------------------------------------


def categorise_path(vault_root: Path, file_path: Path) -> str:
    """Map a file path to a high-level directory category."""
    try:
        rel = file_path.relative_to(vault_root)
        top = rel.parts[0] if rel.parts else "root"
    except ValueError:
        return "other"
    categories = {
        "01-Projects": "projects",
        "02-Areas": "areas",
        "03-Resources": "resources",
        "04-Agent-Knowledge": "agent-knowledge",
        "05-Knowledge": "knowledge",
        "06-Entities": "entities",
    }
    return categories.get(top, top)


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------


def run_audit(vault_root: Path) -> dict[str, Any]:
    """Run the full audit and return a summary dict."""
    md_files = list(vault_root.rglob("*.md"))
    # Exclude .git, .obsidian, node_modules
    md_files = [f for f in md_files if not any(p.startswith(".") for p in f.parts)]

    total = len(md_files)
    classification_counts: Counter[str] = Counter()
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    non_iso_examples: list[dict[str, str]] = []
    non_iso_counts: Counter[str] = Counter()

    for f in md_files:
        cls, raw = audit_file(f)
        classification_counts[cls] += 1
        cat = categorise_path(vault_root, f)
        by_category[cat][cls] += 1
        if cls == "non_iso":
            non_iso_counts[raw] += 1
            if len(non_iso_examples) < 20:
                non_iso_examples.append(
                    {
                        "path": str(f.relative_to(vault_root)),
                        "value": raw,
                    }
                )

    return {
        "total_files": total,
        "classification_counts": dict(classification_counts),
        "by_category": {k: dict(v) for k, v in sorted(by_category.items())},
        "non_iso_top_values": non_iso_counts.most_common(15),
        "non_iso_examples": non_iso_examples,
        "extractable_pct": round(
            100 * (classification_counts["iso"] + classification_counts["datetime"]) / max(total, 1), 1
        ),
    }


def print_report(audit: dict[str, Any]) -> None:
    """Print a human-readable audit report."""
    total = audit["total_files"]
    counts = audit["classification_counts"]

    print("=" * 60)
    print("VAULT DATE FORMAT AUDIT — TMP-5")
    print("=" * 60)
    print(f"\nTotal .md files scanned: {total}")
    print(f"\n{'Classification':<15} {'Count':>6} {'Pct':>6}")
    print("-" * 30)
    for cls in ("iso", "datetime", "non_iso", "absent", "error"):
        n = counts.get(cls, 0)
        pct = 100 * n / max(total, 1)
        note = ""
        if cls == "iso":
            note = " ← date_extract.py: YES"
        elif cls == "datetime":
            note = " ← date_extract.py: YES (date part)"
        elif cls == "non_iso":
            note = " ← date_extract.py: NO"
        elif cls == "absent":
            note = " ← filename fallback only"
        print(f"  {cls:<13} {n:>6} {pct:>5.1f}%{note}")

    print(f"\n  Extractable (iso + datetime): {audit['extractable_pct']}%")

    print("\n\nBy directory category:")
    print(f"{'Category':<20} {'iso':>5} {'datetime':>9} {'non_iso':>8} {'absent':>7}")
    print("-" * 52)
    for cat, cat_counts in audit["by_category"].items():
        print(
            f"  {cat:<18} {cat_counts.get('iso', 0):>5} "
            f"{cat_counts.get('datetime', 0):>9} "
            f"{cat_counts.get('non_iso', 0):>8} "
            f"{cat_counts.get('absent', 0):>7}"
        )

    if audit["non_iso_top_values"]:
        print("\n\nMost common non-ISO date values:")
        for val, count in audit["non_iso_top_values"]:
            print(f"  {count:>4}x {val!r}")

    if audit["non_iso_examples"]:
        print(f"\nNon-ISO examples (first {len(audit['non_iso_examples'])}):")
        for ex in audit["non_iso_examples"][:10]:
            print(f"  [{ex['value']}] {ex['path']}")

    print("\n" + "=" * 60)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Audit vault date formats for TMP-5")
    parser.add_argument("--vault-root", required=True, help="Path to obsidian vault root")
    parser.add_argument("--output", default=None, help="Optional JSON output path")
    args = parser.parse_args(argv)

    vault_root = Path(args.vault_root)
    if not vault_root.exists():
        print(f"ERROR: vault root not found: {vault_root}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {vault_root} ...", file=sys.stderr)
    audit = run_audit(vault_root)
    print_report(audit)

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(json.dumps(audit, indent=2))
        print(f"\nJSON summary written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
