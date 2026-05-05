#!/usr/bin/env python3
"""
chunk-crm-interactions.py — TMP-3: CRM interaction chunker.

Processes a CRM interaction export (JSON) and writes one chunk file per interaction,
with injected frontmatter so each interaction inherits its date and contact
metadata for kairix ingestion.

Expected input format (JSON array):
    [
      {
        "contact_id": "<uuid>",
        "contact_name": "First Last",
        "company": "Company Name",
        "interactions": [
          {
            "id": "<uuid>",
            "event_time": "2026-04-06T09:00:00Z",
            "meeting_type": "meeting",
            "body": "Met at event. Discussed topics. Follow up on action."
          }
        ]
      }
    ]

The `interactions` array maps directly from your CRM API response.
`event_time` must be an ISO 8601 datetime string.
`meeting_type` is one of: meeting, call, message, email, other.

Output files are written to --output-dir (default: /tmp/crm-chunks/).
They are ephemeral — regenerate by re-running this script.

After running, ingest with:
  kairix embed <output-dir>

Usage:
    python3 chunk-crm-interactions.py --input /path/to/crm-export.json
    python3 chunk-crm-interactions.py --input /path/to/crm-export.json --output-dir /tmp/chunks --dry-run
    python3 chunk-crm-interactions.py --input /path/to/crm-export.json --vault-root ~/vault
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str, maxlen: int = 40) -> str:
    """Convert text to a filename-safe slug."""
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:maxlen]


def _extract_date(event_time: str) -> str | None:
    """
    Extract YYYY-MM-DD from an ISO 8601 datetime string.

    Returns None if parsing fails.
    """
    m = _ISO_DATE_RE.match(event_time.strip())
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def parse_crm_export(path: Path) -> list[dict[str, Any]]:
    """
    Load and validate a CRM export JSON file.

    Returns list of contact dicts, each with 'interactions' list.
    Raises ValueError on invalid format.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Cannot read {path}: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {path}: {e}") from e

    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array in {path}, got {type(data).__name__}")

    return data


def chunk_contact(contact: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Produce per-interaction chunk dicts for a single contact.

    Returns list of dicts: {date, contact_name, company, meeting_type, body, filename}
    Interactions missing event_time or body are skipped with a warning.
    """
    contact_name = contact.get("contact_name", "").strip()
    company = contact.get("company", "").strip()
    interactions: list[dict[str, Any]] = contact.get("interactions") or []

    if not contact_name:
        logger.warning("chunk_contact: contact missing contact_name — skipping (keys: %s)", list(contact.keys())[:10])
        return []

    chunks: list[dict[str, Any]] = []
    for item in interactions:
        event_time = (item.get("event_time") or "").strip()
        body = (item.get("body") or item.get("note") or "").strip()
        meeting_type = (item.get("meeting_type") or "interaction").strip()
        interaction_id = (item.get("id") or "").strip()

        if not event_time:
            logger.warning(
                "chunk_contact: interaction missing event_time for contact %r — skipping",
                contact_name,
            )
            continue
        if not body:
            logger.debug(
                "chunk_contact: interaction with empty body for contact %r at %s — skipping",
                contact_name,
                event_time,
            )
            continue

        date = _extract_date(event_time)
        if date is None:
            logger.warning(
                "chunk_contact: cannot parse date from event_time %r for contact %r — skipping",
                event_time,
                contact_name,
            )
            continue

        contact_slug = _slug(contact_name, 30)
        id_suffix = _slug(interaction_id[:8] if interaction_id else meeting_type, 12)
        filename = f"crm-{contact_slug}-{date}-{id_suffix}.md"

        chunks.append(
            {
                "date": date,
                "contact_name": contact_name,
                "company": company,
                "meeting_type": meeting_type,
                "body": body,
                "filename": filename,
            }
        )

    return chunks


def render_chunk(chunk: dict[str, Any]) -> str:
    """Render a chunk dict as a markdown file with YAML frontmatter."""
    date = chunk["date"]
    contact_name = chunk["contact_name"]
    company = chunk["company"]
    meeting_type = chunk["meeting_type"]
    body = chunk["body"]

    company_line = f'company: "{company}"\n' if company else ""
    heading = f"## {date} — {meeting_type}"

    return (
        f"---\n"
        f"date: {date}\n"
        f'contact: "{contact_name}"\n'
        f"{company_line}"
        f"interaction_type: {meeting_type}\n"
        f"type: crm_interaction\n"
        f"source: crm_crm\n"
        f"---\n\n"
        f"{heading}\n\n"
        f"{body}\n"
    )


def process_export(
    input_path: Path,
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Process a CRM export file, writing chunk files to output_dir.

    Returns (contacts_processed, chunks_written).
    """
    contacts = parse_crm_export(input_path)

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    total_contacts = 0
    total_chunks = 0

    for contact in contacts:
        chunks = chunk_contact(contact)
        if not chunks:
            continue
        total_contacts += 1
        for chunk in chunks:
            total_chunks += 1
            out_path = output_dir / chunk["filename"]
            content = render_chunk(chunk)
            if dry_run:
                logger.info("[dry-run] would write %s", out_path)
            else:
                out_path.write_text(content, encoding="utf-8")
                logger.debug("wrote %s", out_path)

    return total_contacts, total_chunks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1].strip())
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to CRM export JSON file",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / ".cache" / "kairix" / "crm-chunks",
        help="Output directory for chunk files (default: ~/.cache/kairix/crm-chunks)",
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root (not used directly; reserved for future integration)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without writing files",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if not args.input.exists():
        logger.error("input file not found: %s", args.input)
        return 1

    prefix = "[dry-run] " if args.dry_run else ""
    logger.info("%sProcessing %s → %s", prefix, args.input, args.output_dir)

    contacts, chunks = process_export(
        args.input,
        args.output_dir,
        dry_run=args.dry_run,
    )
    logger.info(
        "%s%d contact(s), %d interaction chunk(s) %s",
        prefix,
        contacts,
        chunks,
        "would be written" if args.dry_run else "written",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
