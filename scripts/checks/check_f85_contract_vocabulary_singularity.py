"""F85: registered cross-tier contract vocabularies are single-sourced.

Motivation (EPIC #499 Phase 1; the session-escape-8 phase-string class)
-----------------------------------------------------------------------
A *contract vocabulary* is a small closed set of string members that
more than one architectural tier must agree on — the
``SourceAuthStatus.phase`` strings (``idle`` … ``failed``), the
wizard's azure-provider grouping. When such a vocabulary is encoded
three times — once in the backend, once in the wizard routes, once as
raw strings in a Jinja template — a single rename breaks the
choreography while every suite stays green: the HX-Redirect phase logic
drifted out of sync exactly this way and shipped (session escape 8).

The M11 fix single-sourced the phase vocabulary and the azure grouping
into :mod:`kairix.platform.setup.service`. That module *owns* them;
``routes.py`` imports the symbols and republishes them through
``env.globals``; ``source_auth_status.html`` reads
``PHASE_CONSENT`` / ``PHASE_DONE`` from the rendered context, never raw
phase strings. F85 makes that end-state structural: it pins each
registered vocabulary to one owning module and fails any *other* tier
that re-declares a member as a constant, collects it into a literal, or
(in a template) compares against the raw string instead of the
``env.globals`` symbol.

What F85 harvests (the DECLARED registry)
-----------------------------------------
The registry lives inside this check — ``VOCABULARIES`` maps a
vocabulary name to ``(owning_module, (member_literal, ...))``. Members
are listed explicitly (not auto-discovered) so the rule is
precision-first: it flags only the literals an operator deliberately
registered as cross-tier contracts, never every shared constant. The
owner module is exempt by construction; the sweep covers the rest of
the setup tier (``kairix/platform/setup/**``, minus the owner) and its
templates (``kairix/platform/setup/web/templates/**``).

What counts as a re-declaration (a violation)
---------------------------------------------
In a non-owning **Python** module under the setup tier, a member string
appearing in a *vocabulary-definition shape*:

  * the right-hand side of a module-level or class-level constant
    assignment — ``_PLUGIN_AZURE_FOUNDRY = "azure_foundry"``;
  * an element of a ``set`` / ``frozenset`` / ``tuple`` / ``list``
    literal, or a ``dict`` **key** — ``(_PLUGIN_AZURE_FOUNDRY,
    "azure_legacy")``.

In a setup-tier **template** (``.html``), the raw member string appearing
as a quoted literal anywhere in the rendered text — the M11 contract is
that templates branch on the ``env.globals`` symbol
(``status.phase == PHASE_CONSENT``), so a raw ``== "consent"`` is the
regression this rule exists to stop.

A member is NOT a violation when it is imported from the owning module
(``from kairix.platform.setup.service import PHASE_CONSENT``) — that is
the desired pattern — or when its line carries a ``# F85-allowed: <why>``
rationale.

Intentionally NOT caught (precision over recall — a detector agents
distrust is worse than no detector):

  * **Incidental string uses that are not vocabulary definitions** — a
    call keyword or ``dict`` *value* (``{"prompt": "consent"}`` is the
    OAuth protocol parameter, not the wizard phase), an attribute-name
    string (``getattr(result, "failed", 0)``). The definition-shape
    gate (const-assign RHS / collection element / dict key) is the
    contract; reviewers hold the line on it.
  * **Members outside the setup tier.** ``azure_foundry`` /
    ``azure_legacy`` are *also* the identity strings each provider
    plugin legitimately owns (``PROVIDER_NAME = "azure_foundry"`` in
    ``kairix/providers/azure_foundry/``); the phase words appear as
    English prose across the repo. Sweeping the whole repo would
    over-fire massively, so F85 scopes to the wizard tier where the
    cross-tier contract actually lives.
  * **Auto-discovery of un-registered shared constants.** F85 only
    knows the vocabularies in ``VOCABULARIES``; it makes no attempt to
    infer "this looks like a shared enum". Adding a vocabulary is a
    deliberate registry edit — that is the point.
  * **Substring / interpolation hits.** Only whole quoted-string
    literals match; ``"the consent screen"`` prose in a template body
    is a different token and is not flagged (the detector matches the
    exact member string as a Python ``ast.Constant`` or a template
    quoted literal, not a substring of running text).

There is no grandfathering: every re-declaration of a registered
vocabulary blocks at pre-commit / safe-commit / CI Stage 0.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

# ---------------------------------------------------------------------------
# The DECLARED registry. Each vocabulary pins one owning module and the
# explicit member literals that module single-sources. Members are listed
# (not harvested) so the rule flags ONLY deliberately-registered cross-tier
# contracts — precision over recall (see module docstring).
# ---------------------------------------------------------------------------
SERVICE_OWNER = "kairix/platform/setup/service.py"

VOCABULARIES: dict[str, tuple[str, tuple[str, ...]]] = {
    # SourceAuthStatus.phase vocabulary (#489) — backend reports it,
    # routes republish via env.globals, source_auth_status.html branches
    # on the PHASE_* symbols. Session escape 8: a rename desynced the
    # three copies with every suite green.
    "source_auth_phase": (SERVICE_OWNER, ("idle", "starting", "consent", "exchanging", "done", "failed")),
    # The wizard's azure-provider grouping (#484) — service.py owns
    # AZURE_PROVIDER_NAMES; the key screen and the probe map react to it.
    "wizard_azure_provider_names": (SERVICE_OWNER, ("azure_foundry", "azure_legacy")),
}

# All member strings across every vocabulary, with the owning module each
# belongs to — flattened once for the sweep.
_MEMBER_OWNER: dict[str, str] = {member: owner for owner, members in VOCABULARIES.values() for member in members}

# The tier F85 sweeps — the setup package and its templates. Scoped here
# (not the whole repo) because the member strings are legitimately owned
# elsewhere (provider plugins' PROVIDER_NAME; English prose).
SETUP_TIER_REL = "kairix/platform/setup"
TEMPLATE_TIER_REL = "kairix/platform/setup/web/templates"

RATIONALE_TAG = "# F85-allowed:"

# A cheap raw-text pre-filter — only files mentioning a member pay the parse.
_PREFILTER_RE = re.compile("|".join(re.escape(m) for m in sorted(_MEMBER_OWNER)))


def _vocab_for(member: str) -> str:
    """The vocabulary name a member belongs to (members are unique across
    vocabularies in the registry)."""
    for name, (_owner, members) in VOCABULARIES.items():
        if member in members:
            return name
    return "?"  # unreachable: callers only pass registered members


REMEDIATION = """F85: a registered cross-tier contract vocabulary is re-declared
outside its owning module — the session-escape-8 class where phase
strings were encoded three times (backend, routes, template) so a
rename broke the HX-Redirect choreography with every suite green.

