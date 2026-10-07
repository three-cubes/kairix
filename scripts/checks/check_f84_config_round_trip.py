"""F84: every production config-write site has a composed write→read round-trip test.

Motivation (EPIC #499 Phase 1; the #492 overlay split-brain class)
------------------------------------------------------------------
The setup wizard wrote ``topology_v2`` through the overlay writer
(``write_config_updates``) while the worker read its config through a
different, non-overlay resolver — the flagship feature was silently
inert in Docker with every suite green (#492, H1). A composed test that
writes through the production writer and reads the value back through
the canonical layered reader (``kairix/config_layers.py::
load_merged_mapping`` and its re-export ``config_loader.load_config``)
would have failed immediately. F84 makes that round-trip structural for
every site that writes ``kairix.config.yaml``-shaped data.

What F84 harvests (a "config-write site")
-----------------------------------------
A function ``def`` in ``kairix/**`` is a config-write site when EITHER:

  (a) it is public (no leading underscore) and its name matches the
      config-writer naming convention — a write verb (``write`` /
      ``update`` / ``save`` / ``persist``) compounded with ``config``
      (``write_config_updates``, ``update_config_file``,
      ``write_config_yaml``, a future ``save_operator_config``); OR
  (b) regardless of visibility, its name matches that convention AND
      its body contains a stream-form ``yaml.dump`` / ``yaml.safe_dump``
      call (two or more positional args — dumping TO a file object, not
      rendering a string).

Private delegation shims (``_default_write_config`` — lazy-import DI
defaults with no dump of their own) are deliberately NOT harvested:
house style drives private helpers through their public callers, and
their round-trip behaviour IS the callee's.

The coverage convention (document of record)
--------------------------------------------
A write site ``W`` is round-trip covered when ANY of:

  1. **Naming convention** — some test module under ``tests/``
     references ``W`` by name (import, call, or attribute) AND
     references at least one canonical-reader name:
     ``load_merged_mapping``, ``load_config``,
     ``load_top_level_config``, or ``feature_flag_config_overlay``
     (the last two are the thin ``kairix/paths.py`` wrappers that the
     #492 tests pin to flow through ``load_merged_mapping``).
  2. **Registry tag** — some test module carries the line marker
     ``# F84-round-trip: W`` — the reviewed declaration for tests that
     drive ``W`` through a composed surface (CLI subprocess, web
     wizard) where the writer's name never appears in the module.
  3. **Delegation propagation** — a covered site's body calls ``W``
     (transitively): ``update_config_file`` is covered by name, so its
     callee ``write_config_yaml`` is covered too — one round-trip test
     proves the whole delegation chain.
  4. **Site exemption** — the ``def`` line (or the line directly above
     it) carries ``# F84-allowed: <why>`` — for writer-named functions
     that do NOT write operator config (e.g. a public
     ``save_probe_config`` emitting eval-suite YAML).

Pass example: ``tests/integration/test_wizard_config_overlay_split_brain.py``
(the H1 fix) — it imports ``write_config_updates`` /
``update_config_file`` AND ``load_merged_mapping`` /
``load_top_level_config``, writes through the production writers, and
asserts the written values come back through the layered reader in both
overlay-ON and single-file modes. That one module covers all three
current sites (``write_config_yaml`` via delegation from
``update_config_file``).

Intentionally NOT caught (precision over recall — a detector agents
distrust is worse than no detector):

  * A config write inside a function WITHOUT the writer naming
    convention (``def _flush(...)`` doing ``yaml.dump(cfg, fh)``) —
    name-blind dump harvesting would flag every eval-suite/report YAML
    emitter in ``kairix/quality/**``. The naming convention is the
    contract; reviewers hold the line on writer names.
  * Arbitrary CALLERS of the writer family (a service method calling
    ``write_config_updates``) — the writer function itself carries the
    round-trip contract; per-caller coverage is data-flow analysis,
    not a conservative detector.
  * Whether the test module's writer reference and reader reference
    occur in the SAME test function — module-level granularity only.
    A module that writes in one test and reads in another satisfies
    the convention; review catches insincere pairings.
  * Non-YAML config writes (``json.dump``, raw ``write_text``) hidden
    in non-writer-named functions — same rationale as the first bullet.
  * Two sites sharing one function name in different files are covered
    together — names, not qualified paths, key the convention.

There is no grandfathering: every uncovered config writer blocks at
pre-commit / safe-commit / CI Stage 0.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

# Config-writer naming convention: a write verb compounded with "config".
WRITER_NAME_RE = re.compile(
    r"(?:^|_)(?:write|update|save|persist)_\w*config|config\w*_(?:write|update|save|persist)(?:_|$)"
)

# The canonical reader family (#492): config_layers.load_merged_mapping,
# its retrieval re-export config_loader.load_config, and the two thin
# paths.py wrappers the split-brain tests pin to the layered resolver.
READER_NAMES = frozenset({"load_merged_mapping", "load_config", "load_top_level_config", "feature_flag_config_overlay"})

# Cheap raw-text pre-filter so only candidate files pay the AST parse.
PREFILTER_RE = re.compile(
    r"yaml\.(?:safe_)?dump|(?:write|update|save|persist)_\w*config|config\w*_(?:write|update|save|persist)"
)

RATIONALE_TAG = "# F84-allowed:"
ROUND_TRIP_TAG_RE = re.compile(r"#\s*F84-round-trip:\s*(\w+)")

REMEDIATION = """F84: production config-write site without a composed write→read
round-trip test — the #492 class where the wizard wrote topology_v2 to
the overlay while the worker read a different resolver, leaving the
feature silently inert behind a green suite.

