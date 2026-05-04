"""kairix config validate — schema validation for kairix.config.yaml.

Operator-facing utility that reads the YAML, parses collections + agents,
and reports any structural issues. Exits non-zero on errors so it can
be wired into CI pre-deploy checks.

Validates:
  - Each collection has a name (required) and a path (required).
  - Each agent has a name; collection names match the agent_pattern (or
    are explicitly declared); write_paths are non-overlapping.
  - retrieval_overrides keys (when present) name fields that exist on
    RetrievalConfig — silent typos in this section are a common operator
    mistake.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# RetrievalConfig fields valid as override keys. Kept as a literal list so we
# don't drag the dataclass in as a runtime dependency for validation alone.
_VALID_OVERRIDE_KEYS = frozenset(
    {
        "fusion_strategy",
        "rrf_k",
        "bm25_limit",
        "vec_limit",
        "skip_vector",
        "entity",
        "procedural",
        "temporal",
        "rerank",
        "rerank_intents",
    }
)


def validate_config(data: dict[str, Any]) -> list[str]:
    """Validate a parsed kairix.config.yaml dict.

    Returns a list of human-readable error messages. Empty list means valid.
    Never raises — returns errors as strings.
    """
    errors: list[str] = []
    errors.extend(_validate_collections(data.get("collections")))
    errors.extend(_validate_agents(data.get("agents"), data.get("collections")))
    return errors


def _validate_collections(collections: Any) -> list[str]:
    errors: list[str] = []
    if collections is None:
        return errors  # absence is valid (search-everything fallback)
    if not isinstance(collections, dict):
        return ["collections: must be a mapping"]

    shared = collections.get("shared", [])
    if not isinstance(shared, list):
        errors.append("collections.shared: must be a list")
        return errors

    seen_names: set[str] = set()
    for i, item in enumerate(shared):
        prefix = f"collections.shared[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: must be a mapping with name + path")
            continue
        name = item.get("name")
        if not name:
            errors.append(f"{prefix}: missing required 'name'")
            continue
        if name in seen_names:
            errors.append(f"{prefix}: duplicate collection name {name!r}")
        seen_names.add(name)
        if "path" not in item:
            errors.append(f"{prefix} ({name}): missing required 'path'")

        overrides = item.get("retrieval")
        if overrides is not None:
            if not isinstance(overrides, dict):
                errors.append(f"{prefix} ({name}): 'retrieval' must be a mapping")
            else:
                bad = set(overrides.keys()) - _VALID_OVERRIDE_KEYS
                if bad:
                    errors.append(
                        f"{prefix} ({name}): unknown retrieval override key(s) {sorted(bad)} "
                        f"— valid: {sorted(_VALID_OVERRIDE_KEYS)}"
                    )

    pattern = collections.get("agent_pattern")
    if pattern is not None and not isinstance(pattern, str):
        errors.append("collections.agent_pattern: must be a string template")
    if pattern is not None and isinstance(pattern, str) and "{agent}" not in pattern:
        errors.append("collections.agent_pattern: must contain '{agent}' placeholder")

    return errors


def _validate_agents(agents: Any, collections: Any) -> list[str]:
    errors: list[str] = []
    if agents is None:
        return errors  # absence is valid (no all-agents support)
    if not isinstance(agents, list):
        return ["agents: must be a list"]

    seen_names: set[str] = set()
    write_paths: list[tuple[str, str]] = []  # (agent_name, path)
    pattern = "{agent}-memory"
    if isinstance(collections, dict):
        custom = collections.get("agent_pattern")
        if isinstance(custom, str):
            pattern = custom

    for i, item in enumerate(agents):
        prefix = f"agents[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: must be a mapping")
            continue
        name = item.get("name")
        if not name:
            errors.append(f"{prefix}: missing required 'name'")
            continue
        if name in seen_names:
            errors.append(f"{prefix}: duplicate agent name {name!r}")
        seen_names.add(name)

        collection = item.get("collection") or pattern.format(agent=name)
        if not isinstance(collection, str):
            errors.append(f"{prefix} ({name}): collection must be a string")

        write_path = item.get("write_path", "")
        if write_path:
            if not isinstance(write_path, str):
                errors.append(f"{prefix} ({name}): write_path must be a string")
            else:
                # Detect overlapping write_paths — one being a prefix of another.
                for other_name, other_path in write_paths:
                    if write_path == other_path:
                        errors.append(
                            f"{prefix} ({name}): write_path {write_path!r} duplicates "
                            f"agent {other_name!r}"
                        )
                    elif other_path and (
                        write_path.startswith(other_path.rstrip("/") + "/")
                        or other_path.startswith(write_path.rstrip("/") + "/")
                    ):
                        errors.append(
                            f"{prefix} ({name}): write_path {write_path!r} overlaps with "
                            f"agent {other_name!r} write_path {other_path!r}"
                        )
                write_paths.append((str(name), write_path))

    return errors


def main(argv: list[str] | None = None) -> int:
    """CLI entry: kairix config validate [path]"""
    import argparse

    import yaml

    parser = argparse.ArgumentParser(prog="kairix config", description="Validate kairix configuration")
    sub = parser.add_subparsers(dest="subcommand")
    validate_p = sub.add_parser("validate", help="Validate kairix.config.yaml")
    validate_p.add_argument(
        "path",
        nargs="?",
        help="Path to config file (default: $KAIRIX_CONFIG_PATH or ./kairix.config.yaml)",
    )

    args = parser.parse_args(argv)
    if args.subcommand != "validate":
        parser.print_help()
        return 1

    if args.path:
        config_path = Path(args.path)
    else:
        from kairix.core.search.config_loader import _resolve_config_path

        resolved = _resolve_config_path()
        if resolved is None:
            print("No config file found. Set KAIRIX_CONFIG_PATH or place kairix.config.yaml in the cwd.")
            return 1
        config_path = resolved

    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        return 1

    try:
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        print(f"YAML parse error in {config_path}: {exc}")
        return 1

    errors = validate_config(data)
    if errors:
        print(f"Found {len(errors)} validation error(s) in {config_path}:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(f"OK: {config_path} is valid.")
    return 0
