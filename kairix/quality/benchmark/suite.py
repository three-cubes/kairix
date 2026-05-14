"""
Benchmark suite loader and validator.

Loads YAML suite files and validates them against the kairix index.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from kairix.quality.eval.constants import CATEGORY_ALIASES, CATEGORY_WEIGHTS

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

VALID_CATEGORIES = frozenset(CATEGORY_WEIGHTS.keys()) | frozenset(CATEGORY_ALIASES.keys())
VALID_SCORE_METHODS = frozenset({"exact", "fuzzy", "llm", "classification", "ndcg"})


@dataclass
class BenchmarkCase:
    id: str
    category: str  # recall|temporal|entity|conceptual|multi_hop|procedural|classification
    query: str
    gold_path: str | None
    score_method: str  # exact|fuzzy|llm|classification|ndcg
    notes: str | None = None
    expected_type: str | None = None  # for classification score_method
    gold_paths: list[dict] | None = None  # for ndcg: [{path, relevance}] graded relevance 0-2 (path-based)
    gold_title: str | None = None  # stable note title for exact/fuzzy cases (path-agnostic)
    gold_titles: list[dict] | None = None  # for ndcg: [{title, relevance}] graded relevance 0-2 (title-based)
    agent: str | None = None  # per-case agent override (builder|shape|consultant|…)


@dataclass
class BenchmarkSuite:
    meta: dict
    cases: list[BenchmarkCase] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------


def resolve_suite_path(suite_arg: str, root: Path | None = None) -> Path:
    """Resolve a `--suite` argument to a concrete YAML path.

    ``suite_arg`` may be:
      - An explicit filesystem path (used directly when it exists).
      - A bundle name (e.g. ``reflib``) — searches ``root`` for the
        highest gold version matching ``<name>-gold-v*.yaml``, then
        falls back to ``<name>.yaml``.

    ``root`` defaults to ``kairix.paths.bundled_suites_root()`` for
    production callers; tests pass an explicit ``tmp_path`` to avoid
    env-var monkeypatching (F2).

    The bundle-name shortcut is the user-facing UX from #222 — running
    ``kairix benchmark run reflib`` no longer requires hunting for the
    in-image suite path.
    """
    p = Path(suite_arg)
    if p.exists():
        return p

    if root is None:
        from kairix.paths import bundled_suites_root

        root = bundled_suites_root()

    if root.is_dir():
        gold = sorted(
            root.glob(f"{suite_arg}-gold-v*.yaml"),
            key=lambda x: x.name,
            reverse=True,
        )
        if gold:
            return gold[0]

        fallback = root / f"{suite_arg}.yaml"
        if fallback.exists():
            return fallback

    raise FileNotFoundError(
        f"Suite '{suite_arg}' not found. Tried path lookup and bundled "
        f"resolution in {root}. Run 'kairix benchmark list' to see available bundled suites."
    )


def list_bundled_suites(root: Path | None = None) -> list[dict]:
    """Return metadata for each bundled suite for the ``list`` subcommand.

    Returns: list of dicts with keys ``name``, ``path``, ``default_collection``,
    ``n_cases``, ``description``. Missing fields are ``None``.

    ``root`` defaults to ``kairix.paths.bundled_suites_root()``; tests
    pass an explicit path for hermetic resolution.
    """
    if root is None:
        from kairix.paths import bundled_suites_root

        root = bundled_suites_root()

    if not root.is_dir():
        return []

    out: list[dict] = []
    for yaml_path in sorted(root.glob("*.yaml")):
        try:
            raw = load_yaml_file(yaml_path)
        except (FileNotFoundError, ValueError):
            continue
        meta = raw.get("meta", {}) if isinstance(raw, dict) else {}
        out.append(
            {
                "name": meta.get("name") or yaml_path.stem,
                "path": str(yaml_path),
                "default_collection": meta.get("default_collection"),
                "n_cases": len(raw.get("cases", [])) if isinstance(raw, dict) else 0,
                "description": meta.get("description"),
            }
        )
    return out


def load_yaml_file(path: Path) -> dict:
    """Read a YAML file, raise on parse error or unexpected type."""
    if not path.exists():
        raise FileNotFoundError(f"Suite file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ValueError(f"Suite file must be a YAML mapping, got {type(raw).__name__}")

    return raw


def validate_meta_and_cases_structure(raw: dict, path: str) -> tuple[dict, list[dict], list[str]]:
    """Validate root dict has valid meta and cases. Returns (meta, raw_cases, errors)."""
    meta = raw.get("meta", {})
    if not isinstance(meta, dict):
        raise ValueError("'meta' must be a mapping")

    raw_cases = raw.get("cases", [])
    if not isinstance(raw_cases, list):
        raise ValueError("'cases' must be a list")

    return meta, raw_cases, []


def validate_required_fields(case_id: str | None, case: dict, i: int, errors: list[str]) -> None:
    """Check id, category, query, score_method are present and valid."""
    if not case_id:
        errors.append(f"Case [{i}]: missing required field 'id'")

    category = case.get("category")
    if not category:
        errors.append(f"Case [{i}] ({case_id}): missing required field 'category'")
    elif category not in VALID_CATEGORIES:
        errors.append(
            f"Case [{i}] ({case_id}): invalid category {category!r}; must be one of {sorted(VALID_CATEGORIES)}"
        )

    if not case.get("query"):
        errors.append(f"Case [{i}] ({case_id}): missing required field 'query'")

    score_method = case.get("score_method")
    if not score_method:
        errors.append(f"Case [{i}] ({case_id}): missing required field 'score_method'")
    elif score_method not in VALID_SCORE_METHODS:
        errors.append(
            f"Case [{i}] ({case_id}): invalid score_method {score_method!r}; "
            f"must be one of {sorted(VALID_SCORE_METHODS)}"
        )


def validate_gold_titles_structure(
    gold_titles: list[dict] | None, case_id: str | None, i: int, errors: list[str]
) -> None:
    """Validate each gold_titles entry has title (str) and relevance (int 0-2)."""
    if not gold_titles or not isinstance(gold_titles, list):
        return

    for j, gt in enumerate(gold_titles):
        if not isinstance(gt, dict):
            errors.append(f"Case [{i}] ({case_id}): gold_titles[{j}] must be a mapping")
        elif "title" not in gt:
            errors.append(f"Case [{i}] ({case_id}): gold_titles[{j}] missing required field 'title'")
        elif "relevance" not in gt:
            errors.append(f"Case [{i}] ({case_id}): gold_titles[{j}] missing required field 'relevance'")
        elif gt["relevance"] not in (0, 1, 2):
            errors.append(f"Case [{i}] ({case_id}): gold_titles[{j}] relevance must be 0, 1, or 2")


def validate_recall_gold_requirement(
    category: str | None,
    gold_path: str | None,
    gold_paths: list[dict] | None,
    gold_title: str | None,
    gold_titles: list[dict] | None,
    case_id: str | None,
    i: int,
    errors: list[str],
) -> None:
    """Check recall cases have at least one gold reference."""
    if category == "recall" and not gold_path and not gold_paths and not gold_title and not gold_titles:
        if not errors:
            errors.append(f"Case [{i}] ({case_id}): recall cases must have gold_path, gold_title, or a gold list")


def derive_gold_path_from_gold_lists(
    gold_path: str | None,
    gold_paths: list[dict] | None,
    gold_title: str | None,
    gold_titles: list[dict] | None,
) -> str | None:
    """Derive best gold_path from gold lists for backwards compat.

    Priority: explicit gold_path > highest-relevance gold_paths entry
    > highest-relevance gold_titles entry > gold_title.
    """
    if gold_path:
        return gold_path

    if gold_paths and isinstance(gold_paths, list):
        # _coerce_gold_list allows non-dict scalars through; filter to dicts here
        # so the relevance-based max doesn't AttributeError on a string item.
        dict_paths = [g for g in gold_paths if isinstance(g, dict)]
        best = max(dict_paths, key=lambda g: g.get("relevance", 0), default=None)
        if best:
            return best.get("path")
    elif gold_titles and isinstance(gold_titles, list):
        dict_titles = [g for g in gold_titles if isinstance(g, dict)]
        best_t = max(dict_titles, key=lambda g: g.get("relevance", 0), default=None)
        if best_t:
            return best_t.get("title")  # title as path-equivalent for display
    elif gold_title:
        return gold_title

    return None


def _coerce_gold_list(items: list[dict] | None, ref_field: str) -> list[dict] | None:
    """Coerce the ref-field of each item in a gold list to ``str``.

    PyYAML parses unquoted ISO-shaped values like ``2026-04-07`` as
    ``datetime.date`` objects. Downstream scoring calls ``.endswith(".md")``
    on these refs, which raises AttributeError on date objects. Coerce here
    at the suite-load boundary so scoring sees only strings.
    """
    if not isinstance(items, list):
        return None
    coerced: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            coerced.append(item)
            continue
        new_item = dict(item)
        if ref_field in new_item and new_item[ref_field] is not None:
            new_item[ref_field] = str(new_item[ref_field])
        coerced.append(new_item)
    return coerced


def build_benchmark_case(
    i: int,
    case_id: str | None,
    category: str | None,
    query: str | None,
    gold_path: str | None,
    score_method: str | None,
    notes: str | None,
    expected_type: str | None,
    gold_paths: list[dict] | None,
    gold_title: str | None,
    gold_titles: list[dict] | None,
    case_agent: str | None,
) -> BenchmarkCase:
    """Construct a BenchmarkCase from validated fields."""
    return BenchmarkCase(
        id=str(case_id) if case_id else f"case_{i}",
        category=str(category) if category else "",
        query=str(query) if query else "",
        gold_path=str(gold_path) if gold_path else None,
        score_method=str(score_method) if score_method else "",
        notes=str(notes) if notes else None,
        expected_type=str(expected_type) if expected_type else None,
        gold_paths=_coerce_gold_list(gold_paths, "path"),
        gold_title=str(gold_title) if gold_title else None,
        gold_titles=_coerce_gold_list(gold_titles, "title"),
        agent=str(case_agent) if case_agent else None,
    )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_suite(path: str) -> BenchmarkSuite:
    """
    Load a benchmark suite from a YAML file.

    Args:
        path: Path to the suite YAML file.

    Returns:
        BenchmarkSuite parsed from the file.

    Raises:
        ValueError: If the file cannot be parsed or the schema is invalid.
        FileNotFoundError: If the file does not exist.
    """
    raw = load_yaml_file(Path(path))
    meta, raw_cases, errors = validate_meta_and_cases_structure(raw, path)

    cases: list[BenchmarkCase] = []

    for i, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            errors.append(f"Case [{i}]: must be a mapping")
            continue

        case_id = raw_case.get("id")
        category = raw_case.get("category")
        query = raw_case.get("query")
        gold_path = raw_case.get("gold_path")
        score_method = raw_case.get("score_method")
        notes = raw_case.get("notes")
        expected_type = raw_case.get("expected_type")
        gold_paths = raw_case.get("gold_paths")
        gold_title = raw_case.get("gold_title")
        gold_titles = raw_case.get("gold_titles")
        case_agent = raw_case.get("agent")

        validate_required_fields(case_id, raw_case, i, errors)
        validate_gold_titles_structure(gold_titles, case_id, i, errors)
        validate_recall_gold_requirement(category, gold_path, gold_paths, gold_title, gold_titles, case_id, i, errors)

        gold_path = derive_gold_path_from_gold_lists(gold_path, gold_paths, gold_title, gold_titles)

        if not errors or (case_id and category and query and score_method):
            cases.append(
                build_benchmark_case(
                    i,
                    case_id,
                    category,
                    query,
                    gold_path,
                    score_method,
                    notes,
                    expected_type,
                    gold_paths,
                    gold_title,
                    gold_titles,
                    case_agent,
                )
            )

    if errors:
        raise ValueError(f"Suite schema errors in {path}:\n" + "\n".join(f"  - {e}" for e in errors))

    return BenchmarkSuite(meta=meta, cases=cases)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def validate_suite(
    suite: BenchmarkSuite,
    db: sqlite3.Connection,
    strict: bool = True,
) -> list[str]:
    """
    Validate a benchmark suite against the kairix index.

    Checks:
    - Gold paths for recall cases exist in the index (case-insensitive)
    - No duplicate gold paths across the suite

    Args:
        suite:  The BenchmarkSuite to validate.
        db:     Open sqlite3.Connection to the kairix index.
        strict: If True, missing gold paths are errors. If False, they are warnings.

    Returns:
        List of error strings. Empty list means all checks passed.
    """
    errors: list[str] = []

    # Collect gold paths from recall cases that use path-based identity
    path_based_recall: list[tuple[str, str]] = [
        (case.id, case.gold_path)
        for case in suite.cases
        if case.gold_path and case.category == "recall" and not case.gold_title and not case.gold_titles
    ]

    # Check for duplicate gold paths (path-based recall only)
    seen_gold: dict[str, str] = {}
    for case_id, gp in path_based_recall:
        gp_lower = gp.lower()
        if gp_lower in seen_gold:
            errors.append(f"Duplicate gold_path: {gp!r} used by both {seen_gold[gp_lower]!r} and {case_id!r}")
        else:
            seen_gold[gp_lower] = case_id

    # Check gold paths exist in the kairix index (path-based only; title-based is path-agnostic)
    for case_id, gp in path_based_recall:
        if not _gold_path_in_index(db, gp):
            msg = f"Case {case_id!r}: gold_path {gp!r} not found in kairix index"
            errors.append(msg)

    # Check for duplicate gold_title values across recall cases
    seen_titles: dict[str, str] = {}
    for case in suite.cases:
        if case.gold_title and case.category == "recall":
            title_lower = case.gold_title.lower()
            if title_lower in seen_titles:
                errors.append(
                    f"Duplicate gold_title: {case.gold_title!r} used by both "
                    f"{seen_titles[title_lower]!r} and {case.id!r}"
                )
            else:
                seen_titles[title_lower] = case.id

    return errors


def _gold_path_in_index(db: sqlite3.Connection, gold_path: str) -> bool:
    """
    Check whether a gold path exists in the kairix index (case-insensitive substring match).

    kairix stores paths as document-root-relative paths like:
      01-projects/...

    We match using a LIKE query on the path column.
    """
    # Normalise the gold path for comparison
    # Strip leading path components and match as suffix
    norm = gold_path.lower().replace("\\", "/")

    # Try exact suffix match first
    cursor = db.execute(
        "SELECT 1 FROM documents WHERE lower(path) LIKE ? LIMIT 1",
        (f"%{norm}",),
    )
    if cursor.fetchone():
        return True

    # Try without any leading prefix (just filename portion)
    parts = norm.split("/")
    if len(parts) > 1:
        # Try progressively shorter suffixes
        for n in range(len(parts) - 1, 0, -1):
            suffix = "/".join(parts[n:])
            cursor = db.execute(
                "SELECT 1 FROM documents WHERE lower(path) LIKE ? LIMIT 1",
                (f"%{suffix}",),
            )
            if cursor.fetchone():
                return True

    return False