fix: add (or extend) a test module under tests/ that calls the writer
and reads the written value back through the canonical layered reader —
reference BOTH the writer name and one of load_merged_mapping /
load_config / load_top_level_config / feature_flag_config_overlay in
the same module. If the test drives the writer through a composed
surface (CLI subprocess, web wizard) where the writer's name cannot
appear, declare it with a `# F84-round-trip: <writer_name>` line in the
test module. If the function matches the writer naming convention but
does NOT write operator config, put `# F84-allowed: <why>` on its def
line.
next: re-run python3 scripts/checks/check_f84_config_round_trip.py to
confirm the gate goes green. See #492 for the overlay split-brain
post-mortem this rule mechanises, and
tests/integration/test_wizard_config_overlay_split_brain.py for the
canonical round-trip shape.
run: bash scripts/safe-commit.sh "test(config): round-trip <writer> through the layered reader (#492 class)"

Pass example: tests/integration/test_wizard_config_overlay_split_brain.py
  from kairix.config_layers import load_merged_mapping
  from kairix.platform.setup.backends import write_config_updates

  def test_wizard_save_is_seen_by_the_layered_reader(tmp_path):
      overlay = tmp_path / "kairix.config.local.yaml"
      write_config_updates({"topology_v2": {...}}, overlay_path=str(overlay), config_path=None)
      merged = load_merged_mapping(env={"KAIRIX_CONFIG_OVERLAY_PATH": str(overlay)})
      assert merged["topology_v2"]["connectors"]  # the write is OBSERVED

Forbidden example:
  # kairix/platform/setup/exporter.py — a new writer, tested write-only:
  def save_runtime_config(target, cfg):
      with open(target, "w") as fh:
          yaml.dump(cfg, fh)
  # tests assert target.exists() / parse the file directly — nothing
  # proves a production READER resolves this file, which is exactly how
  # #492 shipped: green write-side tests, silently inert feature."""


def _python_files(root: Path) -> list[Path]:
    """All ``.py`` files under ``root``, skipping ``__pycache__``."""
    if not root.exists():
        return []
    return [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts]


