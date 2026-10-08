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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairix.platform.warm.runner import WarmDeps, run_warm

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


def build_parser() -> argparse.ArgumentParser:
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
    p.add_argument(
        "--db-path",
        default=None,
        help=(
            "Override the search index SQLite path for this invocation. "
            "When omitted, the default resolution chain (KAIRIX_DB_PATH "
            "env / kairix.config.yaml / ~/.cache/kairix/index.sqlite) runs. "
            "Matches the canonical F30 subprocess seam in "
            "``kairix bootstrap --document-root``; enables outcome tests to "
            "drive a tmp sqlite without touching the process environment "
            "(F2-clean)."
        ),
    )
    p.add_argument(
        "--document-root",
        default=None,
        help=(
            "Override the document root for this invocation. Pairs with "
            "--db-path so the warm-up subprocess can run against a tmp "
            "vault without touching the process environment."
        ),
    )
    return p


def resolve_paths_overlay(db_path: str | None, document_root: str | None) -> Any:
    """Build a :class:`kairix.paths.KairixPaths` overlay reflecting CLI args.

    Resolves the unset fields from :meth:`KairixPaths.resolve` so the
    overlay is additive — operators who only supply ``--db-path`` keep
    their configured ``document_root``, and vice-versa. Pure function
    (no I/O beyond the cached resolve()), unit-testable without
    spinning up build_search_pipeline.
    """
    from kairix.paths import KairixPaths

    defaults = KairixPaths.resolve()
    return KairixPaths(
        document_root=Path(document_root) if document_root else defaults.document_root,
        db_path=Path(db_path) if db_path else defaults.db_path,
        log_dir=defaults.log_dir,
        workspace_root=defaults.workspace_root,
    )


def build_pipeline_builder_for_paths(db_path: str | None, document_root: str | None) -> Any:
    """Construct the ``WarmDeps.build_pipeline`` callable for ``run_warm``.

    When neither override is supplied, returns ``None`` — ``run_warm``
    then uses its default ``_step_build_pipeline`` which resolves paths
    from env / config / platform default. When at least one override is
    supplied, returns a callable that calls
    :func:`kairix.core.factory.build_search_pipeline` with the overlay
    from :func:`resolve_paths_overlay`.

    F30 subprocess seam: outcome tests pass ``--db-path tmp/index.sqlite``
    and ``--document-root tmp`` to drive the warm-up against a tmp
    sandbox without setting ``KAIRIX_*`` env vars.
    """
    if db_path is None and document_root is None:
        return None

    overlay = resolve_paths_overlay(db_path, document_root)

    def _builder() -> Any:
        from kairix.core.factory import build_search_pipeline

        return build_search_pipeline(paths=overlay)

    return _builder


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


@dataclass(frozen=True)
class WarmCliDeps:
    """Injectable collaborators for :func:`main` (canonical Deps shape).

    - ``run_warm``: the warm-up runner. Production default is
      :func:`kairix.platform.warm.runner.run_warm`; tests inject a callable
      returning a canned :class:`WarmResult` to drive the exit-code and
      rendering logic without patching the module (F1).
    """

    run_warm: Callable[..., WarmResult] = field(default_factory=lambda: run_warm)


def main(argv: list[str] | None = None, deps: WarmCliDeps | None = None) -> int:
    deps = deps if deps is not None else WarmCliDeps()
    args = build_parser().parse_args(argv)
    builder = build_pipeline_builder_for_paths(args.db_path, args.document_root)
    result = deps.run_warm(deps=WarmDeps(build_pipeline=builder)) if builder is not None else deps.run_warm()
    if args.json:
        print(json.dumps(result.to_envelope(), indent=2))
    else:
        print(_format_text(result))
    return 0 if result.ok else 1