fix: import the member from its owning module instead of re-declaring
the literal. The setup contract vocabularies live in
kairix/platform/setup/service.py:
  * Python tier — `from kairix.platform.setup.service import PHASE_CONSENT`
    (or AZURE_PROVIDER_NAMES for the azure grouping) and reference the
    symbol; delete the local `X = "consent"` / `(..., "azure_legacy")`.
  * Template tier — branch on the env.globals symbol the routes layer
    publishes (`{% if status.phase == PHASE_CONSENT %}`), never the raw
    string (`== "consent"`). Add the global in build_jinja_env if a new
    member needs surfacing.
If a literal is genuinely NOT the registered vocabulary (an OAuth
`prompt=consent` query param, a `getattr(obj, "failed")` attribute
name), it is already excluded by the definition-shape gate; if a real
definition must stay, put `# F85-allowed: <why>` on its line.
next: re-run python3 scripts/checks/check_f85_contract_vocabulary_singularity.py
to confirm the gate goes green. See the EPIC #499 Phase 1 brief for the
session-escape-8 post-mortem this rule mechanises, and
kairix/platform/setup/service.py for the owning module.
run: bash scripts/safe-commit.sh "fix(setup): import <vocab> member from service.py (#499 single-source)"

Pass example: kairix/platform/setup/web/routes.py (post-M11)
  from kairix.platform.setup.service import PHASE_CONSENT, PHASE_DONE
  ...
  env.globals["PHASE_CONSENT"] = PHASE_CONSENT   # template reads the symbol
  # source_auth_status.html: {% if status.phase == PHASE_CONSENT %}

