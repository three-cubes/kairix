"""F90: setup-wizard template ↔ route choreography stays referentially intact.

Motivation (EPIC #499 Phase 3; the dangling-button class)
---------------------------------------------------------
The wizard's screens are HTMX-choreographed: a button carries
``hx-post="/setup/tour/prep"`` and swaps the response into
``hx-target="#tour-prep-result"``; a link carries
``href="/setup/folder"``. None of that is type-checked. When the
capability tour (#490) replaced the first-search screen, a template
``hx-post`` pointing at a route that no longer existed would have
rendered a dead button — the operator clicks, nothing happens, no error
anywhere, every test green. F90 is the F52 referential-integrity pattern
applied to the browser tier: every ``/setup``-prefixed URL a template
fires at must resolve to a route REGISTERED in ``routes.py``, and every
element id a template targets must be DEFINED in some template.

What F90 checks (three referential invariants)
----------------------------------------------
  1. **URL → route.** Every ``hx-get`` / ``hx-post`` URL, every form
     ``action``, and every ``href`` in the setup templates that points at
     a ``/setup/...`` path resolves (after stripping any ``?query``) to a
     path the route table serves — an exact ``Route`` path, or a prefix
     under a ``Mount`` (the ``/static`` sub-mount). External / absolute
     (``https://``) and non-``/setup`` URLs are out of scope.
  2. **hx-target / hx-include → id.** Every ``hx-target="#id"`` and
     ``hx-include="#id"`` references an element ``id="id"`` defined in
     SOME setup template (HTMX resolves these against the live DOM, which
     spans the rendered screen + its swapped-in partials).
  3. **template reachability.** Every template file is reachable — it is
     rendered by a route (its name appears as a ``_TPL_*`` constant the
     route table uses) OR it ``{% extends %}`` / is extended within a
     reachable chain (``base.html`` is reached because the screens
     extend it).

How the registered surface is resolved (AST, not regex)
-------------------------------------------------------
``routes.py`` is AST-parsed once: module-level string constants are
evaluated (so ``_PROVIDER_URL = f"{SETUP_PATH_PREFIX}/provider"`` and the
``_TPL_*`` template names resolve), then every ``Route(path, ...)`` and
``Mount(path, ...)`` call in the route list contributes its path. The
mount prefix (``SETUP_PATH_PREFIX`` = ``/setup``) is prepended so the
registered set is in the same absolute space the templates use. The
``_TPL_*`` constants whose values reach a ``Route``/render are the
rendered-template set for invariant 3.

Intentionally NOT caught (precision over recall — a detector agents
distrust is worse than no detector):

  * **Dynamically-built URLs.** A template that assembles a path from a
    Jinja expression (``hx-post="{{ some_url }}"`` or
    ``href="/setup/{{ step }}"``) cannot be resolved statically — F90
    skips any attribute value containing ``{{`` / ``{%``. The wizard's
    real URLs are all literal today; a future dynamic one is review
    surface, not a false positive.
  * **Query-string parameters.** Only the path is checked; the
    ``?provider=...`` suffix the source screens append is stripped before
    resolution (the route doesn't vary on it).
  * **ids created client-side.** An id a ``<script>`` injects at runtime
    isn't in any template's markup; F90 only knows ids that appear as
    ``id="..."`` literals. The wizard targets only server-rendered ids.
  * **Non-setup URLs and HTTP method mismatch.** A ``GET`` href hitting a
    ``POST``-only route is a different (method) concern; F90 proves the
    PATH is served, matching F52's path-level granularity. External docs
    links (``https://...``) are out of scope by design.
  * **Routes registered outside the literal route list.** The resolver
    reads ``Route(...)`` / ``Mount(...)`` calls in ``routes.py``; a route
    added through some other mechanism would need its own registration
    site. The wizard builds its table as one literal list.

There is no grandfathering: every dangling URL / id / unreachable
template blocks at pre-commit / safe-commit / CI Stage 0.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

# The wizard tier this rule governs.
ROUTES_REL = "kairix/platform/setup/web/routes.py"
TEMPLATES_REL = "kairix/platform/setup/web/templates"

# The attribute values that carry a navigable URL.
_URL_ATTRS = ("hx-get", "hx-post", "href", "action")
# The attribute values that carry an element-id reference (HTMX selectors).
_ID_REF_ATTRS = ("hx-target", "hx-include")


# Match `name="value"` / `name='value'` for one attribute, capturing value.
def _attr_re(name: str) -> re.Pattern[str]:
    return re.compile(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"|\b{re.escape(name)}\s*=\s*\'([^\']*)\'')


_URL_ATTR_RES = {name: _attr_re(name) for name in _URL_ATTRS}
_ID_REF_ATTR_RES = {name: _attr_re(name) for name in _ID_REF_ATTRS}
# Element-id definitions: id="..." literals in the markup.
_ID_DEF_RE = _attr_re("id")
# A Jinja expression makes a value non-static — skip it.
_JINJA_RE = re.compile(r"\{\{|\{%")


REMEDIATION = """F90: a setup-wizard template fires at a route that isn't registered,
targets an element id that isn't defined, or a template is unreachable.
This is the dangling-button class — when the tour replaced first-search,
an hx-post to a removed route or an hx-target to a deleted id renders a
dead control: the operator clicks, nothing happens, no error anywhere,
every test green.

