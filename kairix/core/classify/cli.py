"""
kairix classify — auto-classify memory writes.

Usage:
  kairix classify "<content>" [--agent <agent>]
  echo "<content>" | kairix classify --agent builder

Output: JSON to stdout
  {"type": "...", "target_path": "...", "confidence": 0.xx, "reason": "..."}
  {"type": "...", "target_path": "...", "confidence": 0.xx, "reason": "...", "needs_confirmation": true}
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any


def _resolve_classifiers(
    rule_classifier: Callable[..., Any] | None,
    llm_classifier: Callable[..., Any] | None,
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """Resolve the production rule + LLM classifier when callers leave them ``None``.

    Lazy-imports keep heavy modules out of the CLI's import path until
    actually invoked; tests inject fakes through the public seams.
    """
    if rule_classifier is None:
        from kairix.core.classify.rules import classify_content

        rule_classifier = classify_content
    if llm_classifier is None:
        from kairix.core.classify.judge import classify_with_llm

        llm_classifier = classify_with_llm
    return rule_classifier, llm_classifier


def main(
    args: list[str] | None = None,
    *,
    rule_classifier: Callable[..., Any] | None = None,
    llm_classifier: Callable[..., Any] | None = None,
) -> None:
    """Entry point for `kairix classify`.

    ``rule_classifier`` and ``llm_classifier`` are the public DI seams for
    tests that want to drive error paths through the public CLI surface
    instead of monkey-patching the classify-module imports. Production
    callers leave them at ``None`` and the CLI lazy-imports the real ones.
    """
    import argparse

    rule_classifier, llm_classifier = _resolve_classifiers(rule_classifier, llm_classifier)

    if args is None:
        args = sys.argv[2:]  # strip 'kairix classify'

    parser = argparse.ArgumentParser(
        prog="kairix classify",
        description="Auto-classify memory writes to the correct document path.",
    )
    parser.add_argument(
        "content",
        nargs="?",
        default=None,
        help="Content to classify (or pipe via stdin).",
    )
    parser.add_argument(
        "--agent",
        default="shared",
        help="Agent name for path scoping (builder, shape, growth, consultant, shared).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=False,
        help="Disable LLM fallback — return unknown if no rule matches.",
    )

    parsed = parser.parse_args(args)

    # Get content
    content = parsed.content
    if content is None:
        if not sys.stdin.isatty():
            content = sys.stdin.read()
        else:
            print(
                "Error: no content provided (pass as argument or pipe via stdin)",
                file=sys.stderr,
            )
            sys.exit(1)

    agent = parsed.agent
    use_llm = not parsed.no_llm

    # Run classification
    try:
        from kairix.core.classify.rules import VALID_AGENTS

        if agent not in VALID_AGENTS:
            print(
                f"Error: invalid agent {agent!r}. Must be one of: {sorted(VALID_AGENTS)}",
                file=sys.stderr,
            )
            sys.exit(1)

        result = rule_classifier(content, agent=agent)

        # If rule didn't match, try LLM judge
        if result.type == "unknown" and use_llm:
            result = llm_classifier(content, agent=agent)

        output: dict = {
            "type": result.type,
            "target_path": result.target_path,
            "confidence": round(result.confidence, 2),
            "reason": result.reason,
        }
        if result.needs_confirmation:
            output["needs_confirmation"] = True

        print(json.dumps(output))

    except ValueError as e:
        print(
            json.dumps({"error": "Classification failed — check server logs"}),
            file=sys.stderr,
        )
        import logging as _logging

        _logging.getLogger(__name__).warning("classify CLI ValueError: %s", e)
        sys.exit(1)
    except Exception as e:
        print(
            json.dumps({"error": "Classification failed — check server logs"}),
            file=sys.stderr,
        )
        import logging as _logging

        _logging.getLogger(__name__).warning("classify CLI unexpected error: %s", e)
        sys.exit(1)
