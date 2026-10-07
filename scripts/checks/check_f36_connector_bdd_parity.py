"""F36: every connector / extractor plugin has matching BDD coverage.

The connector + ingestion architecture (see
``docs/architecture/connector-ingestion-architecture.md`` §3 + §6 + §9 -
"BDD coverage matrix") requires, for every plugin directory under
``kairix/connectors/<name>/`` and ``kairix/extractors/<name>/``, two
pieces of BDD coverage:

  1. A per-plugin feature file at
     ``tests/bdd/features/connector_<name>.feature`` (for connectors) or
     ``tests/bdd/features/extractor_<name>.feature`` (for extractors)
     covering the plugin-specific behaviour. The plugin owns this file.

  2. The plugin name appears as a Scenario Outline Examples-table row in
     ``tests/bdd/features/e2e_connector_sync.feature``. The E2E journey
     is parameterised over connectors x extractors — adding a new plugin
     means adding one Examples row, not duplicating a feature.

A plugin may explicitly opt out of the E2E journey by tagging
``e2e_connector_sync.feature`` with ``@<name>_no_sync``. The tag is the
documented escape hatch (mirrors F28's ``@<name>_no_<journey>`` shape).

Plugin discovery: every immediate subdirectory of
``kairix/connectors/`` or ``kairix/extractors/`` whose name is not
``_``-prefixed and is not in the small allow-list (``__pycache__``) is a
plugin. A bare ``.py`` file at either root (e.g. ``_base.py``,
``__init__.py``) is NOT a plugin.

The detector lists plugins, then for each plugin checks both the
per-plugin feature file existence and the Examples-row presence in
``e2e_connector_sync.feature``. Violations are reported as the synthetic
path ``kairix/connectors/<name>`` or ``kairix/extractors/<name>``.
There is no grandfathering: every violation fails the gate.

If neither ``kairix/connectors/`` nor ``kairix/extractors/`` exists (and
``e2e_connector_sync.feature`` is also absent — Wave 0 state), the
check passes trivially.

Note on Examples-row matching: a row matches when, after splitting the
table line on ``|`` and stripping whitespace, ANY non-empty cell equals
the plugin name exactly. This is more permissive than F28 (first-cell
only) because ``e2e_connector_sync.feature`` is parameterised over both
connector AND extractor — the plugin identifier may sit in column 1
(connector) or column 2 (extractor). The detector tolerates surrounding
whitespace and ignores the header row.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

_CONNECTORS_DIR_REL = Path("kairix") / "connectors"
_EXTRACTORS_DIR_REL = Path("kairix") / "extractors"
_FEATURES_DIR_REL = Path("tests") / "bdd" / "features"
_E2E_FEATURE_NAME = "e2e_connector_sync.feature"

# Names under kairix/connectors/ or kairix/extractors/ that are NOT
# plugins (shared scaffolding / cache directories).
_NON_PLUGIN_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
    }
)

REMEDIATION = """Refactor to add the missing BDD coverage for the listed
connector or extractor plugin — every plugin needs a per-plugin feature
file AND a row in the e2e_connector_sync.feature Examples table.

fix: create tests/bdd/features/connector_<name>.feature (or
extractor_<name>.feature) with at least one happy-path Scenario covering
the plugin's authentication shape, list-changes / fetch shape (for
connectors) or MIME handling + page-citation (for extractors). Then add
a row for <name> to the Examples table of
tests/bdd/features/e2e_connector_sync.feature so the parameterised
sync journey runs against the new plugin. To opt the plugin out of the
end-to-end sync journey (e.g. an extractor used only via direct upload
that never participates in connector sync), tag
e2e_connector_sync.feature with ``@<name>_no_sync``.
next: re-run python3 scripts/checks/check_f36_connector_bdd_parity.py
to confirm the gate goes green.
run: bash scripts/safe-commit.sh "test(bdd): connector_<name> + e2e Examples row"

Pass example: (tests/bdd/features/connector_obsidian.feature)
  Feature: obsidian connector
    Scenario: list_changes since cursor returns modified notes
      Given an obsidian vault with a .md note modified after the cursor
      When the connector lists changes since the cursor
      Then the modified note path is returned

Pass example: (tests/bdd/features/e2e_connector_sync.feature)
  Scenario Outline: sync with connector <connector> + extractor <extractor>
    Given the kairix process is configured with connector <connector>
    And the extractor <extractor> is registered
    When the operator runs the connector sync
    Then docs land in the index with source_uri populated

    Examples:
      | connector | extractor    |
      | obsidian  | markitdown   |
      | gdrive    | passthrough  |

Forbidden example:
  kairix/connectors/obsidian/  exists, but no
  tests/bdd/features/connector_obsidian.feature, AND
  tests/bdd/features/e2e_connector_sync.feature has no obsidian row.