fix: per the failing line printed above —
  * URL → route: the /setup path the template fires at has no matching
    Route/Mount in kairix/platform/setup/web/routes.py. Either register
    the route (add a `Route("<mount-relative path>", ...)` to the route
    list) or correct the template's hx-get/hx-post/href/action to a path
    that IS served.
  * hx-target / hx-include → id: the `#id` the template targets is not
    defined as `id="id"` in any setup template. Add the target element,
    or point the selector at an id that exists.
  * unreachable template: the .html file is neither rendered by a route
    (named by a `_TPL_*` constant the route table uses) nor extended by a
    reachable template. Wire it into a route, make it extend a reachable
    base, or delete it.
next: re-run `python3 scripts/checks/check_f90_template_route_choreography.py`
to confirm the gate goes green.
run: bash scripts/safe-commit.sh "fix(setup): repair wizard template↔route choreography (#499 phase 3)"

Pass example: kairix/platform/setup/web/templates/setup/folder.html
  <button hx-post="/setup/folder/scan" hx-target="#scan-result" ...>Scan folder</button>
  <div id="scan-result"></div>
  # /folder/scan is `Route("/folder/scan", ...)` under the /setup mount;
  # #scan-result is defined in this same template — both resolve.

Forbidden example:
  <button hx-post="/setup/first-search/run" hx-target="#first-search-result">
  # /setup/first-search/run is not in the route table (the tour replaced
  # it) and #first-search-result is defined nowhere — a dead button that
  # ships silently."""


