"""Extract a single section from CHANGELOG.md by version (or 'Unreleased').

Used by ``.github/workflows/release.yml`` to derive GitHub Release notes
from the ``[Unreleased]`` section of CHANGELOG.md without an operator
copy-pasting between files.

Usage:
    python scripts/extract_changelog_section.py [--version Unreleased] [CHANGELOG_PATH]

Prints the section's body (the lines under ``## [<version>]`` up to
the next ``## `` heading) to stdout. Exits 1 with a clear error when
the section can't be found.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


_HEADING_RE = re.compile(r"^## \[(?P<version>[^\]]+)\]")


def extract_section(text: str, version: str) -> str:
    """Return the body of ``## [<version>] ...`` up to the next ``## `` heading.

    The heading line itself is excluded from the body. Trailing blank
    lines are stripped. Raises ``KeyError`` when the section is absent.
    """
    lines = text.splitlines()
    start: int | None = None

    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match and match.group("version") == version:
            start = i
            break

    if start is None:
        raise KeyError(f"section [{version}] not found in CHANGELOG")

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break

    body_lines = lines[start + 1 : end]
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)

    return "\n".join(body_lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract a CHANGELOG section by version.")
    parser.add_argument(
        "changelog",
        nargs="?",
        type=Path,
        default=Path("CHANGELOG.md"),
        help="Path to CHANGELOG.md (default: ./CHANGELOG.md)",
    )
    parser.add_argument(
        "--version",
        default="Unreleased",
        help="Version label to extract (default: Unreleased)",
    )
    args = parser.parse_args()

    if not args.changelog.is_file():
        print(f"error: changelog file not found: {args.changelog}", file=sys.stderr)
        return 1

    text = args.changelog.read_text(encoding="utf-8")
    try:
        body = extract_section(text, args.version)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not body.strip():
        print(f"error: section [{args.version}] is empty", file=sys.stderr)
        return 1

    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
