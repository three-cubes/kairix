#!/usr/bin/env python3
"""
chunk-daily-files.py — TMP-4: Daily memory log section chunker.

Pre-processes agent daily memory log files (YYYY-MM-DD.md) by splitting
them into per-section chunk files suitable for kairix ingestion. Each ##
section becomes a separate document with injected frontmatter carrying:
  - source: original vault-relative path
  - section_heading: the ## heading text
  - date: extracted from filename (YYYY-MM-DD)
  - type: daily_chunk

Output files are written to --output-dir (default: /tmp/daily-chunks/).
They are ephemeral — regenerate by re-running this script.

After running, ingest with:
  kairix embed <output-dir>

Usage:
    python3 chunk-daily-files.py --vault-root /data/obsidian-vault
    python3 chunk-daily-files.py --vault-root /vault --output-dir /tmp/chunks --dry-run
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MEMORY_LOG_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def _slug(text: str) -> str:
    """Convert text to a filename-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def find_memory_logs(vault_root: Path) -> list[Path]:
    """Find all YYYY-MM-DD.md files in the vault."""
    logs: list[Path] = []
    for path in vault_root.rglob("*.md"):
        if _MEMORY_LOG_RE.match(path.name):
            logs.append(path)
    return sorted(logs)


def split_into_sections(content: str) -> list[tuple[str, str]]:
    """
    Split markdown content on ## headings.

    Returns list of (heading, body) tuples.
    Content before the first heading is returned as ("", body) if non-empty.
    Empty sections (body is whitespace-only) are excluded.
    """
    sections: list[tuple[str, str]] = []
    parts = _SECTION_RE.split(content)

    # parts[0] is content before first ##; parts[1], parts[2], ...
    # alternate between heading and body
    preamble = parts[0].strip()
    if preamble:
        sections.append(("", preamble))

    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if body:
            sections.append((heading, body))

    return sections


def chunk_file(log_path: Path, vault_root: Path) -> list[dict[str, Any]]:
    """
    Chunk a single memory log file into per-section dicts.

    Returns list of dicts: {heading, body, date, source, filename}
    """
    m = _MEMORY_LOG_RE.match(log_path.name)
    if not m:
        return []

    file_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    try:
        vault_rel = str(log_path.relative_to(vault_root))
    except ValueError:
        vault_rel = str(log_path)

    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("chunk_file: cannot read %s — %s", log_path, e)
        return []

    # Strip frontmatter
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            content = content[end + 4 :].lstrip("\n")

    sections = split_into_sections(content)
    chunks: list[dict[str, Any]] = []
    for idx, (heading, body) in enumerate(sections):
        slug = _slug(heading) if heading else "preamble"
        stem = log_path.stem  # YYYY-MM-DD
        filename = f"{stem}-{slug}-{idx:02d}.md"
        chunks.append(
            {
                "heading": heading,
                "body": body,
                "date": file_date,
                "source": vault_rel,
                "filename": filename,
            }
        )

    return chunks


def write_chunk(chunk: dict[str, Any], output_dir: Path) -> Path:
    """Write a single chunk to output_dir with injected frontmatter."""
    heading = chunk["heading"]
    body = chunk["body"]
    date = chunk["date"]
    source = chunk["source"]
    filename = chunk["filename"]

    frontmatter_lines = [
        "---",
        "type: daily_chunk",
        f"date: {date}",
        f'source: "{source}"',
    ]
    if heading:
        frontmatter_lines.append(f'section_heading: "{heading}"')
    frontmatter_lines.append("---")

    title = f"# {heading}" if heading else f"# {source} — preamble"
    content = "\n".join(frontmatter_lines) + f"\n\n{title}\n\n{body}\n"

    out_path: Path = output_dir / filename
    out_path.write_text(content, encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="TMP-4: Daily memory log section chunker")
    parser.add_argument("--vault-root", required=True, help="Path to vault root")
    parser.add_argument(
        "--output-dir",
        default=str(Path.home() / ".cache" / "kairix" / "daily-chunks"),
        help="Output directory for chunk files (default: ~/.cache/kairix/daily-chunks)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without creating files",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    vault_root = Path(args.vault_root)
    output_dir = Path(args.output_dir)

    if not vault_root.exists():
        logger.error("vault root not found: %s", vault_root)
        sys.exit(1)

    logs = find_memory_logs(vault_root)
    logger.info("Found %d memory log files", len(logs))

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    total_chunks = 0
    total_files = 0

    for log_path in logs:
        chunks = chunk_file(log_path, vault_root)
        if not chunks:
            continue
        total_files += 1
        for chunk in chunks:
            total_chunks += 1
            if args.dry_run:
                heading = chunk["heading"] or "(preamble)"
                print(f"  [DRY RUN] {chunk['filename']} — {heading}")
            else:
                write_chunk(chunk, output_dir)

    if args.dry_run:
        logger.info("Dry run: %d chunks from %d files (no files written)", total_chunks, total_files)
    else:
        logger.info("Done: %d chunks written from %d files to %s", total_chunks, total_files, output_dir)
        print(f"\nTo ingest: kairix embed {output_dir}")


if __name__ == "__main__":
    main()
