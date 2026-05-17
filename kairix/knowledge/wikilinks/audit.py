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

# Canonical wikilink regex (excludes anchor links)
_WIKILINK_RE = WIKILINK_RE

# Markdown table divider for the 3-column report tables rendered below.
# Sonar python:S1192 — single source of truth for the repeated literal.
_MD_TABLE_DIVIDER_3COL = "|---|---|---|"


# ---------------------------------------------------------------------------
# Broken link detection
# ---------------------------------------------------------------------------


def _build_target_to_path(entities: list[WikiEntity]) -> dict[str, str]:
    """Map each entity's wikilink target (``[[target]]``) onto its vault_path."""
    target_to_path: dict[str, str] = {}
    for entity in entities:
        m = re.match(r"\[\[([^\]|]+)", entity.link)
        if m:
            target_to_path[m.group(1)] = entity.vault_path
    return target_to_path


def _broken_link_rows(
    md_file: Path,
    content: str,
    doc_path: Path,
    target_to_path: dict[str, str],
) -> list[dict[str, Any]]:
    """Emit a row per [[wikilink]] in ``content`` whose tracked vault_path is missing."""
    rows: list[dict[str, Any]] = []
    for link_match in _WIKILINK_RE.finditer(content):
        link_target = link_match.group(1)
        expected_path = target_to_path.get(link_target)
        if expected_path is None:
            continue  # not a tracked entity link
        full_expected = doc_path / expected_path
        if full_expected.exists():
            continue
        # Tolerate trailing-slash differences in the vault_path.
        alt_path = doc_path / expected_path.rstrip("/")
        if alt_path.exists():
            continue
        rows.append(
            {
                "file": str(md_file.relative_to(doc_path)),
                "link": f"[[{link_target}]]",
                "reason": f"vault_path not found: {expected_path}",
            }
        )
    return rows


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

    target_to_path = _build_target_to_path(get_entities())
    doc_path = Path(vault_root or document_root)
    results: list[dict[str, Any]] = []
    for md_file in doc_path.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        results.extend(_broken_link_rows(md_file, content, doc_path, target_to_path))
    return results


# ---------------------------------------------------------------------------
# Unlinked mention sampling
# ---------------------------------------------------------------------------


def _gather_audit_files(doc_path: Path, paths: KairixPaths) -> list[Path]:
    """Collect every ``should_inject``-eligible .md file under doc_path + workspaces."""
    eligible: list[Path] = []
    for md_file in doc_path.rglob("*.md"):
        if should_inject(str(md_file), paths=paths):
            eligible.append(md_file)
    workspaces_root = paths.workspace_root
    if workspaces_root.exists():
        for md_file in workspaces_root.rglob("*.md"):
            if should_inject(str(md_file), paths=paths):
                eligible.append(md_file)
    return eligible


def _read_audit_file(md_file: Path, *, max_file_size: int = MAX_FILE_SIZE) -> str | None:
    """Read an audit-eligible .md file; return None when oversize or unreadable.

    ``max_file_size`` is the public threshold seam — production callers
    leave it at the module-level ``MAX_FILE_SIZE``; tests pass 0 to
    exercise the oversize-skip branch without monkey-patching the
    audit-module constant.
    """
    try:
        if md_file.stat().st_size > max_file_size:
            return None
        return md_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _already_linked_names(content: str) -> set[str]:
    """Collect every entity-name that already appears inside ``[[wikilinks]]``."""
    already_linked: set[str] = set()
    for m in _WIKILINK_RE.finditer(content):
        already_linked.add(m.group(1))
        if m.lastindex and m.lastindex >= 2:
            display = re.search(r"\[\[[^\]|]+\|([^\]]+)\]\]", m.group(0))
            if display:
                already_linked.add(display.group(1))
    return already_linked


def _count_plain_mentions(entity: WikiEntity, content: str) -> int:
    """Count whole-word plain-text mentions of every trigger for ``entity``."""
    total = 0
    for trigger in entity.all_triggers():
        escaped = re.escape(trigger)
        pattern = rf"(?<!\w){escaped}(?!\w)"
        total += len(re.findall(pattern, content, re.IGNORECASE))
    return total


def _relative_audit_path(md_file: Path, doc_path: Path) -> str:
    """Convert an absolute path to one relative to ``doc_path`` when nested under it."""
    md_str = str(md_file)
    doc_str = str(doc_path)
    return str(md_file.relative_to(doc_path)) if md_str.startswith(doc_str) else md_str


def _scan_file_for_unlinked(
    md_file: Path,
    doc_path: Path,
    entities: list[WikiEntity],
) -> list[dict[str, Any]]:
    """Emit one row per (entity, count>0) pair for unlinked mentions in this file."""
    content = _read_audit_file(md_file)
    if content is None:
        return []
    already_linked = _already_linked_names(content)
    rel_file = _relative_audit_path(md_file, doc_path)
    rows: list[dict[str, Any]] = []
    for entity in entities:
        if any(t in already_linked for t in entity.all_triggers()):
            continue
        count = _count_plain_mentions(entity, content)
        if count > 0:
            rows.append({"file": rel_file, "entity_name": entity.name, "mention_count": count})
    return rows


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
    eligible = _gather_audit_files(doc_path, paths)

    if len(eligible) > sample_size:
        # NOSONAR: non-security audit sampling — picking a representative
        # subset of files for human review; no security boundary.
        sampled = random.sample(eligible, sample_size)
    else:
        sampled = list(eligible)

    results: list[dict[str, Any]] = []
    for md_file in sampled:
        results.extend(_scan_file_for_unlinked(md_file, doc_path, entities))

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
        _MD_TABLE_DIVIDER_3COL,
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
        _MD_TABLE_DIVIDER_3COL,
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
        _MD_TABLE_DIVIDER_3COL,
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
    log_path: str | None = None,
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
        log_path:      Public seam for the injection-log file path.
                       Production callers leave it ``None`` and the function
                       threads through to the module-level ``_LOG_PATH``;
                       tests pass a tmp-path so they exercise the
                       missing-log / valid-entries / out-of-window branches
                       without monkey-patching ``_LOG_PATH``.
    """
    paths = paths or KairixPaths.resolve()

    now = datetime.now(timezone.utc)
    report_date = now.strftime("%Y-%m-%d")

    total_entities = len(entities)
    with_vault_path = sum(1 for e in entities if e.vault_path)
    without_vault_path = total_entities - with_vault_path

    broken = find_broken_links(document_root)
    unlinked = find_unlinked_mentions(document_root, entities, sample_size=50, paths=paths)
    recent_injections = _read_recent_log(days=7, log_path=log_path)

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


def _read_recent_log(days: int = 7, *, log_path: str | None = None) -> list[dict[str, Any]]:
    """Read injection log entries from the last N days.

    ``log_path`` is the public seam — production callers leave it
    ``None`` and the function reads from the module-level ``_LOG_PATH``;
    tests pass a tmp_path-derived path to drive the
    missing-file / valid-entries / out-of-window branches without
    monkey-patching the module constant.
    """
    cutoff = time.time() - (days * 86400)
    entries: list[dict[str, Any]] = []
    effective_path = log_path if log_path is not None else _LOG_PATH
    try:
        with open(effective_path, encoding="utf-8") as fh:
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
