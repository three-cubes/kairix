"""Append one row to ``benchmark-results/history/INDEX.md`` and archive the JSON.

Called by ``.github/workflows/reflib-history-capture.yml`` when a release is
created. Reads a fresh benchmark output JSON (produced by
``kairix benchmark run --suite reflib``), writes it to
``benchmark-results/history/<tag>-<date>.json``, and appends a row to
``benchmark-results/history/INDEX.md``.

Idempotent: re-running with the same tag does not double-add the row or
overwrite the archived JSON. Re-running with a *different* result JSON for
the same tag fails fast — the workflow should never overwrite history.

Usage:
    python3 scripts/update_reflib_history.py RESULT_JSON --tag TAG [--date YYYY-MM-DD] [--history-dir DIR]

Exit codes:
    0 — appended (or no-op when already present)
    1 — error (missing input, malformed JSON, tag re-use with different data)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date as date_cls
from pathlib import Path
from typing import Any

DEFAULT_HISTORY_DIR = Path("benchmark-results/history")
INDEX_FILENAME = "INDEX.md"

# Categories rendered in the INDEX table, in column order. Keep aligned with
# the column header in INDEX.md so a sabotage of the header forces a sabotage
# of this constant (and vice versa).
CATEGORY_COLUMNS: tuple[str, ...] = (
    "conceptual",
    "recall",
    "temporal",
    "entity",
    "multi_hop",
    "procedural",
)

# Tag pattern matches the release.yml validation (CalVer vYYYY.M.D[.N]) so the
# archive filename can be parsed back to a release.
_TAG_RE = re.compile(r"^v[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}(\.[0-9]+)?$")


# ---------------------------------------------------------------------------
# Pure helpers — no I/O, easy to unit-test.
# ---------------------------------------------------------------------------


def validate_tag(tag: str) -> None:
    """Raise ``ValueError`` if ``tag`` doesn't match the release CalVer pattern.

    The pattern mirrors ``.github/workflows/release.yml`` — keeping the two
    in sync means an archive entry can always be traced back to a real tag.
    """
    if not _TAG_RE.match(tag):
        raise ValueError(
            f"tag '{tag}' does not match CalVer vYYYY.M.D[.N]. "
            "fix: pass --tag from the release workflow (github.ref_name). "
            "run: python3 scripts/update_reflib_history.py <result.json> --tag vYYYY.M.D"
        )


def archive_filename(tag: str, date: str) -> str:
    """Compose the per-tag archive filename (``<tag>-<date>.json``)."""
    return f"{tag}-{date}.json"


def _fmt_score(value: Any) -> str:
    """Format a numeric score to 3dp, with ``-`` for missing/non-numeric."""
    if value is None:
        return "-"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "-"


def format_index_row(tag: str, date: str, summary: dict[str, Any]) -> str:
    """Build the markdown table row for one release.

    The row links to the per-tag JSON (``<tag>-<date>.json``) and renders
    weighted_total, NDCG@10, Hit@5 and the six category scores. Missing
    values render as ``-`` rather than ``0.000`` so a gap in capture is
    visible at a glance.
    """
    archive = archive_filename(tag, date)
    weighted = _fmt_score(summary.get("weighted_total"))
    ndcg = _fmt_score(summary.get("ndcg_at_10"))
    hit5 = _fmt_score(summary.get("hit_rate_at_5"))
    cats = summary.get("category_scores", {}) or {}
    cat_cells = " | ".join(_fmt_score(cats.get(c)) for c in CATEGORY_COLUMNS)
    return (
        f"| [{tag}]({archive}) | {date} | {weighted} | {ndcg} | {hit5} | {cat_cells} |"
    )


def index_has_tag(index_text: str, tag: str) -> bool:
    """Return True if the INDEX already lists this tag (idempotency check)."""
    # Match the link cell ``[v2026.5.10.1](...)`` — robust against
    # leading whitespace or trailing-cell variations.
    return bool(re.search(rf"\|\s*\[{re.escape(tag)}\]\(", index_text))


def append_row(index_text: str, row: str) -> str:
    """Append ``row`` to the end of ``index_text``.

    Preserves a single trailing newline. ``append`` semantics mean newest
    rows land at the bottom — chronological reading order.
    """
    stripped = index_text.rstrip("\n")
    return stripped + "\n" + row + "\n"


# ---------------------------------------------------------------------------
# I/O orchestration.
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(
            f"cannot read result JSON: {exc}. "
            f"fix: pass the path emitted by `kairix benchmark run --output`. "
            f"run: kairix benchmark run --suite reflib --output benchmark-results/history/"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"result JSON at {path} is malformed: {exc}. "
            f"fix: re-run the benchmark with --output and pass the produced file unchanged. "
            f"next: cat the path and confirm it parses as JSON."
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"result JSON at {path} is not an object — got {type(data).__name__}. "
            f"fix: pass the JSON emitted by `kairix benchmark run`, not a wrapping array."
        )
    return data


def _resolve_date(arg_date: str | None, result_meta: dict[str, Any]) -> str:
    """Pick the capture date: CLI arg > result.meta.generated > today (UTC)."""
    if arg_date:
        return arg_date
    generated = result_meta.get("generated")
    if isinstance(generated, str) and generated:
        return generated
    return date_cls.today().isoformat()


def update_history(
    result_json_path: Path,
    tag: str,
    history_dir: Path,
    date_override: str | None = None,
) -> tuple[Path, bool]:
    """Archive the JSON + append the INDEX row. Returns ``(archive_path, appended)``.

    ``appended`` is ``False`` when the tag was already present (idempotent
    no-op). Re-running with the same tag but *different* JSON content raises
    ``ValueError`` — history is append-only by design.
    """
    validate_tag(tag)

    data = _read_json(result_json_path)
    summary = data.get("summary", {}) or {}
    meta = data.get("meta", {}) or {}
    date = _resolve_date(date_override, meta)

    history_dir.mkdir(parents=True, exist_ok=True)
    archive_path = history_dir / archive_filename(tag, date)
    new_payload = json.dumps(data, indent=2, sort_keys=True) + "\n"

    if archive_path.exists():
        existing = archive_path.read_text(encoding="utf-8")
        if existing.strip() != new_payload.strip():
            raise ValueError(
                f"archive {archive_path} exists with different content. "
                f"fix: history is append-only — pick a new --date or restore the prior JSON. "
                f"next: diff the two payloads before deciding."
            )
    else:
        archive_path.write_text(new_payload, encoding="utf-8")

    index_path = history_dir / INDEX_FILENAME
    if not index_path.exists():
        raise FileNotFoundError(
            f"{index_path} not found. "
            f"fix: commit the INDEX.md scaffold from issue #271 before running this script. "
            f"run: ls benchmark-results/history/"
        )

    index_text = index_path.read_text(encoding="utf-8")
    if index_has_tag(index_text, tag):
        return archive_path, False

    row = format_index_row(tag, date, summary)
    index_path.write_text(append_row(index_text, row), encoding="utf-8")
    return archive_path, True


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive a reflib benchmark JSON + append a row to INDEX.md.",
    )
    parser.add_argument(
        "result_json",
        type=Path,
        help="Path to the benchmark output JSON (from `kairix benchmark run --output`).",
    )
    parser.add_argument(
        "--tag",
        required=True,
        help="Release tag (e.g. v2026.5.10.1) — must match release.yml CalVer.",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Capture date (YYYY-MM-DD). Defaults to result.meta.generated, else today.",
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=DEFAULT_HISTORY_DIR,
        help="History root (default: benchmark-results/history).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        archive_path, appended = update_history(
            result_json_path=args.result_json,
            tag=args.tag,
            history_dir=args.history_dir,
            date_override=args.date,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if appended:
        print(f"appended: {args.tag} -> {archive_path}")
    else:
        print(f"no-op: {args.tag} already in INDEX.md (archive: {archive_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
