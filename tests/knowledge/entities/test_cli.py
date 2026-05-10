"""Unit tests for ``kairix.knowledge.entities.cli`` adapter shells + pure helpers.

The CLI body is a thin adapter — argv parsing + run_entity_suggest /
run_entity_validate + stdout formatting. Logic belongs to the use
cases (covered in ``tests/use_cases/test_entity.py``). These tests
drive each pure helper directly and the cmd_* orchestrators with a
deps injection.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

import pytest

from kairix.knowledge.entities.cli import (
    cmd_get,
    cmd_suggest,
    cmd_validate,
    format_get_output,
    format_suggest_output,
    format_validate_table,
)
from kairix.use_cases.entity import (
    EntitySuggestDeps,
    EntitySuggestOutput,
    EntityValidateDeps,
    EntityValidateMatch,
    EntityValidateOutput,
    SuggestedEntityHit,
)
from kairix.use_cases.entity_get import EntityGetDeps, EntityGetOutput

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Pure formatters — format_suggest_output, format_validate_table, _validate_envelope
# ---------------------------------------------------------------------------


def test_format_suggest_output_table_lists_each_hit() -> None:
    out = EntitySuggestOutput(
        text="t",
        suggestions=[
            SuggestedEntityHit(text="Acme", label="ORG", is_new=False, existing_id="acme", existing_name="Acme"),
            SuggestedEntityHit(text="Bob", label="PERSON", is_new=True),
        ],
        new_count=1,
        existing_count=1,
    )
    rendered = format_suggest_output(out, fmt="table")
    assert "Acme" in rendered
    assert "Bob" in rendered
    assert "Total: 2 entities found (1 new, 1 existing)" in rendered


def test_format_suggest_output_jsonl_one_per_line() -> None:
    out = EntitySuggestOutput(
        text="t",
        suggestions=[
            SuggestedEntityHit(text="X", label="ORG", is_new=True),
            SuggestedEntityHit(text="Y", label="PERSON", is_new=False, existing_id="y"),
        ],
    )
    rendered = format_suggest_output(out, fmt="jsonl")
    lines = rendered.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["text"] == "X"
    assert json.loads(lines[1])["existing_id"] == "y"


def test_format_suggest_output_empty_table_still_shows_total() -> None:
    out = EntitySuggestOutput(text="t", suggestions=[], new_count=0, existing_count=0)
    rendered = format_suggest_output(out, fmt="table")
    assert "Total: 0 entities found (0 new, 0 existing)" in rendered


def test_format_validate_table_renders_neo4j_id_and_matches() -> None:
    out = EntityValidateOutput(
        name="Acme",
        neo4j_id="acme",
        matches=[
            EntityValidateMatch(
                qid="Q1",
                label="Acme Inc",
                description="A supplier of road-runner traps",
                url="http://wiki/Q1",
                confidence="high",
            ),
        ],
        updated=False,
    )
    rendered = format_validate_table(out, with_update_hint=True)
    assert "Entity: Acme" in rendered
    assert "Neo4j id: acme" in rendered
    assert "Q1" in rendered
    assert "high" in rendered
    assert "http://wiki/Q1" in rendered
    assert "Run with --update" in rendered  # hint shown for high-confidence match


def test_format_validate_table_no_matches_message() -> None:
    out = EntityValidateOutput(name="Bogus", matches=[])
    rendered = format_validate_table(out, with_update_hint=True)
    assert "Entity: Bogus" in rendered
    assert "Neo4j id: (not found)" in rendered
    assert "No Wikidata matches found." in rendered
    # Hint suppressed when no matches
    assert "Run with --update" not in rendered


def test_format_validate_table_updated_marker() -> None:
    out = EntityValidateOutput(
        name="Acme",
        neo4j_id="acme",
        matches=[EntityValidateMatch(qid="Q1", label="Acme", description="d", url="u", confidence="high")],
        updated=True,
    )
    rendered = format_validate_table(out, with_update_hint=False)
    assert "Updated: wikidata_qid written to Neo4j node" in rendered
    assert "Run with --update" not in rendered


def test_format_validate_table_low_confidence_suppresses_hint() -> None:
    out = EntityValidateOutput(
        name="X",
        neo4j_id="x",
        matches=[EntityValidateMatch(qid="Q1", label="x", description="d", url="u", confidence="low")],
    )
    rendered = format_validate_table(out, with_update_hint=True)
    assert "Run with --update" not in rendered


# --json envelope shape is exercised through cmd_validate's --format json path
# in test_cmd_validate_json_format_emits_envelope below.


# ---------------------------------------------------------------------------
# cmd_suggest orchestrator — driven through EntitySuggestDeps.
# ---------------------------------------------------------------------------


class _FakeNeo4j:
    available = True


def _capture(fn: Any) -> tuple[int, str, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    rc = 0
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = int(fn())
    except SystemExit as e:  # NOSONAR — CLI test captures exit code; reraising would defeat the test
        rc = int(e.code) if e.code is not None else 0
    return rc, out_buf.getvalue(), err_buf.getvalue()


def test_cmd_suggest_happy_path_prints_table_and_returns_zero() -> None:
    args = argparse.Namespace(text="Acme is a client.", file=None, format="table")
    deps = EntitySuggestDeps(
        suggest_fn=lambda text, neo4j: [
            type(
                "S",
                (),
                {
                    "text": "Acme",
                    "label": "ORG",
                    "is_new": True,
                    "existing_id": None,
                    "existing_name": None,
                    "context": "",
                },
            )()
        ],
        neo4j_client_fn=lambda: _FakeNeo4j(),
    )
    rc, stdout, _ = _capture(lambda: cmd_suggest(args, deps=deps))
    assert rc == 0
    assert "Acme" in stdout
    assert "1 new, 0 existing" in stdout


def test_cmd_suggest_jsonl_format() -> None:
    args = argparse.Namespace(text="X", file=None, format="jsonl")
    deps = EntitySuggestDeps(
        suggest_fn=lambda text, neo4j: [
            type(
                "S",
                (),
                {
                    "text": "X",
                    "label": "ORG",
                    "is_new": True,
                    "existing_id": None,
                    "existing_name": None,
                    "context": "",
                },
            )()
        ],
        neo4j_client_fn=lambda: _FakeNeo4j(),
    )
    rc, stdout, _ = _capture(lambda: cmd_suggest(args, deps=deps))
    assert rc == 0
    assert json.loads(stdout.strip())["text"] == "X"


def test_cmd_suggest_use_case_error_exits_nonzero() -> None:
    args = argparse.Namespace(text="X", file=None, format="table")

    def _boom(text: str, neo4j: Any) -> list:
        raise ImportError("spaCy missing")

    deps = EntitySuggestDeps(suggest_fn=_boom, neo4j_client_fn=lambda: _FakeNeo4j())
    rc, _stdout, stderr = _capture(lambda: cmd_suggest(args, deps=deps))
    assert rc == 1
    assert "ERROR" in stderr
    assert "kairix[nlp]" in stderr


def test_cmd_suggest_unreadable_file_exits_with_message(tmp_path: Any) -> None:
    args = argparse.Namespace(text="", file=str(tmp_path / "nope.txt"), format="table")
    deps = EntitySuggestDeps()
    rc, _stdout, stderr = _capture(lambda: cmd_suggest(args, deps=deps))
    assert rc == 1
    assert "ERROR" in stderr


def test_cmd_suggest_reads_from_file(tmp_path: Any) -> None:
    p = tmp_path / "prose.md"
    p.write_text("Acme is a client", encoding="utf-8")
    args = argparse.Namespace(text="", file=str(p), format="table")

    captured: dict = {}

    def _capture_text(text: str, neo4j: Any) -> list:
        captured["text"] = text
        return []

    deps = EntitySuggestDeps(suggest_fn=_capture_text, neo4j_client_fn=lambda: _FakeNeo4j())
    rc, _stdout, _ = _capture(lambda: cmd_suggest(args, deps=deps))
    assert rc == 0
    assert captured["text"] == "Acme is a client"


# ---------------------------------------------------------------------------
# cmd_validate orchestrator — driven through EntityValidateDeps.
# ---------------------------------------------------------------------------


def test_cmd_validate_table_format_with_match_returns_zero() -> None:
    args = argparse.Namespace(name="Acme", update=False, format="table")
    deps = EntityValidateDeps(
        validate_fn=lambda name, neo4j, update: {
            "name": name,
            "neo4j_id": "acme",
            "matches": [{"qid": "Q1", "label": "Acme", "description": "x", "url": "u", "confidence": "high"}],
            "updated": False,
        },
        neo4j_client_fn=lambda: _FakeNeo4j(),
    )
    rc, stdout, _ = _capture(lambda: cmd_validate(args, deps=deps))
    assert rc == 0
    assert "Q1" in stdout


def test_cmd_validate_table_no_matches_returns_one() -> None:
    args = argparse.Namespace(name="X", update=False, format="table")
    deps = EntityValidateDeps(
        validate_fn=lambda name, neo4j, update: {"name": name, "matches": [], "updated": False},
        neo4j_client_fn=lambda: _FakeNeo4j(),
    )
    rc, stdout, _ = _capture(lambda: cmd_validate(args, deps=deps))
    assert rc == 1
    assert "No Wikidata matches" in stdout


def test_cmd_validate_json_format_emits_envelope() -> None:
    args = argparse.Namespace(name="Acme", update=False, format="json")
    deps = EntityValidateDeps(
        validate_fn=lambda name, neo4j, update: {
            "name": name,
            "neo4j_id": "acme",
            "matches": [{"qid": "Q1", "label": "Acme", "description": "x", "url": "u", "confidence": "high"}],
            "updated": False,
        },
        neo4j_client_fn=lambda: _FakeNeo4j(),
    )
    rc, stdout, _ = _capture(lambda: cmd_validate(args, deps=deps))
    assert rc == 0
    payload = json.loads(stdout)
    assert payload["name"] == "Acme"
    assert payload["matches"][0]["qid"] == "Q1"


def test_cmd_validate_use_case_error_exits_one() -> None:
    args = argparse.Namespace(name="X", update=False, format="table")

    def _boom(name: str, neo4j: Any, update: bool) -> dict:
        raise ConnectionError("KAIRIX_NEO4J_URI not reachable")

    deps = EntityValidateDeps(validate_fn=_boom, neo4j_client_fn=lambda: _FakeNeo4j())
    rc, _stdout, stderr = _capture(lambda: cmd_validate(args, deps=deps))
    assert rc == 1
    assert "Validation failed" in stderr


# ---------------------------------------------------------------------------
# cmd_get — Phase 3e
# ---------------------------------------------------------------------------


def test_format_get_output_renders_entity_with_summary() -> None:
    out = EntityGetOutput(
        id="acme", name="Acme", type="Organisation", summary="supplier — Tier A", vault_path="/Acme.md"
    )
    text = format_get_output(out)
    assert "Entity:     Acme" in text
    assert "Type:       Organisation" in text
    assert "Neo4j id:   acme" in text
    assert "Vault path: /Acme.md" in text
    assert "supplier — Tier A" in text


def test_format_get_output_renders_unknown_for_empty_fields() -> None:
    out = EntityGetOutput(name="X")
    text = format_get_output(out)
    assert "Type:       (unknown)" in text
    assert "Neo4j id:   (none)" in text
    assert "Vault path: (none)" in text


def test_format_get_output_short_circuits_on_error() -> None:
    out = EntityGetOutput(name="X", error="Entity not found: X")
    assert format_get_output(out).startswith("error:")


def test_cmd_get_table_format_returns_zero_on_match() -> None:
    args = argparse.Namespace(name="Acme", format="table")
    deps = EntityGetDeps(
        fetch_fn=lambda name: {
            "id": "acme",
            "name": name,
            "type": "Organisation",
            "summary": "supplier",
            "vault_path": "/Acme.md",
        }
    )
    rc, stdout, _ = _capture(lambda: cmd_get(args, deps=deps))
    assert rc == 0
    assert "Acme" in stdout
    assert "Organisation" in stdout


def test_cmd_get_returns_one_on_not_found() -> None:
    args = argparse.Namespace(name="Bogus", format="table")
    deps = EntityGetDeps(fetch_fn=lambda name: None)
    rc, stdout, _ = _capture(lambda: cmd_get(args, deps=deps))
    assert rc == 1
    assert "Entity not found" in stdout


def test_cmd_get_json_format_emits_envelope() -> None:
    args = argparse.Namespace(name="Acme", format="json")
    deps = EntityGetDeps(
        fetch_fn=lambda name: {
            "id": "acme",
            "name": name,
            "type": "Organisation",
            "summary": "s",
            "vault_path": "/p",
        }
    )
    rc, stdout, _ = _capture(lambda: cmd_get(args, deps=deps))
    assert rc == 0
    payload = json.loads(stdout)
    assert payload["id"] == "acme"
    assert payload["name"] == "Acme"
