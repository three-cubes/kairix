"""F28: every provider plugin has matching BDD coverage.

The three-layer provider-plugin split (see
``docs/architecture/provider-plugin-architecture.md`` - "BDD coverage
matrix") requires, for every plugin directory under
``kairix/providers/<name>/``, two pieces of BDD coverage:

  1. A per-plugin feature file at
     ``tests/bdd/features/provider_<name>.feature`` covering auth shape,
     URL shape, error mapping, model-id semantics. The plugin owns this
     file.

  2. The plugin name appears as a Scenario Outline Examples-table row
     in every ``tests/bdd/features/e2e_provider_*.feature`` file. The
     E2E journeys are parameterised — adding a new provider means
     adding one Examples row, not duplicating a feature.

A plugin may explicitly opt out of a single E2E journey by tagging the
journey with ``@<name>_no_embed`` (or ``@<name>_no_chat``, etc.). The
tag is the documented escape hatch — for example, an embed-only
provider that legitimately has no chat capability.

Plugin discovery: every immediate subdirectory of
``kairix/providers/`` whose name is not ``_``-prefixed and is not in
the small allow-list (``__pycache__``) is a plugin. A bare ``.py``
file at the providers root (e.g. ``_base.py``, ``__init__.py``) is
NOT a plugin.

The detector lists plugins, then for each plugin checks both the
per-plugin feature file existence and Examples-row presence across
every ``e2e_provider_*.feature``. Violations are reported as the
plugin name (one entry per missing-coverage plugin), grandfathered
through ``.architecture/baseline/f28-files.txt``.

If ``kairix/providers/`` does not exist or has no plugin
subdirectories, the check passes trivially.

Note on Examples-row matching: a row matches when, after splitting the
table line on ``|`` and stripping whitespace, the first non-empty cell
equals the plugin name exactly. This is the convention the ADR
documents — the first column of the Examples table is the provider
identifier. The detector tolerates surrounding whitespace and ignores
the header row (``| provider | ... |``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import REPO_ROOT, gate

_PROVIDERS_DIR_REL = Path("kairix") / "providers"
_FEATURES_DIR_REL = Path("tests") / "bdd" / "features"

# Names under kairix/providers/ that are NOT plugins (shared
# scaffolding / cache directories).
_NON_PLUGIN_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
    }
)

REMEDIATION = """Refactor to add the missing BDD coverage for the
listed provider — every plugin needs a per-plugin feature file AND a
row in each e2e_provider_*.feature Examples table.

fix: create tests/bdd/features/provider_<name>.feature with at least
one happy-path Scenario covering the plugin's auth shape, URL shape,
error mapping, and model-id semantics (the FakeHttpClient fixture
stubs the wire). Then add ``| <name> | <model> | ... |`` rows to the
Examples table of every tests/bdd/features/e2e_provider_*.feature so
the parameterised journey runs against the new plugin. To opt a
single plugin out of a specific E2E journey (e.g. an embed-only
plugin with no chat), tag that journey with ``@<name>_no_embed``.
next: re-run python3 scripts/checks/check_provider_bdd_completeness.py
to confirm the gate goes green.
run: bash scripts/safe-commit.sh "test(bdd): provider_<name> + e2e Examples row"

Pass example (tests/bdd/features/provider_openai.feature):
  Feature: openai provider
    Scenario: embed_batch reaches the configured base_url
      Given an openai plugin configured with base_url=https://api.openai.com
      When the caller invokes embed_batch with two texts
      Then the recorded request URL is https://api.openai.com/v1/embeddings

Pass example (tests/bdd/features/e2e_provider_embed.feature):
  Scenario Outline: embed with provider <provider>
    Given the kairix process is configured with provider <provider>
    ...
    Examples:
      | provider      | model              |
      | openai        | text-embedding-3   |
      | azure_foundry | text-embedding-ada |
      | bedrock       | titan-embed-v1     |

Forbidden example:
  kairix/providers/bedrock/  exists, but no
  tests/bdd/features/provider_bedrock.feature, AND
  tests/bdd/features/e2e_provider_embed.feature has no bedrock row.

