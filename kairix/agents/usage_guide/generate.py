"""Generate the bundled agent usage guide from the capability catalogue.

The bundled guide (``data/agent-usage-guide.md``) is what a self-training
agent reads, and it is what F99 (usage-guide currency) asserts on. To stop
the guide drifting from the tool registry, the capability index is
**derived** from :data:`kairix.agents.mcp.server.CAPABILITIES_CATALOG`
rather than hand-maintained: this module renders the six loop-ordered groups
(Orient → Find → Synthesise → Remember → Check health → Escalate) straight
from :func:`~kairix.agents.mcp.server.by_loop_group`, with the real **bare**
MCP tool names an agent puts on the wire (``search``, not ``tool_search``).

The prose around the index — the "core loops — read this first" lead, the
scope / intent / troubleshooting sections — lives in the sibling template
``data/agent-usage-guide.md.tmpl``. The template carries a pair of
generated-index markers; this module reads the template, renders the index
from the catalogue, and writes the assembled guide back to
``data/agent-usage-guide.md``.

Regenerate after any catalogue change::

    python -m kairix.agents.usage_guide.generate            # rewrite the guide
    python -m kairix.agents.usage_guide.generate --check    # verify it is current

``--check`` exits non-zero when the committed guide is stale, so the same
call doubles as a currency guard in the test suite.
"""

from __future__ import annotations

import argparse
import sys
from importlib import resources
from pathlib import Path

from kairix.agents.mcp.server import (
    CAP_CATEGORY_RETRIEVAL,
    CAP_CATEGORY_SYNTHESIS,
    LOOP_GROUP_CHECK_HEALTH,
    LOOP_GROUP_ESCALATE,
    LOOP_GROUP_FIND,
    LOOP_GROUP_ORDER,
    LOOP_GROUP_ORIENT,
    LOOP_GROUP_REMEMBER,
    LOOP_GROUP_SYNTHESISE,
    RECOMMEND_CAPABILITIES_TOOL_NAME,
    Capability,
    by_loop_group,
)
from kairix.paths import agent_cli_roots, confine_to_roots

_GUIDE_PACKAGE = "kairix.agents.usage_guide"
_TEMPLATE_RESOURCE = "data/agent-usage-guide.md.tmpl"
_GUIDE_RESOURCE = "data/agent-usage-guide.md"

# The template carries this marker pair; everything between them is replaced
# by the freshly-rendered capability index. Keeping the markers in the output
# lets a maintainer see the block is machine-owned (and lets ``--check``
# re-derive it deterministically).
_BEGIN_MARKER = "<!-- BEGIN GENERATED capability index (see kairix.agents.usage_guide.generate) -->"
_END_MARKER = "<!-- END GENERATED capability index -->"

# One purpose line per loop group — the IA copy that tells an agent when to
# reach into the group. Keyed by the same LOOP_GROUP_* constants the catalogue
# groups by, so the six sections stay aligned with by_loop_group().
_GROUP_PURPOSE: dict[str, str] = {
    LOOP_GROUP_ORIENT: "Start here every session — learn the surface and warm the caches before you retrieve.",
    LOOP_GROUP_FIND: "The read loop: `search` first, then `expand` / `timeline` / `entity` / `facts_about` the hits.",
    LOOP_GROUP_SYNTHESISE: "When one hit isn't the whole answer — combine many, and cite the `source_uri` on each.",
    LOOP_GROUP_REMEMBER: "The write loop — save a fact or transcript now so the next session can recall it.",
    LOOP_GROUP_CHECK_HEALTH: "Agent-callable diagnostics — run one when a call misbehaves, before giving up.",
    LOOP_GROUP_ESCALATE: "Operator-only — the MCP tool returns an escalation envelope you hand to your human.",
}

# Categories whose result rows carry the resolvable ``source_uri`` breadcrumb
# (F97 / F98) — the retrieval and synthesis surfaces. An agent cites or feeds
# that pointer back to ``expand``; every other surface returns a status
# envelope with nothing to cite.
_RETURNS_SOURCE_URI: frozenset[str] = frozenset({CAP_CATEGORY_RETRIEVAL, CAP_CATEGORY_SYNTHESIS})

_TABLE_HEADER = "| capability | when to reach | CLI | MCP tool | returns `source_uri` |"
_TABLE_RULE = "|---|---|---|---|---|"


def _advertised(cap: Capability) -> bool:
    """Return whether a capability is advertised in the agent guide.

    Mirrors :func:`kairix.agents.mcp.server.agent_facing`'s one exclusion:
    the flag-gated recommender (``recommend_capabilities``) defaults OFF and
    returns a disabled envelope, so advertising it would read as live. Every
    other catalogue row — including the operator-only escalation stubs, which
    belong in the Escalate group — is advertised.
    """
    return cap.mcp_tool != RECOMMEND_CAPABILITIES_TOOL_NAME


def _mcp_cell(cap: Capability) -> str:
    """Render the MCP-tool table cell — the bare wire name, or the escalation."""
    if cap.mcp_tool is None:
        return f"operator only (escalate: `{cap.escalate_via}`)"
    return f"`{cap.mcp_tool}`"