def _parse(path: Path) -> ast.Module | None:
    """AST-parse ``path``; ``None`` on unreadable/unparseable files."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def _is_stream_yaml_dump(node: ast.AST) -> bool:
    """True iff ``node`` is ``yaml.dump(data, stream, ...)`` /
    ``yaml.safe_dump(data, stream, ...)`` — two or more positional args
    means dumping TO a stream (a file write), not rendering a string.
    Bare ``dump(data, stream)`` (the ``from yaml import dump`` form)
    also counts.
    """
    if not isinstance(node, ast.Call) or len(node.args) < 2:
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in ("dump", "safe_dump") and isinstance(func.value, ast.Name) and func.value.id == "yaml"
    return isinstance(func, ast.Name) and func.id in ("dump", "safe_dump")


def _called_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Terminal names of every call in ``func``'s body (``f()`` → ``f``,
    ``mod.f()`` → ``f``) — feeds delegation propagation.
    """
    out: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                out.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                out.add(node.func.attr)
    return frozenset(out)


def _def_is_exempt(node: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str]) -> bool:
    """``# F84-allowed: <why>`` on the def line or the line directly above."""
    for lineno in (node.lineno - 1, node.lineno):
        if 0 < lineno <= len(source_lines) and RATIONALE_TAG in source_lines[lineno - 1]:
            return True
    return False


def _harvest_write_sites(
    repo_root: Path,
) -> tuple[dict[str, list[tuple[Path, int]]], dict[str, frozenset[str]]]:
    """Scan ``kairix/**`` for config-write sites (see module docstring).

    Returns ``(sites, call_edges)``: ``sites`` maps writer name →
    ``[(repo-relative file, lineno), ...]``; ``call_edges`` maps writer
    name → terminal names its body calls.
    """
    sites: dict[str, list[tuple[Path, int]]] = {}
    call_edges: dict[str, frozenset[str]] = {}
    for path in _python_files(repo_root / "kairix"):
        text = path.read_text(encoding="utf-8")
        if not PREFILTER_RE.search(text):
            continue
        tree = _parse(path)
        if tree is None:
            continue
        source_lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not WRITER_NAME_RE.search(node.name):
                continue
            has_dump = any(_is_stream_yaml_dump(n) for n in ast.walk(node))
            if node.name.startswith("_") and not has_dump:
                continue  # private delegation shim — callee carries the contract
            if _def_is_exempt(node, source_lines):
                continue
            sites.setdefault(node.name, []).append((path.relative_to(repo_root), node.lineno))
            call_edges[node.name] = call_edges.get(node.name, frozenset()) | _called_names(node)
    return sites, call_edges


def _referenced_names(tree: ast.Module) -> set[str]:
    """Every Name id, Attribute attr, and imported name in the module."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out.update(alias.name for alias in node.names)
    return out


def _covered_writers(repo_root: Path, site_names: frozenset[str]) -> set[str]:
    """Writers covered by a test module — naming convention or registry tag."""
    covered: set[str] = set()
    for path in _python_files(repo_root / "tests"):
        text = path.read_text(encoding="utf-8")
        covered.update(m.group(1) for m in ROUND_TRIP_TAG_RE.finditer(text))
        if not any(name in text for name in site_names):
            continue
        tree = _parse(path)
        if tree is None:
            continue
        names = _referenced_names(tree)
        if names & READER_NAMES:
            covered.update(names & site_names)
    return covered


def _propagate_coverage(covered: set[str], call_edges: dict[str, frozenset[str]], site_names: frozenset[str]) -> None:
    """Fixed-point: a covered writer covers every harvested writer it calls."""
    changed = True
    while changed:
        changed = False
        for writer in list(covered & site_names):
            uncovered_callees = (call_edges.get(writer, frozenset()) & site_names) - covered
            if uncovered_callees:
                covered.update(uncovered_callees)
                changed = True


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Harvest write sites, resolve coverage, print per-site detail lines,
    and return the violating files (repo-relative).
    """
    sites, call_edges = _harvest_write_sites(repo_root)
    site_names = frozenset(sites)
    covered = _covered_writers(repo_root, site_names)
    _propagate_coverage(covered, call_edges, site_names)

    violations: set[Path] = set()
    for name in sorted(site_names - covered):
        for rel, lineno in sites[name]:
            violations.add(rel)
            print(f"  [f84] {rel}: line {lineno}: config-write site '{name}' has no composed round-trip test")
    return violations


def main() -> int:
    return gate("f84", collect_violations(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
