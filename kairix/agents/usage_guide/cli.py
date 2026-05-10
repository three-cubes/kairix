"""
kairix usage-guide — read the agent usage guide from a shell.

Usage:
  kairix usage-guide [topic] [--guide-path PATH] [--json]

Adapter only — business logic lives in
``kairix.use_cases.usage_guide.run_usage_guide``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kairix.use_cases.usage_guide import (
    UsageGuideDeps,
    UsageGuideOutput,
    run_usage_guide,
    usage_guide_output_to_envelope,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kairix usage-guide",
        description="Read the kairix agent usage guide. Filter by topic when given.",
    )
    parser.add_argument(
        "topic",
        nargs="?",
        default="",
        help="Topic filter (e.g. 'temporal', 'entity', 'budget'). Empty returns the full guide.",
    )
    parser.add_argument(
        "--guide-path",
        type=Path,
        default=None,
        help="Override the production guide-file location (operator escape hatch).",
    )
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output JSON envelope")
    return parser


def format_text(out: UsageGuideOutput) -> str:
    """Render a ``UsageGuideOutput`` as the human-readable text the CLI prints."""
    if out.error:
        return f"error: {out.error}"
    if out.topic:
        header = f"Usage guide — topic: {out.topic}"
        return f"{header}\n{'=' * len(header)}\n\n{out.content}"
    return out.content


def main(argv: list[str] | None = None, *, deps: UsageGuideDeps | None = None) -> int:
    """Entry point for ``kairix usage-guide``."""
    args = build_parser().parse_args(argv)

    out = run_usage_guide(args.topic, guide_path=args.guide_path, deps=deps)

    if args.as_json:
        print(json.dumps(usage_guide_output_to_envelope(out), indent=2))
    else:
        print(format_text(out))

    return 1 if out.error else 0


if __name__ == "__main__":
    sys.exit(main())
