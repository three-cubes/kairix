"""Unit tests for the usage-guide generator (PLA-321 / W4).

The bundled agent usage guide's capability index is generated from
``CAPABILITIES_CATALOG`` so it can't drift from the tool registry. These
tests pin the generator's PUBLIC contract (``render_capability_index`` /
``render_guide`` / ``main``):

  * the six loop-ordered groups render in ``LOOP_GROUP_ORDER``;
  * every advertised capability appears with its **bare** wire name (no
    ``tool_`` / ``mcp-kairix__`` prefix) — the #694 tool-name-accuracy fix;
  * the flag-gated recommender is excluded (mirrors ``agent_facing``);
  * ``render_guide`` splices the index into the template between its markers;
  * ``--check`` is a currency guard: green iff the committed guide is current.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from kairix.agents.mcp.server import (
    CAPABILITIES_CATALOG,
    LOOP_GROUP_ORDER,
    RECOMMEND_CAPABILITIES_TOOL_NAME,
    agent_facing,
)
from kairix.agents.usage_guide import generate

pytestmark = pytest.mark.unit


def _real_template() -> str:
    """Read the shipped guide template (package data) — the production input."""
    return (
        resources.files("kairix.agents.usage_guide")
        .joinpath("data/agent-usage-guide.md.tmpl")
        .read_text(encoding="utf-8")
    )


def _index_row_for(index: str, capability: str) -> str:
    """Return the single index table row that names the given capability."""
    prefix = f"| `{capability}` |"
    rows = [line for line in index.splitlines() if line.startswith(prefix)]
    assert len(rows) == 1, f"expected exactly one row for {capability!r}; got {rows}"
    return rows[0]


# ---------------------------------------------------------------------------
# render_capability_index — structure + bare names
# ---------------------------------------------------------------------------


def test_index_renders_the_six_groups_in_loop_order() -> None:
    index = generate.render_capability_index()
    headings = [line[4:] for line in index.splitlines() if line.startswith("### ")]
    assert headings == list(LOOP_GROUP_ORDER), f"expected loop-ordered groups; got {headings}"


def test_index_uses_bare_mcp_names_not_tool_prefix() -> None:
    """The MCP tool column carries the bare wire name — the #694 fix.

    Sabotage: revert ``_mcp_cell`` to ``f"tool_{cap.mcp_tool}"`` and this
    assertion trips on the reintroduced prefix.
    """
    index = generate.render_capability_index()
    assert "`search`" in index
    assert "tool_search" not in index
    assert "mcp-kairix__" not in index


def test_index_covers_every_agent_facing_capability() -> None:
    """Every agent-callable capability's bare wire name is discoverable."""
    index = generate.render_capability_index()
    for cap in agent_facing():
        assert cap.mcp_tool is not None
        assert f"`{cap.mcp_tool}`" in index, f"{cap.name} missing its bare MCP name"


def test_index_excludes_the_flag_gated_recommender() -> None:
    """The recommender defaults OFF, so it must not read as a live capability."""
    index = generate.render_capability_index()
    assert RECOMMEND_CAPABILITIES_TOOL_NAME not in index
    assert "kairix recommend" not in index


def test_escalation_rows_render_the_operator_envelope() -> None:
    """Operator-only rows show they escalate rather than a callable wire name."""
    index = generate.render_capability_index()
    escalate_caps = [c for c in CAPABILITIES_CATALOG if c.escalate_via is not None]
    assert escalate_caps, "expected at least one escalation capability"
    for cap in escalate_caps:
        row = _index_row_for(index, cap.name)
        assert f"escalate: `{cap.escalate_via}`" in row, f"{cap.name} missing its escalation cell"


def test_source_uri_column_marks_retrieval_and_synthesis() -> None:
    """`search` (retrieval) carries a source_uri; `warm` (diagnostic) does not."""
    index = generate.render_capability_index()
    assert _index_row_for(index, "search").endswith("| yes |")
    assert _index_row_for(index, "warm").endswith("| — |")


def test_row_without_trigger_reads_the_group_note_fallback() -> None:
    """A capability with no ``when_to_use`` reads the fallback, not a blank cell."""
    index = generate.render_capability_index()
    assert "see the group note above" in _index_row_for(index, "bootstrap")