Why: see docs/architecture/connector-ingestion-architecture.md §9 ("BDD
coverage matrix"). The E2E feature is a Scenario Outline (one feature,
N rows) so adding a plugin is one fixture + one row, not a copy-pasted
feature. F36 is the mechanical guard that keeps that property — a
plugin without coverage shouldn't ship."""


_TAG_LINE_RE = re.compile(r"^\s*@")
_EXAMPLES_RE = re.compile(r"^\s*Examples\b:", re.IGNORECASE)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _discover_plugins(root: Path) -> list[str]:
    """List plugin directory names under ``root``.

    Skips ``_``-prefixed names (shared scaffolding) and the cache
    allow-list. Files at the root are never plugins. Returns sorted
    plugin names; empty list if the root doesn't exist.
    """
    if not root.exists():
        return []
    out: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("_"):
            continue
        if name in _NON_PLUGIN_NAMES:
            continue
        out.append(name)
    return out


def _examples_rows(text: str) -> list[list[str]]:
    """Extract every table row from every ``Examples:`` block in the
    given Gherkin feature text.

    Returns a list of rows; each row is a list of cell strings
    (stripped). The header row is excluded. Multiple Examples tables in
    one feature are concatenated.
    """
    rows: list[list[str]] = []
    lines = text.splitlines()
    in_examples = False
    rows_seen_in_block = 0
    for line in lines:
        if _EXAMPLES_RE.match(line):
            in_examples = True
            rows_seen_in_block = 0
            continue
        if in_examples:
            if _TABLE_ROW_RE.match(line):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if rows_seen_in_block == 0:
                    # Header row — skip.
                    rows_seen_in_block += 1
                    continue
                rows.append(cells)
                rows_seen_in_block += 1
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Any other content ends the Examples block.
            in_examples = False
    return rows


def _feature_tags(text: str) -> set[str]:
    """All ``@tag`` tokens that appear anywhere in the feature file.

    Used to honour the ``@<plugin>_no_sync`` opt-out convention.
    """
    out: set[str] = set()
    for line in text.splitlines():
        if not _TAG_LINE_RE.match(line):
            continue
        for token in line.strip().split():
            if token.startswith("@"):
                out.add(token.lower())
    return out


def _plugin_appears_in_e2e(plugin: str, e2e_path: Path) -> bool:
    """True if the plugin name appears as any non-empty cell of any
    Examples-table row in ``e2e_path``, OR the file is tagged with the
    ``@<plugin>_no_sync`` opt-out. If the E2E file does not exist, the
    requirement is vacuously satisfied (Wave 0 scaffold).
    """
    if not e2e_path.is_file():
        return True
    try:
        text = e2e_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    opt_out = f"@{plugin}_no_sync".lower()
    if opt_out in _feature_tags(text):
        return True
    for cells in _examples_rows(text):
        for cell in cells:
            if cell == plugin:
                return True
    return False


def _collect_for_kind(repo_root: Path, kind_dir_rel: Path, feature_prefix: str) -> set[Path]:
    """Collect violations for a single plugin kind (``connector`` or
    ``extractor``). ``feature_prefix`` is ``"connector"`` or
    ``"extractor"``.
    """
    root = repo_root / kind_dir_rel
    features_dir = repo_root / _FEATURES_DIR_REL
    e2e_path = features_dir / _E2E_FEATURE_NAME
    plugins = _discover_plugins(root)
    if not plugins:
        return set()

    violations: set[Path] = set()
    for plugin in plugins:
        per_plugin = features_dir / f"{feature_prefix}_{plugin}.feature"
        if not per_plugin.is_file():
            violations.add(kind_dir_rel / plugin)
            continue
        if not _plugin_appears_in_e2e(plugin, e2e_path):
            violations.add(kind_dir_rel / plugin)
    return violations


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """For every plugin under ``<repo_root>/kairix/connectors/`` and
    ``<repo_root>/kairix/extractors/``, return a synthetic violation
    path of the form ``kairix/connectors/<name>`` or
    ``kairix/extractors/<name>`` when EITHER:

      * the per-plugin feature file does not exist
        (``tests/bdd/features/connector_<name>.feature`` /
        ``extractor_<name>.feature``), OR
      * ``tests/bdd/features/e2e_connector_sync.feature`` exists but
        lacks an Examples-table cell equal to ``<name>`` AND lacks the
        ``@<name>_no_sync`` opt-out tag.

    The synthetic path is what the gate reports — one entry per
    plugin missing coverage. Empty set if there are no plugins.
    """
    violations: set[Path] = set()
    violations |= _collect_for_kind(repo_root, _CONNECTORS_DIR_REL, "connector")
    violations |= _collect_for_kind(repo_root, _EXTRACTORS_DIR_REL, "extractor")
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f36", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
