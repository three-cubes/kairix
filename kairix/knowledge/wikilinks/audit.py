"""
Wikilink audit functions for kairix.

Provides:
- find_broken_links(): vault wikilinks pointing to non-existent paths
- find_unlinked_mentions(): entity mentions in eligible files that lack wikilinks
- weekly_report(): markdown summary of wikilink health
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kairix.knowledge.wikilinks import WIKILINK_RE
from kairix.knowledge.wikilinks.injector import MAX_FILE_SIZE, should_inject
from kairix.knowledge.wikilinks.resolver import WikiEntity
from kairix.paths import KairixPaths

_LOG_PATH = str(Path.home() / ".cache" / "kairix" / "wikilinks-log.jsonl")

# Markdown table separator row used in the report's 3-column tables (Broken,
# Unlinked, Audit-Summary sections all share the same column layout).
_MD_TABLE_SEPARATOR_3COL = "|---|---|---|"

# Canonical wikilink regex (excludes anchor links)
_WIKILINK_RE = WIKILINK_RE


# ---------------------------------------------------------------------------
# Broken link detection
# ---------------------------------------------------------------------------


def find_broken_links(
    document_root: str = str(Path.home() / "kairix-vault"),
    vault_root: str | None = None,
) -> list[dict[str, Any]]:
    """
    Scan document store for [[wikilinks]] pointing to non-existent files/folders.

    Only checks wikilinks to entities with a vault_path property set.
    Returns list of dicts: {file, link, reason}.
    """
    from kairix.knowledge.wikilinks.resolver import get_entities

    root = vault_root or document_root
    entities = get_entities()
    # Build lookup: wikilink target → vault_path
    target_to_path: dict[str, str] = {}
    for entity in entities:
        # Extract link target from [[target]] or [[target|display]]
        m = re.match(r"\[\[([^\]|]+)", entity.link)
        if m:
            target_to_path[m.group(1)] = entity.vault_path

    doc_path = Path(root)
    results: list[dict[str, Any]] = []

    # Walk all markdown files in document store
    for md_file in doc_path.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for link_match in _WIKILINK_RE.finditer(content):
            link_target = link_match.group(1)
            if link_target not in target_to_path:
                continue  # Not a tracked entity link, skip

            expected_path = target_to_path[link_target]
            full_expected = doc_path / expected_path

            # Check if path exists (as file or directory)
            if not full_expected.exists():
                # Also check without trailing slash
                alt_path = doc_path / expected_path.rstrip("/")
                if not alt_path.exists():
                    rel_file = str(md_file.relative_to(doc_path))
                    results.append(
                        {
                            "file": rel_file,
                            "link": f"[[{link_target}]]",
                            "reason": f"vault_path not found: {expected_path}",
                        }
                    )

    return results


# ---------------------------------------------------------------------------
# Unlinked mention sampling
# ---------------------------------------------------------------------------


def find_unlinked_mentions(
    document_root: str,
    entities: list[WikiEntity],
    sample_size: int = 50,
    *,
    paths: KairixPaths | None = None,
) -> list[dict[str, Any]]:
    """
    Sample eligible files and find entity mentions that are NOT wikilinked.

    Returns list of dicts: {file, entity_name, mention_count}.
    Used to estimate injection backlog.

    Args:
        document_root: Path to the document store root (string).
        entities:      Entities eligible for injection.
        sample_size:   Maximum number of files to sample.
        paths:         Injected ``KairixPaths`` controlling workspace
                       discovery and ``should_inject`` eligibility. When
                       ``None``, falls back to ``KairixPaths.resolve()``.
    """
    paths = paths or KairixPaths.resolve()

    doc_path = Path(document_root)

    # Gather eligible files
    eligible: list[Path] = []
    for md_file in doc_path.rglob("*.md"):
        if should_inject(str(md_file), paths=paths):
            eligible.append(md_file)

    # Also check workspace memory files
    workspaces_root = paths.workspace_root
    if workspaces_root.exists():
        for md_file in workspaces_root.rglob("*.md"):
            if should_inject(str(md_file), paths=paths):
                eligible.append(md_file)

    # Sample
    if len(eligible) > sample_size:
        sampled = random.sample(
            eligible, sample_size
        )  # NOSONAR — non-security sampling for human audit review; no trust boundary.
    else:
        sampled = list(eligible)

    results: list[dict[str, Any]] = []
    for md_file in sampled:
        try:
            size = md_file.stat().st_size
            if size > MAX_FILE_SIZE:
                continue
            content = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # Find already-linked entity names in this file
        already_linked: set[str] = set()
        for m in _WIKILINK_RE.finditer(content):
            already_linked.add(m.group(1))
            if m.lastindex and m.lastindex >= 2:
                display = re.search(r"\[\[[^\]|]+\|([^\]]+)\]\]", m.group(0))
                if display:
                    already_linked.add(display.group(1))

        for entity in entities:
            # Skip if already linked
            if any(t in already_linked for t in entity.all_triggers()):
                continue

            # Count plain-text mentions (whole word)
            count = 0
            for trigger in entity.all_triggers():
                escaped = re.escape(trigger)
                pattern = rf"(?<!\w){escaped}(?!\w)"
                count += len(re.findall(pattern, content, re.IGNORECASE))

            if count > 0:
                md_str = str(md_file)
                doc_str = str(doc_path)
                rel_file = str(md_file.relative_to(doc_path)) if md_str.startswith(doc_str) else md_str
                results.append(
                    {
                        "file": rel_file,
                        "entity_name": entity.name,
                        "mention_count": count,
                    }
                )

    # Sort by mention count descending
    results.sort(key=lambda x: -x["mention_count"])
    return results


# ---------------------------------------------------------------------------
# Weekly report
# ---------------------------------------------------------------------------


def _render_broken_links(broken: list[dict[str, Any]]) -> list[str]:
    """Render the 'Broken Links' section as markdown lines."""
    lines = ["## Broken Links", ""]
    if not broken:
        lines += ["✅ No broken links detected.", ""]
        return lines
    lines += [
        f"Found **{len(broken)}** broken wikilink(s):",
        "",
        "| File | Link | Reason |",
        _MD_TABLE_SEPARATOR_3COL,
    ]
    for item in broken[:20]:
        lines.append(f"| {item['file']} | {item['link']} | {item['reason']} |")
    if len(broken) > 20:
        lines.append(f"| _(and {len(broken) - 20} more)_ | | |")
    lines.append("")
    return lines


def _render_unlinked_mentions(unlinked: list[dict[str, Any]]) -> list[str]:
    """Render the 'Unlinked Mentions' sample section."""
    lines = ["## Unlinked Mentions (sample)", ""]
    if not unlinked:
        lines += ["✅ No unlinked mentions found in sampled files.", ""]
        return lines
    lines += [
        f"Found **{len(unlinked)}** unlinked entity mention(s) in sampled files:",
        "",
        "| File | Entity | Mentions |",
        _MD_TABLE_SEPARATOR_3COL,
    ]
    for item in unlinked[:20]:
        lines.append(f"| {item['file']} | {item['entity_name']} | {item['mention_count']} |")
    if len(unlinked) > 20:
        lines.append(f"| _(and {len(unlinked) - 20} more)_ | | |")
    lines.append("")
    return lines


def _render_recent_injections(recent: list[dict[str, Any]]) -> list[str]:
    """Render the 'Recent Injections' summary + per-file table."""
    lines = ["## Recent Injections (last 7 days)", ""]
    if not recent:
        lines += ["No injections recorded in the last 7 days.", ""]
        return lines
    injected_count = sum(len(e.get("injected", [])) for e in recent)
    dry_runs = sum(1 for e in recent if e.get("dry_run"))
    real_runs = len(recent) - dry_runs
    lines += [
        "| Metric | Value |",
        "|---|---|",
        f"| Files processed | {len(recent)} |",
        f"| Real injections | {real_runs} |",
        f"| Dry runs | {dry_runs} |",
        f"| Total wikilinks injected | {injected_count} |",
        "",
        "### Recent Files",
        "",
        "| File | Entities Injected | Mode |",
        _MD_TABLE_SEPARATOR_3COL,
    ]
    for entry in recent[-10:]:
        mode = "dry-run" if entry.get("dry_run") else "live"
        injected_list = ", ".join(entry.get("injected", []))
        lines.append(f"| {entry.get('file', '?')} | {injected_list} | {mode} |")
    lines.append("")
    return lines


def weekly_report(
    document_root: str,
    entities: list[WikiEntity],
    *,
    paths: KairixPaths | None = None,
) -> str:
    """
    Generate markdown weekly audit report covering:
    - Total entities in ontology (with/without vault_path)
    - Broken links found
    - Sample of unlinked mentions
    - Files injected since last report (from injection log)

    Args:
        document_root: Path to the document store root (string).
        entities:      Entities eligible for injection.
        paths:         Injected ``KairixPaths`` for workspace discovery in
                       unlinked-mention sampling. When ``None``, falls back
                       to ``KairixPaths.resolve()``.
    """
    paths = paths or KairixPaths.resolve()

    now = datetime.now(timezone.utc)
    report_date = now.strftime("%Y-%m-%d")

    total_entities = len(entities)
    with_vault_path = sum(1 for e in entities if e.vault_path)
    without_vault_path = total_entities - with_vault_path

    broken = find_broken_links(document_root)
    unlinked = find_unlinked_mentions(document_root, entities, sample_size=50, paths=paths)
    recent_injections = _read_recent_log(days=7)

    lines: list[str] = [
        f"# Wikilink Audit Report — {report_date}",
        "",
        f"_Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        "## Entity Ontology",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| Total entities | {total_entities} |",
        f"| With vault_path (linkable) | {with_vault_path} |",
        f"| Without vault_path (not linked) | {without_vault_path} |",
        "",
    ]
    lines += _render_broken_links(broken)
    lines += _render_unlinked_mentions(unlinked)
    lines += _render_recent_injections(recent_injections)
    lines += [
        "---",
        "_Report generated by `kairix wikilinks audit`_",
    ]
    return "\n".join(lines)


def _read_recent_log(days: int = 7) -> list[dict[str, Any]]:
    """Read injection log entries from the last N days."""
    cutoff = time.time() - (days * 86400)
    entries: list[dict[str, Any]] = []
    try:
        with open(_LOG_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("ts", 0) >= cutoff:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries
