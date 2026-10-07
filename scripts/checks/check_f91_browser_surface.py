"""F91: browser-surface governance — security headers + inline-script discipline.

Motivation (EPIC #499 Phase 3)
------------------------------
The setup wizard (``kairix/platform/setup/web/``) is the only part of
kairix that serves HTML to a browser. It renders operator-controlled
values — config paths, provider labels, folder hints — into pages, and
it ships hand-written inline ``<script>`` glue for its HTMX reveal
animations. Two browser-side risks ride along with that surface, and
F91 locks both down as the SINGLE rule that owns the browser tier:

  * **Limb A — security headers.** Without ``X-Content-Type-Options:
    nosniff`` a reflected value can be content-sniffed into an
    executable type; without a frame-denial header the wizard can be
    framed for clickjacking; without a Content-Security-Policy there is
    no backstop on what the page may fetch or execute. The wizard's
    HTML-serving path must SET all three. The behavioural proof lives in
    a contract test (``tests/platform/setup/`` drives the live mount and
    asserts the headers arrive); this static limb proves the render
    path REFERENCES the three header names so the contract can never be
    satisfied by accident-then-deleted code.

  * **Limb B — inline-script governance.** Every inline ``<script>``
    body in the wizard templates must be (1) rationale-tagged with a
    ``{# F91-inline: why #}`` Jinja comment or an equivalent HTML
    comment immediately above it, (2) size-capped at
    :data:`MAX_INLINE_SCRIPT_LINES` non-blank lines, and (3) not a
    byte-for-byte duplicate of another template's inline script (the
    same ``afterSwap`` listener copy-pasted into N screens is a shared
    include waiting to happen, and N un-reviewed CSP/XSS surfaces). An
    external ``<script src=...>`` reference (no inline body) is NOT an
    inline script and is never flagged.

What Limb A harvests
--------------------
``kairix/platform/setup/web/routes.py`` must contain all of:
``X-Content-Type-Options``, a frame-denial signal (``X-Frame-Options``
or ``frame-ancestors``), and ``Content-Security-Policy``. A missing name
yields a synthetic violation path
``routes.py::missing-header-<name>`` so the failure reads as an
inventory.

What Limb B harvests
--------------------
Every ``*.html`` under ``kairix/platform/setup/web/templates/``. For
each inline ``<script>...</script>`` (a ``<script>`` tag WITH a body —
``<script src=...></script>`` and self-empty tags are skipped):

  * un-tagged → ``<template>::untagged-inline-script@L<line>``
  * over the line cap → ``<template>::oversized-inline-script@L<line>``
  * body duplicated in another template → BOTH templates flagged
    ``<template>::duplicate-inline-script@L<line>``

Intentionally NOT caught (precision over recall — a detector agents
distrust is worse than no detector)
-----------------------------------------------------------------------
  * External ``<script src=...>`` with no body (the vendored
    ``htmx.min.js`` reference in base.html) — it is governed by the CSP
    ``script-src`` allow-list, not by inline-body rules.
  * Inline event-handler attributes (``onclick=...``) and
    ``javascript:`` URLs — the wizard ships none today; revisit with a
    dedicated attribute scan if one appears (this rule stays a
    ``<script>``-body detector to keep the AST/text contract simple and
    its false-positive rate at zero).
  * ``<style>`` blocks — CSS, not script; out of scope.
  * The EXACT header VALUES (a specific CSP directive string) — Limb A
    proves the render path references the header NAMES; the contract
    test owns the value assertions, where a wrong value fails loudly and
    legibly. Encoding the value string here would make every CSP tweak a
    two-file edit for no added safety.
  * Templates outside the wizard tree — F91 governs the one browser
    surface kairix ships; a future second HTML surface extends
    ``TEMPLATE_ROOTS`` deliberately.

There is no grandfathering: every ungoverned inline script and any
regression of the header set blocks at pre-commit / safe-commit / CI
Stage 0.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

# Limb A — the render-path file and the header names it must reference.
ROUTES_REL = Path("kairix/platform/setup/web/routes.py")
NOSNIFF_HEADER = "X-Content-Type-Options"
CSP_HEADER = "Content-Security-Policy"
# Frame denial counts via EITHER the legacy header or the CSP directive.
FRAME_DENIAL_SIGNALS = ("X-Frame-Options", "frame-ancestors")

# Limb B — where the wizard templates live and the inline-script budget.
TEMPLATE_ROOTS = (Path("kairix/platform/setup/web/templates"),)
MAX_INLINE_SCRIPT_LINES = 20

# A <script ...>BODY</script> with a non-empty BODY. DOTALL so the body
# can span lines; non-greedy so adjacent scripts don't merge.
_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL | re.IGNORECASE)
# An src= attribute marks an external reference (no inline body to govern).
_SRC_ATTR_RE = re.compile(r"\bsrc\s*=", re.IGNORECASE)
# Accepted rationale tags on the line(s) directly above the <script>.
_RATIONALE_RE = re.compile(r"\{#\s*F91-inline:|<!--\s*F91-inline:")

REMEDIATION = """F91: the wizard's browser surface lost a security header, or an
inline <script> is ungoverned (untagged / oversized / duplicated).

The setup wizard is the one HTML surface kairix serves. Limb A keeps
its responses carrying nosniff + frame-denial + a Content-Security-
Policy; Limb B keeps every hand-written inline script small, justified,
and un-duplicated so the CSP's 'unsafe-inline' allowance stays a tiny
reviewed surface.

fix (Limb A — a header name vanished from the render path):
  restore the missing name in kairix/platform/setup/web/routes.py
  (_SECURITY_HEADERS) — X-Content-Type-Options, a frame-denial header
  (X-Frame-Options: DENY or a CSP frame-ancestors directive), and
  Content-Security-Policy. The contract test in tests/platform/setup
  asserts they arrive on a live response.
fix (Limb B — an inline <script> is ungoverned):
  * untagged: put a `{# F91-inline: <why this script is inline #}`
    Jinja comment (or an `<!-- F91-inline: ... -->` HTML comment) on the
    line directly above the <script> tag.
  * oversized: a >20-line inline script belongs in a shared static .js
    under web/static/ referenced via <script src=...>, not inline.
  * duplicated: the same body appears in another template — lift it into
    a shared static include and reference it from both, or into a base
    template block; copy-paste inline JS is N un-reviewed CSP surfaces.
next: re-run python3 scripts/checks/check_f91_browser_surface.py to
confirm the gate goes green.
run: bash scripts/safe-commit.sh "fix(wizard): govern the browser surface (F91)"

Pass example: a governed inline script —
  {# F91-inline: reveal the Save button once validation succeeds —
     three lines of HTMX glue, single-template, no shared-include need #}
  <script>
    document.addEventListener('htmx:afterSwap', function () {
      if (document.querySelector('.kx-validation-success')) {
        document.getElementById('save-key-btn').classList.add('kx-revealed');
      }
    });
  </script>

Forbidden example: an ungoverned inline script —
  <script>
    // no F91-inline rationale above; copy-pasted verbatim into
    // folder.html too — two un-reviewed CSP/XSS surfaces, one bug fix
    // away from drifting apart.
    document.addEventListener('htmx:afterSwap', function () { ... });
  </script>"""


def _is_inline_script(attrs: str, body: str) -> bool:
    """True iff this ``<script>`` carries an inline body to govern.

    An ``src=`` reference is external (no body to scan); a whitespace-
    only body is an empty tag.
    """
    if _SRC_ATTR_RE.search(attrs):
        return False
    return bool(body.strip())


def _line_of(text: str, index: int) -> int:
    """1-based line number of ``index`` in ``text``."""
    return text.count("\n", 0, index) + 1


def _non_blank_line_count(body: str) -> int:
    """Lines in the script body that carry any non-whitespace."""
    return sum(1 for line in body.splitlines() if line.strip())


def _has_rationale_above(text: str, tag_start: int) -> bool:
    """True iff a ``F91-inline:`` rationale sits on a line directly above
    the ``<script>`` tag (skipping blank lines between the comment and
    the tag).
    """
    preceding = text[:tag_start].splitlines()
    for line in reversed(preceding):
        if not line.strip():
            continue  # allow blank lines between the comment and the tag
        return bool(_RATIONALE_RE.search(line))
    return False


def _template_files(repo_root: Path) -> list[Path]:
    """Every ``*.html`` under the governed template roots."""
    out: list[Path] = []
    for root in TEMPLATE_ROOTS:
        base = repo_root / root
        if not base.exists():
            continue
        out.extend(sorted(p for p in base.rglob("*.html")))
    return out


def _harvest_inline_scripts(repo_root: Path) -> list[tuple[Path, int, str]]:
    """Return ``[(repo-relative template, line, normalised-body), ...]``
    for every inline ``<script>`` in the governed templates.
    """
    found: list[tuple[Path, int, str]] = []
    for path in _template_files(repo_root):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(repo_root)
        for match in _SCRIPT_RE.finditer(text):
            attrs, body = match.group(1), match.group(2)
            if not _is_inline_script(attrs, body):
                continue
            line = _line_of(text, match.start())
            normalised = "\n".join(ln.strip() for ln in body.splitlines() if ln.strip())
            found.append((rel, line, normalised))
    return found


def _limb_a_violations(repo_root: Path) -> set[Path]:
    """Synthetic paths for any security header missing from the render path."""
    routes = repo_root / ROUTES_REL
    if not routes.is_file():
        return {Path(f"{ROUTES_REL}::missing-render-path")}
    text = routes.read_text(encoding="utf-8")
    missing: set[Path] = set()
    if NOSNIFF_HEADER not in text:
        missing.add(Path(f"{ROUTES_REL}::missing-header-{NOSNIFF_HEADER}"))
    if not any(signal in text for signal in FRAME_DENIAL_SIGNALS):
        missing.add(Path(f"{ROUTES_REL}::missing-header-frame-denial"))
    if CSP_HEADER not in text:
        missing.add(Path(f"{ROUTES_REL}::missing-header-{CSP_HEADER}"))
    return missing


def _limb_b_violations(repo_root: Path) -> set[Path]:
    """Untagged / oversized / cross-template-duplicate inline scripts."""
    scripts = _harvest_inline_scripts(repo_root)
    bodies_by_template: dict[str, set[Path]] = {}
    for rel, _line, body in scripts:
        bodies_by_template.setdefault(body, set()).add(rel)

    violations: set[Path] = set()
    for path in _template_files(repo_root):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(repo_root)
        for match in _SCRIPT_RE.finditer(text):
            attrs, body = match.group(1), match.group(2)
            if not _is_inline_script(attrs, body):
                continue
            line = _line_of(text, match.start())
            normalised = "\n".join(ln.strip() for ln in body.splitlines() if ln.strip())
            if not _has_rationale_above(text, match.start()):
                violations.add(Path(f"{rel}::untagged-inline-script@L{line}"))
                print(f"  [f91] {rel}: line {line}: inline <script> has no '{{# F91-inline: #}}' rationale above it")
            if _non_blank_line_count(body) > MAX_INLINE_SCRIPT_LINES:
                violations.add(Path(f"{rel}::oversized-inline-script@L{line}"))
                print(
                    f"  [f91] {rel}: line {line}: inline <script> exceeds "
                    f"{MAX_INLINE_SCRIPT_LINES} lines — move it to web/static/"
                )
            if len(bodies_by_template.get(normalised, set())) > 1:
                others = sorted(str(p) for p in bodies_by_template[normalised] if p != rel)
                violations.add(Path(f"{rel}::duplicate-inline-script@L{line}"))
                print(f"  [f91] {rel}: line {line}: inline <script> duplicated in {', '.join(others)} — share it")
    return violations


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Union of Limb A (header set) and Limb B (inline-script) violations."""
    return _limb_a_violations(repo_root) | _limb_b_violations(repo_root)


def main() -> int:
    return gate("f91", collect_violations(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