Forbidden example:
  # kairix/platform/setup/backends.py — re-declaring the azure grouping:
  _PLUGIN_AZURE_FOUNDRY = "azure_foundry"   # owned by service.py — import it
  _PLUGIN_AZURE_LEGACY = "azure_legacy"
  # OR source_auth_status.html branching on a raw phase string:
  {% if status.phase == "consent" %}        # drifts the moment service.py renames it"""


def _python_files(root: Path) -> list[Path]:
    """All ``.py`` files under ``root``, skipping ``__pycache__``."""
    if not root.exists():
        return []
    return [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts]


def _template_files(root: Path) -> list[Path]:
    """All ``.html`` templates under ``root``."""
    if not root.exists():
        return []
    return sorted(root.rglob("*.html"))


def _definition_shape_constants(tree: ast.Module) -> list[ast.Constant]:
    """Every string ``Constant`` appearing in a *vocabulary-definition
    shape*: a const-assignment RHS, or an element of a
    set/frozenset/tuple/list/dict-key literal.

    Incidental uses (call keyword values, dict *values*, attribute-name
    strings, f-string parts) are deliberately excluded — only the shapes
    a developer would use to (re-)declare a vocabulary are harvested.
    """
    out: list[ast.Constant] = []

    def _add_if_str(node: ast.expr) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node)

    for node in ast.walk(tree):
        # `NAME = "member"` / `NAME: T = "member"` — a constant binding.
        if isinstance(node, ast.Assign):
            _add_if_str(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _add_if_str(node.value)
        # Collection literals — set/tuple/list members live here. The
        # `frozenset({...})` / `set([...])` call forms wrap a Set/List the
        # AST walk reaches independently, so no Call handling is needed.
        elif isinstance(node, (ast.Set, ast.Tuple, ast.List)):
            for elt in node.elts:
                _add_if_str(elt)
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if key is not None:
                    _add_if_str(key)
    return out


def _python_violations(source_lines: list[str], tree: ast.Module) -> list[str]:
    """Detail lines for member re-declarations in a setup-tier ``.py``."""
    details: list[str] = []
    for const in _definition_shape_constants(tree):
        member = const.value
        owner = _MEMBER_OWNER.get(member)
        if owner is None:
            continue
        lineno = const.lineno
        if 0 < lineno <= len(source_lines) and RATIONALE_TAG in source_lines[lineno - 1]:
            continue
        details.append(
            f"line {lineno}: re-declares '{member}' from the '{_vocab_for(member)}' vocabulary "
            f"(owned by {owner}) — import the symbol instead"
        )
    return details


def _template_violations(text: str) -> list[str]:
    """Detail lines for raw member strings quoted in a template."""
    details: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if RATIONALE_TAG in raw:
            continue
        for member in _MEMBER_OWNER:
            if f'"{member}"' in raw or f"'{member}'" in raw:
                details.append(
                    f"line {lineno}: raw '{member}' string from the '{_vocab_for(member)}' vocabulary — "
                    f"branch on the env.globals symbol instead"
                )
    return details


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Sweep the setup tier + templates for re-declarations of any
    registered vocabulary member; print per-site detail; return the
    violating files (repo-relative).
    """
    owner_rels = {owner for owner, _members in VOCABULARIES.values()}
    violations: set[Path] = set()

    for path in _python_files(repo_root / SETUP_TIER_REL):
        rel = str(path.relative_to(repo_root))
        if rel in owner_rels:
            continue  # the owning module IS the single source — exempt
        text = path.read_text(encoding="utf-8")
        if not _PREFILTER_RE.search(text):
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        details = _python_violations(text.splitlines(), tree)
        if details:
            violations.add(path.relative_to(repo_root))
            for detail in details:
                print(f"  [f85] {rel}: {detail}")

    for path in _template_files(repo_root / TEMPLATE_TIER_REL):
        rel = str(path.relative_to(repo_root))
        text = path.read_text(encoding="utf-8")
        if not _PREFILTER_RE.search(text):
            continue
        details = _template_violations(text)
        if details:
            violations.add(path.relative_to(repo_root))
            for detail in details:
                print(f"  [f85] {rel}: {detail}")

    return violations


def main() -> int:
    return gate("f85", collect_violations(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
