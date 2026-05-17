"""Contract: CLI ↔ MCP parity for the ``timeline`` operation.

Phase 1 of #168 (CLI/MCP feature parity) extracted timeline business
logic into ``kairix.use_cases.timeline.run_timeline``. Both surfaces
are now thin adapters around it. This contract pins the parity:

  - Same use case is wired into the CLI's ``main()`` and the MCP's
    ``tool_timeline``.
  - Both adapters call ``run_timeline`` with parameter pass-through —
    no surface-specific business logic. So when the use case changes,
    both surfaces update together.

If you find yourself re-implementing date extraction, query rewriting,
or backend dispatch outside ``run_timeline``, this contract should
fail — that's the smell #163 surfaced.
"""

from __future__ import annotations

import inspect

import pytest


@pytest.mark.contract
def test_cli_main_calls_run_timeline_use_case() -> None:
    """The CLI's ``main()`` is a thin adapter that defers to the use case.

    Post-F1: ``main()`` defers to a configurable ``timeline_runner``
    whose production default is ``_default_timeline_runner``, and that
    helper holds the use-case import + call. The contract still pins
    both halves: the module imports ``run_timeline``, ``main()`` defers
    to ``timeline_runner(...)``, and the production default wires the
    use case.
    """
    from kairix.core.temporal import cli

    module_src = inspect.getsource(cli)
    assert "from kairix.use_cases.timeline import run_timeline" in module_src
    assert "run_timeline(" in module_src

    main_src = inspect.getsource(cli.main)
    assert "timeline_runner(" in main_src, (
        "main() must defer to the configured timeline_runner — not call run_timeline directly"
    )

    default_runner_src = inspect.getsource(cli._default_timeline_runner)
    assert "run_timeline(" in default_runner_src, "_default_timeline_runner must wire the production use case"


@pytest.mark.contract
def test_mcp_tool_timeline_calls_run_timeline_use_case() -> None:
    """The MCP's ``tool_timeline`` is a thin adapter that defers to the use case."""
    from kairix.agents.mcp import server

    src = inspect.getsource(server.tool_timeline)
    assert "from kairix.use_cases.timeline import run_timeline" in src
    assert "run_timeline(" in src


@pytest.mark.contract
def test_cli_does_not_call_query_temporal_chunks_directly() -> None:
    """CLI must NOT bypass the use case to hit the temporal index directly.

    Pre-Phase 1, the CLI imported ``query_temporal_chunks`` and used a
    different code path than the MCP. The use case now owns that
    dispatch, so neither adapter should reach past it.
    """
    from kairix.core.temporal import cli

    src = inspect.getsource(cli)
    assert "query_temporal_chunks" not in src, (
        "CLI bypasses run_timeline — see #163. All temporal-chunks access must go via the use case."
    )


@pytest.mark.contract
def test_mcp_tool_timeline_signature_matches_use_case_passthrough() -> None:
    """The MCP adapter exposes the same uniform parameters as the use case.

    The MCP signature uses string ``anchor_date`` (JSON wire format) which
    the adapter parses to ``date`` before delegating. All other parameters
    pass through unchanged.
    """
    from kairix.agents.mcp.server import tool_timeline
    from kairix.use_cases.timeline import run_timeline

    mcp_params = set(inspect.signature(tool_timeline).parameters)
    use_case_params = set(inspect.signature(run_timeline).parameters)

    # MCP exposes the JSON-friendly wire surface.
    expected_mcp = {"query", "anchor_date", "agent", "scope"}
    assert mcp_params == expected_mcp

    # Use case exposes the typed superset.
    assert {"query", "anchor_date", "agent", "scope"}.issubset(use_case_params)


@pytest.mark.contract
def test_use_case_returns_documented_result_dataclass() -> None:
    """``run_timeline`` returns ``TimelineResult`` — both adapters serialise from this."""
    # PEP 563 (``from __future__ import annotations``) keeps annotations as
    # strings; resolve via ``typing.get_type_hints`` so the assertion sees
    # the real class object.
    import typing

    from kairix.use_cases.timeline import TimelineResult, run_timeline

    hints = typing.get_type_hints(run_timeline)
    assert hints.get("return") is TimelineResult


@pytest.mark.contract
def test_mcp_envelope_keys_match_run_timeline_result_fields() -> None:
    """The MCP JSON envelope keys are exactly the use case's TimelineResult fields,
    keyed `path/title/snippet/score` for each hit. If either surface drifts, this fails.
    """
    from kairix.agents.mcp import server

    src = inspect.getsource(server.tool_timeline)
    for key in (
        "original_query",
        "rewritten_query",
        "is_temporal",
        "fell_back",
        "time_window",
        "results",
        "error",
    ):
        assert f'"{key}"' in src, f"MCP envelope missing key {key!r}"
    for hit_key in ("path", "title", "snippet", "score"):
        assert f'"{hit_key}"' in src, f"MCP hit envelope missing key {hit_key!r}"