# ---------------------------------------------------------------------------
# render_guide / marker splicing (via the public surface)
# ---------------------------------------------------------------------------


def test_render_guide_splices_index_into_the_template() -> None:
    rendered = generate.render_guide(_real_template())
    for group in LOOP_GROUP_ORDER:
        assert f"### {group}" in rendered
    # The template's placeholder line is replaced by the generated block.
    assert "(generated capability index" not in rendered


def test_render_guide_rejects_a_template_without_markers() -> None:
    with pytest.raises(ValueError, match="generated-index markers"):
        generate.render_guide("# Guide with no generated markers\n")


# ---------------------------------------------------------------------------
# main — write + --check currency guard
# ---------------------------------------------------------------------------


def test_main_writes_the_rendered_guide(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "guide.md"

    rc = generate.main([], template_text=_real_template(), guide_path=out)

    assert rc == 0
    assert out.read_text(encoding="utf-8") == generate.render_guide(_real_template())
    assert "wrote" in capsys.readouterr().out


def test_main_check_is_green_when_guide_is_current(tmp_path: Path) -> None:
    out = tmp_path / "guide.md"
    generate.main([], template_text=_real_template(), guide_path=out)

    rc = generate.main(["--check"], template_text=_real_template(), guide_path=out)
    assert rc == 0


def test_main_check_fires_when_guide_is_stale(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "guide.md"
    out.write_text("stale — not regenerated\n", encoding="utf-8")

    rc = generate.main(["--check"], template_text=_real_template(), guide_path=out)
    assert rc == 1
    assert "STALE" in capsys.readouterr().out


def test_main_check_fires_when_guide_is_absent(tmp_path: Path) -> None:
    rc = generate.main(["--check"], template_text=_real_template(), guide_path=tmp_path / "missing.md")
    assert rc == 1


def test_main_accepts_a_write_target_inside_the_allowed_roots(tmp_path: Path) -> None:
    """A ``guide_path`` under an allowed root (pytest's tmp dir) is written.

    Complements the reject case below — proves the S2083 confinement does not
    regress the legitimate keyword-only test seam.
    """
    out = tmp_path / "accepted-guide.md"

    rc = generate.main([], template_text=_real_template(), guide_path=out)

    assert rc == 0
    assert out.read_text(encoding="utf-8") == generate.render_guide(_real_template())


def test_main_rejects_a_write_target_outside_the_allowed_roots(tmp_path: Path) -> None:
    """A ``guide_path`` that escapes every allowed root raises before writing.

    Sabotage: drop the ``confine_to_roots`` guard in ``main`` and this write
    escapes to ``/etc`` (pythonsecurity:S2083). The escape target is not under
    the package dir, cwd, home, or the temp dir, so confinement rejects it.
    """
    from kairix.paths import PathTraversalError

    escape = Path("/etc") / "kairix-usage-guide-escape" / "guide.md"

    with pytest.raises(PathTraversalError):
        generate.main([], template_text=_real_template(), guide_path=escape)

    assert not escape.exists()


def test_main_rejects_a_dot_dot_traversal_out_of_the_allowed_roots(tmp_path: Path) -> None:
    """A ``guide_path`` that climbs out with ``..`` segments is rejected.

    The path is spelled under ``tmp_path`` but its ``..`` components resolve
    to ``/etc``; confinement resolves before checking, so the traversal is
    caught and nothing is written. Sabotage: drop the ``confine_to_roots``
    guard in ``main`` and the write is attempted at the escaped location.
    """
    from kairix.paths import PathTraversalError

    climb = "../" * (len(tmp_path.resolve().parts) + 2)
    escape = tmp_path / f"{climb}etc/kairix-usage-guide-escape/guide.md"
    assert escape.resolve() == Path("/etc/kairix-usage-guide-escape/guide.md").resolve()

    with pytest.raises(PathTraversalError):
        generate.main([], template_text=_real_template(), guide_path=escape)

    assert not escape.resolve().exists()


def test_committed_bundled_guide_is_current() -> None:
    """The shipped guide equals a fresh render from the catalogue + template.

    This is the drift guard: if the catalogue changes and the guide is not
    regenerated, ``--check`` (rc 1) fails here before the stale guide ships.
    Sabotage: edit ``data/agent-usage-guide.md`` by hand and this fails.
    """
    assert generate.main(["--check"]) == 0