def _eval_constants(tree: ast.Module) -> dict[str, str]:
    """Evaluate module-level string constants in ``routes.py``.

    Handles plain ``NAME = "literal"`` and f-strings whose parts are
    other already-resolved constants (so ``_PROVIDER_URL =
    f"{SETUP_PATH_PREFIX}/provider"`` resolves once ``SETUP_PATH_PREFIX``
    is known). Two passes cover the forward references the wizard uses.
    """
    consts: dict[str, str] = {}

    def _resolve(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    resolved = _resolve(value.value)
                    if resolved is None:
                        return None
                    parts.append(resolved)
                else:
                    return None
            return "".join(parts)
        return None

    assigns = [
        (node.targets[0].id, node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    ]
    # Two passes resolve constants that reference earlier-defined ones.
    for _ in range(2):
        for name, value in assigns:
            if name in consts:
                continue
            resolved = _resolve(value)
            if resolved is not None:
                consts[name] = resolved
    return consts


def _route_arg_path(arg: ast.expr, consts: dict[str, str]) -> str | None:
    """The path string of a ``Route``/``Mount`` first positional arg —
    a literal, or a ``_*_ROUTE``/``_*_PATH`` constant name."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return consts.get(arg.id)
    return None


def _registered_paths(tree: ast.Module, consts: dict[str, str], mount_prefix: str) -> tuple[set[str], set[str]]:
    """Return ``(exact_paths, mount_prefixes)`` in absolute (``/setup/...``)
    space — every ``Route``/``Mount`` path with the mount prefix prepended.
    """
    exact: set[str] = set()
    mounts: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in ("Route", "Mount") or not node.args:
            continue
        rel = _route_arg_path(node.args[0], consts)
        if rel is None:
            continue
        absolute = mount_prefix.rstrip("/") + rel if rel.startswith("/") else f"{mount_prefix.rstrip('/')}/{rel}"
        if node.func.id == "Mount":
            mounts.add(absolute.rstrip("/"))
        else:
            exact.add(absolute)
    return exact, mounts


def _rendered_template_names(consts: dict[str, str]) -> set[str]:
    """The ``_TPL_*`` constant VALUES — the template names the route table
    renders (basis for reachability invariant 3)."""
    return {value for name, value in consts.items() if name.startswith("_TPL_") and value.endswith(".html")}


def _parse_routes(repo_root: Path) -> tuple[set[str], set[str], set[str]] | None:
    """``(exact_paths, mount_prefixes, rendered_template_names)`` for the
    wizard, or ``None`` if ``routes.py`` is unreadable."""
    path = repo_root / ROUTES_REL
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None
    consts = _eval_constants(tree)
    mount_prefix = consts.get("SETUP_PATH_PREFIX", "/setup")
    exact, mounts = _registered_paths(tree, consts, mount_prefix)
    # The wizard's own outer Mount(SETUP_PATH_PREFIX, ...) prepended the
    # prefix to itself above (-> "/setup/setup"), which is noise — drop
    # it. The mount ROOT is served by the inner Route("/"): a bare
    # `/setup` href (provider.html's Back-to-welcome link) redirects to
    # `/setup/` and renders welcome, so register the prefix itself.
    mounts.discard(mount_prefix.rstrip("/") + mount_prefix)
    exact.add(mount_prefix.rstrip("/"))
    exact.add(mount_prefix.rstrip("/") + "/")
    return exact, mounts, _rendered_template_names(consts)


def _template_files(repo_root: Path) -> list[Path]:
    """Every ``.html`` template under the wizard's template tree."""
    root = repo_root / TEMPLATES_REL
    if not root.exists():
        return []
    return sorted(root.rglob("*.html"))


def _attr_values(text: str, attr_res: dict[str, re.Pattern[str]]) -> list[tuple[str, str, int]]:
    """``(attr_name, value, lineno)`` for each matching attribute — Jinja
    expression values are skipped (non-static)."""
    out: list[tuple[str, str, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in attr_res.items():
            for match in pattern.finditer(line):
                value = match.group(1) if match.group(1) is not None else match.group(2)
                if value and not _JINJA_RE.search(value):
                    out.append((name, value, lineno))
    return out


def _defined_ids(templates: dict[Path, str]) -> set[str]:
    """Every ``id="..."`` literal across all templates."""
    ids: set[str] = set()
    for text in templates.values():
        for match in _ID_DEF_RE.finditer(text):
            value = match.group(1) if match.group(1) is not None else match.group(2)
            if value and not _JINJA_RE.search(value):
                ids.add(value)
    return ids


def _extended_templates(templates: dict[Path, str], template_root: Path) -> set[str]:
    """Template names appearing in an ``{% extends "<name>" %}`` directive
    — these are reachable through the extending (reachable) screen."""
    extends_re = re.compile(r'{%-?\s*extends\s+["\']([^"\']+)["\']')
    out: set[str] = set()
    for text in templates.values():
        out.update(extends_re.findall(text))
    return out


def _url_resolves(url: str, exact: set[str], mounts: set[str], mount_prefix: str) -> bool:
    """True iff a ``/setup``-prefixed ``url`` resolves to a registered
    path. Non-``/setup`` and absolute URLs are out of scope (treated as
    resolved — F90 only governs the wizard's own paths)."""
    if "://" in url or not url.startswith(mount_prefix):
        return True
    path = url.split("?", 1)[0].split("#", 1)[0]
    if path in exact:
        return True
    return any(path == m or path.startswith(m + "/") for m in mounts)


def _template_rel(repo_root: Path, path: Path) -> str:
    """The template NAME as routes/extends reference it (loader-relative —
    e.g. ``setup/folder.html``), drawn from the templates root."""
    return str(path.relative_to(repo_root / TEMPLATES_REL))


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Check the three referential invariants; print per-issue detail;
    return the violating files (repo-relative)."""
    parsed = _parse_routes(repo_root)
    if parsed is None:
        rel = Path(ROUTES_REL)
        print(f"  [f90] {ROUTES_REL}: routes.py could not be parsed — cannot resolve the route table")
        return {rel}
    exact, mounts, rendered_names = parsed
    mount_prefix = "/setup"

    template_paths = _template_files(repo_root)
    templates = {p: p.read_text(encoding="utf-8") for p in template_paths}
    defined_ids = _defined_ids(templates)
    extended = _extended_templates(templates, repo_root / TEMPLATES_REL)
    reachable_names = rendered_names | extended

    violations: set[Path] = set()
    for path, text in templates.items():
        rel = path.relative_to(repo_root)
        name = _template_rel(repo_root, path)

        # Invariant 1 — every /setup URL resolves to a registered route.
        for attr, value, lineno in _attr_values(text, _URL_ATTR_RES):
            if not _url_resolves(value, exact, mounts, mount_prefix):
                violations.add(rel)
                print(f"  [f90] {rel}: line {lineno}: {attr}='{value}' targets an unregistered /setup route")

        # Invariant 2 — every hx-target/hx-include id is defined somewhere.
        for attr, value, lineno in _attr_values(text, _ID_REF_ATTR_RES):
            if not value.startswith("#"):
                continue  # closest/this/find selectors etc. — not an id ref
            target_id = value[1:]
            if target_id and target_id not in defined_ids:
                violations.add(rel)
                print(f"  [f90] {rel}: line {lineno}: {attr}='{value}' targets id '#{target_id}' defined nowhere")

        # Invariant 3 — every template is reachable (rendered or extended).
        if name not in reachable_names:
            violations.add(rel)
            print(
                f"  [f90] {rel}: template '{name}' is unreachable — no route renders it, nothing reachable extends it"
            )

    return violations


def main() -> int:
    return gate("f90", collect_violations(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
