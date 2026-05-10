"""CLI entry point for the kairix setup wizard."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kairix.platform.setup.prompts import SetupContext


def main(argv: list[str] | None = None, *, ctx: SetupContext | None = None) -> None:
    """Entry point for `kairix setup`.

    The ``ctx`` keyword lets BDD/integration tests pass an explicit
    ``SetupContext`` (interactive=False, json_mode=…, deterministic
    state_path) instead of relying on ``SetupContext.auto_detect()``,
    which reads ``$XDG_CONFIG_HOME``, ``$CI``, and stdout TTY state.
    """
    parser = argparse.ArgumentParser(
        prog="kairix setup",
        description="Interactive setup wizard — configures LLM, documents, and search in a few steps",
    )
    parser.add_argument(
        "--output",
        default="kairix.config.yaml",
        help="Output config file path (default: kairix.config.yaml)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip all prompts, use defaults (for CI/Docker/scripting)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output config as JSON to stdout instead of writing YAML file",
    )
    parser.add_argument(
        "--preset",
        choices=[
            "consulting",
            "technical",
            "daily-log",
            "general",
            "agent-memory",
            "exploring",
        ],
        default=None,
        help="Use a preset configuration (skips the use-case survey)",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="Document root path (skips the document source prompt)",
    )
    args = parser.parse_args(argv)

    from kairix.platform.setup.wizard import run_setup

    if ctx is None:
        from kairix.platform.setup.prompts import SetupContext

        ctx = SetupContext.auto_detect(
            non_interactive=args.non_interactive,
            json_mode=args.json,
        )

    success = run_setup(
        ctx=ctx,
        output_path=args.output,
        preset=args.preset,
        document_path=args.path,
    )
    sys.exit(0 if success else 1)