Why: see docs/architecture/provider-plugin-architecture.md - "BDD
coverage matrix". The E2E features are Scenario Outlines (one feature,
N rows) so adding a provider is one fixture + one row, not a
copy-pasted feature. F28 is the mechanical guard that keeps that
property — a plugin without coverage shouldn't ship."""


_TAG_LINE_RE = re.compile(r"^\s*@")
_EXAMPLES_RE = re.compile(r"^\s*Examples\b:", re.IGNORECASE)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SCENARIO_RE = re.compile(r"^\s*(Scenario|Scenario Outline):", re.IGNORECASE)


def _discover_plugins(providers_root: Path) -> list[str]:
    """List plugin directory names under ``providers_root``.

    Skips ``_``-prefixed names (shared scaffolding) and the cache
    allow-list. Files at the providers root are never plugins.
    Returns sorted plugin names; empty list if the root doesn't exist.
    """
    if not providers_root.exists():
        return []
    out: list[str] = []
    for child in sorted(providers_root.iterdir()):
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
    (stripped). The header row is included — callers must drop it if
    they only want data rows. Multiple Examples tables in one feature
    are concatenated.
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

    Used to honour the ``@<plugin>_no_<journey>`` opt-out convention.
    """
    out: set[str] = set()
    for line in text.splitlines():
        if not _TAG_LINE_RE.match(line):
            continue
        for token in line.strip().split():
            if token.startswith("@"):
                out.add(token.lower())
    return out


def _e2e_files(features_dir: Path) -> list[Path]:
    """Sorted list of ``e2e_provider_*.feature`` files under
    ``features_dir``. Empty list if the directory does not exist or
    holds no E2E provider features (Wave 1 scaffold may not have any
    yet — in that case, F28 only enforces the per-plugin feature
    requirement, not the Examples-row requirement).
    """
    if not features_dir.exists():
        return []
    return sorted(features_dir.glob("e2e_provider_*.feature"))


def _journey_key(e2e_path: Path) -> str:
    """Map ``e2e_provider_embed.feature`` -> ``embed``; used to derive
    the opt-out tag ``@<plugin>_no_embed``.
    """
    stem = e2e_path.stem  # e.g. "e2e_provider_embed"
    return stem.removeprefix("e2e_provider_") or stem


def _plugin_has_examples_row(plugin: str, e2e_path: Path) -> bool:
    """True if the plugin name appears as the first non-empty cell of
    any Examples-table row in ``e2e_path``, OR the file is tagged with
    the ``@<plugin>_no_<journey>`` opt-out.
    """
    try:
        text = e2e_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    journey = _journey_key(e2e_path)
    opt_out = f"@{plugin}_no_{journey}".lower()
    if opt_out in _feature_tags(text):
        return True
    for cells in _examples_rows(text):
        # First non-empty cell is the provider identifier by ADR
        # convention.
        for cell in cells:
            if not cell:
                continue
            return_match = cell == plugin
            if return_match:
                return True
            break
    return False


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """For every plugin under ``<repo_root>/kairix/providers/``, return
    a synthetic violation path of the form
    ``kairix/providers/<name>`` when EITHER:

      * ``tests/bdd/features/provider_<name>.feature`` does not exist,
        OR
      * any ``tests/bdd/features/e2e_provider_*.feature`` lacks an
        Examples-table row for ``<name>`` AND lacks the
        ``@<name>_no_<journey>`` opt-out tag.

    The synthetic path is what the baseline tracks — one entry per
    plugin missing coverage. Empty set if there are no plugins.
    """
    providers_root = repo_root / _PROVIDERS_DIR_REL
    features_dir = repo_root / _FEATURES_DIR_REL
    plugins = _discover_plugins(providers_root)
    if not plugins:
        return set()

    violations: set[Path] = set()
    e2es = _e2e_files(features_dir)

    for plugin in plugins:
        per_plugin = features_dir / f"provider_{plugin}.feature"
        if not per_plugin.is_file():
            violations.add(_PROVIDERS_DIR_REL / plugin)
            continue
        # Per-plugin file present; check every E2E journey accepts the
        # plugin. If there are no E2E features yet (Wave 1 scaffold),
        # the requirement is automatically satisfied.
        if not all(_plugin_has_examples_row(plugin, e2e) for e2e in e2es):
            violations.add(_PROVIDERS_DIR_REL / plugin)

    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f28", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