def _when_cell(cap: Capability) -> str:
    """Render the when-to-reach cell, falling back to the group's own purpose."""
    return cap.when_to_use or "see the group note above"


def _source_uri_cell(cap: Capability) -> str:
    """Render the source_uri cell — ``yes`` for breadcrumb-carrying surfaces."""
    return "yes" if cap.category in _RETURNS_SOURCE_URI else "—"


def _row(cap: Capability) -> str:
    """Render one capability as a markdown table row with its bare wire name."""
    return f"| `{cap.name}` | {_when_cell(cap)} | `{cap.cli}` | {_mcp_cell(cap)} | {_source_uri_cell(cap)} |"


def _render_group(group_name: str, caps: tuple[Capability, ...]) -> str:
    """Render one loop-ordered group: heading, purpose line, capability table."""
    lines = [f"### {group_name}", "", _GROUP_PURPOSE[group_name], "", _TABLE_HEADER, _TABLE_RULE]
    lines.extend(_row(cap) for cap in caps)
    return "\n".join(lines)


def render_capability_index() -> str:
    """Render the six loop-ordered capability groups from the live catalogue.

    Walks :func:`kairix.agents.mcp.server.by_loop_group` in
    :data:`~kairix.agents.mcp.server.LOOP_GROUP_ORDER`, drops the flag-gated
    recommender, and renders each non-empty group as a heading + purpose line
    + table of ``capability / when to reach / CLI / bare MCP tool / source_uri``.
    """
    grouped = by_loop_group()
    sections: list[str] = []
    for group_name in LOOP_GROUP_ORDER:
        caps = tuple(cap for cap in grouped[group_name] if _advertised(cap))
        if caps:
            sections.append(_render_group(group_name, caps))
    return "\n\n".join(sections)


def _inject_index(template_text: str, index: str) -> str:
    """Splice the rendered index between the template's generated-index markers."""
    start = template_text.find(_BEGIN_MARKER)
    end = template_text.find(_END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise ValueError(
            f"usage-guide template is missing the generated-index markers ({_BEGIN_MARKER!r} … {_END_MARKER!r})"
        )
    before = template_text[: start + len(_BEGIN_MARKER)]
    after = template_text[end:]
    return f"{before}\n\n{index}\n\n{after}"


def render_guide(template_text: str) -> str:
    """Return the full guide: the template with its index block regenerated."""
    return _inject_index(template_text, render_capability_index())


def _data_path(resource: str) -> Path:
    """Return the on-disk path of a bundled usage-guide data file (source tree)."""
    return Path(str(resources.files(_GUIDE_PACKAGE).joinpath(resource)))


def _check(guide_path: Path, rendered: str) -> int:
    """Compare the committed guide to the freshly-rendered one; 0 iff current."""
    current = guide_path.read_text(encoding="utf-8") if guide_path.exists() else ""
    if current == rendered:
        print(f"usage guide is up to date: {guide_path}")
        return 0
    print(
        f"usage guide is STALE: {guide_path}\n"
        "fix: regenerate it — `python -m kairix.agents.usage_guide.generate`\n"
        "next: stage the regenerated guide and re-run the gate\n"
        "run: python -m kairix.agents.usage_guide.generate --check"
    )
    return 1


def _bundled_template_text() -> str:
    """Read the bundled guide template straight from the package data."""
    return resources.files(_GUIDE_PACKAGE).joinpath(_TEMPLATE_RESOURCE).read_text(encoding="utf-8")


def main(
    argv: list[str] | None = None,
    *,
    template_text: str | None = None,
    guide_path: Path | None = None,
) -> int:
    """Regenerate (or ``--check``) the bundled guide from the catalogue.

    ``template_text`` / ``guide_path`` are keyword-only test seams that default
    to the bundled template contents and the bundled guide file. They are
    deliberately NOT command-line flags: the generator only ever reads the one
    bundled template (from package data, never a caller-supplied path) and
    writes the one bundled guide, so the CLI exposes no user-controllable
    filesystem path (an agent running this with faulty arguments cannot escape
    to an arbitrary path).
    """
    parser = argparse.ArgumentParser(
        prog="python -m kairix.agents.usage_guide.generate",
        description="Generate the bundled agent usage guide from CAPABILITIES_CATALOG.",
    )
    parser.add_argument("--check", action="store_true", help="Verify the committed guide is current; do not write.")
    args = parser.parse_args(argv)

    gpath = guide_path or _data_path(_GUIDE_RESOURCE)

    rendered = render_guide(template_text if template_text is not None else _bundled_template_text())

    if args.check:
        return _check(gpath, rendered)

    # Confine the write target to the bundled package data dir (its only
    # production destination) plus the standard agent-CLI roots, so the
    # keyword-only ``guide_path`` seam can never be steered outside a
    # legitimate base. ``confine_to_roots`` resolves + allow-lists the path and
    # returns it, which also clears the pythonsecurity:S2083 write-target taint.
    package_root = Path(str(resources.files(_GUIDE_PACKAGE)))
    safe_gpath = confine_to_roots(gpath, agent_cli_roots(package_root))
    safe_gpath.write_text(rendered, encoding="utf-8")
    print(f"wrote {safe_gpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
