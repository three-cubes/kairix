"""`kairix warm` — CLI binding over `kairix.platform.warm.run_warm`.

Operator surface for the warm-up capability. Container entrypoints
should invoke this before flipping `/healthz/ready` to 200 so the
agent's first request never pays the factory-init + cache-population
cost (#278).

Exit-code semantics:
    0 — every warm-up step succeeded
    1 — at least one step failed; partial warm-up may still be useful
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

from kairix.platform.warm.runner import run_warm

if TYPE_CHECKING:
    from kairix.platform.warm.runner import WarmResult


_HELP_DESCRIPTION = """\
Warm kairix caches so the first agent request lands hot.

Builds the search pipeline (pays factory init: DB connection, Azure
embed client, BM25/vector backend instantiation), issues one no-op
probe so per-call caches populate, and opens the Neo4j driver.

Run at container start, BEFORE /healthz/ready flips to 200. The agent's
first tool_search then finds the pipeline warm.

MCP equivalent: tool_warm — same envelope; safe for agents to call as a
                'is kairix warm?' probe (idempotent; fast once warm).

See: docs/architecture/operational-tests-design.md
"""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kairix warm",
        description=_HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON envelope on stdout; suppress human-readable output",
    )
    return p


def _format_text(result: WarmResult) -> str:
    """Render a WarmResult as the human-readable operator report."""
    lines: list[str] = [f"warm: total={result.total_duration_s}s"]
    for step in result.steps:
        status = "OK" if step.ok else "FAIL"
        suffix = f"  ({step.detail})" if step.detail else ""
        lines.append(f"  {status:4}  {step.name:24s}  {step.duration_s}s{suffix}")
    lines.append("")
    if result.ok:
        lines.append("warm-up complete — pipeline + caches hot")
    else:
        lines.append(f"warm-up partial — {len(result.failures)} step(s) failed:")
        for f in result.failures:
            lines.append(f"  - [{f.step}] {f.detail}")
        lines.append("")
        lines.append("fix: investigate the failing step; agent requests may pay extra cold-start cost until resolved")
        lines.append("next: re-run 'kairix warm' once the underlying issue is fixed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = run_warm()
    if args.json:
        print(json.dumps(result.to_envelope(), indent=2))
    else:
        print(_format_text(result))
    return 0 if result.ok else 1
