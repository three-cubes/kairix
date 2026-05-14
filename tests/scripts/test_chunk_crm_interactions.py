"""Tests for chunk-crm-interactions.py (TMP-3)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "chunk-crm-interactions.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("chunk_crm_interactions", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]  # importlib Loader protocol omits exec_module in stubs
    return mod


cdi = _load_script()


# ── _extract_date ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_extract_date_iso_utc():
    assert cdi._extract_date("2026-04-06T09:00:00Z") == "2026-04-06"


@pytest.mark.unit
def test_extract_date_iso_offset():
    assert cdi._extract_date("2026-04-06T19:00:00+10:00") == "2026-04-06"


@pytest.mark.unit
def test_extract_date_date_only():
    assert cdi._extract_date("2026-04-06") == "2026-04-06"


@pytest.mark.unit
def test_extract_date_invalid():
    assert cdi._extract_date("not-a-date") is None


@pytest.mark.unit
def test_extract_date_empty():
    assert cdi._extract_date("") is None


# ── parse_crm_export ──────────────────────────────────────────────────────────


@pytest.mark.integration
def test_parse_crm_export_valid(tmp_path):
    data = [{"contact_name": "Alice Smith", "company": "Acme", "interactions": []}]
    p = tmp_path / "export.json"
    p.write_text(json.dumps(data))
    result = cdi.parse_crm_export(p)
    assert result == data


@pytest.mark.integration
def test_parse_crm_export_not_array(tmp_path):
    p = tmp_path / "export.json"
    p.write_text('{"key": "value"}')
    with pytest.raises(ValueError, match="Expected JSON array"):
        cdi.parse_crm_export(p)


@pytest.mark.integration
def test_parse_crm_export_invalid_json(tmp_path):
    p = tmp_path / "export.json"
    p.write_text("not json")
    with pytest.raises(ValueError, match="Invalid JSON"):
        cdi.parse_crm_export(p)


# ── chunk_contact ─────────────────────────────────────────────────────────────

SAMPLE_CONTACT = {
    "contact_id": "abc-123",
    "contact_name": "Bob Jones",
    "company": "TechCorp",
    "interactions": [
        {
            "id": "int-001",
            "event_time": "2026-04-06T09:00:00Z",
            "meeting_type": "meeting",
            "body": "Discussed product roadmap. Follow up on pricing.",
        },
        {
            "id": "int-002",
            "event_time": "2026-03-15T14:00:00Z",
            "meeting_type": "call",
            "body": "Intro call. Aligned on scope.",
        },
    ],
}


@pytest.mark.unit
def test_chunk_contact_produces_one_chunk_per_interaction():
    chunks = cdi.chunk_contact(SAMPLE_CONTACT)
    assert len(chunks) == 2


@pytest.mark.unit
def test_chunk_contact_date_extraction():
    chunks = cdi.chunk_contact(SAMPLE_CONTACT)
    dates = {c["date"] for c in chunks}
    assert "2026-04-06" in dates
    assert "2026-03-15" in dates


@pytest.mark.unit
def test_chunk_contact_meeting_type():
    chunks = cdi.chunk_contact(SAMPLE_CONTACT)
    types = {c["meeting_type"] for c in chunks}
    assert "meeting" in types
    assert "call" in types


@pytest.mark.unit
def test_chunk_contact_skips_missing_event_time():
    contact = {
        "contact_name": "Carol White",
        "company": "Foo",
        "interactions": [
            {"id": "x", "event_time": "", "meeting_type": "meeting", "body": "Hello"},
        ],
    }
    chunks = cdi.chunk_contact(contact)
    assert chunks == []


@pytest.mark.unit
def test_chunk_contact_skips_empty_body():
    contact = {
        "contact_name": "Dave Brown",
        "company": "Bar",
        "interactions": [
            {
                "id": "y",
                "event_time": "2026-04-01T10:00:00Z",
                "meeting_type": "call",
                "body": "",
            },
        ],
    }
    chunks = cdi.chunk_contact(contact)
    assert chunks == []


@pytest.mark.unit
def test_chunk_contact_skips_missing_name():
    contact = {
        "contact_name": "",
        "company": "Baz",
        "interactions": [
            {
                "id": "z",
                "event_time": "2026-04-01T10:00:00Z",
                "meeting_type": "call",
                "body": "Hi",
            },
        ],
    }
    chunks = cdi.chunk_contact(contact)
    assert chunks == []


# ── render_chunk ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_render_chunk_contains_frontmatter():
    chunk = {
        "date": "2026-04-06",
        "contact_name": "Bob Jones",
        "company": "TechCorp",
        "meeting_type": "meeting",
        "body": "Discussed roadmap.",
        "filename": "dex-bob-jones-2026-04-06-int-001.md",
    }
    rendered = cdi.render_chunk(chunk)
    assert "date: 2026-04-06" in rendered
    assert 'contact: "Bob Jones"' in rendered
    assert 'company: "TechCorp"' in rendered
    assert "interaction_type: meeting" in rendered
    assert "type: crm_interaction" in rendered
    assert "source: crm_crm" in rendered


@pytest.mark.unit
def test_render_chunk_has_section_heading():
    chunk = {
        "date": "2026-04-06",
        "contact_name": "Bob Jones",
        "company": "",
        "meeting_type": "call",
        "body": "Quick call.",
        "filename": "dex-bob-jones-2026-04-06-abc.md",
    }
    rendered = cdi.render_chunk(chunk)
    assert "## 2026-04-06 — call" in rendered
    assert "Quick call." in rendered


@pytest.mark.unit
def test_render_chunk_omits_company_line_if_empty():
    chunk = {
        "date": "2026-04-06",
        "contact_name": "Eve Green",
        "company": "",
        "meeting_type": "meeting",
        "body": "Notes here.",
        "filename": "dex-eve-green-2026-04-06-xxx.md",
    }
    rendered = cdi.render_chunk(chunk)
    assert "company:" not in rendered


# ── process_export integration ────────────────────────────────────────────────


@pytest.mark.integration
def test_process_export_writes_files(tmp_path):
    export = [
        {
            "contact_name": "Alice Smith",
            "company": "Acme Corp",
            "interactions": [
                {
                    "id": "aaa-111",
                    "event_time": "2026-04-06T09:00:00Z",
                    "meeting_type": "meeting",
                    "body": "Intro call.",
                },
            ],
        }
    ]
    input_path = tmp_path / "export.json"
    input_path.write_text(json.dumps(export))
    output_dir = tmp_path / "chunks"

    contacts, chunks = cdi.process_export(input_path, output_dir)
    assert contacts == 1
    assert chunks == 1
    files = list(output_dir.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "date: 2026-04-06" in content
    assert 'contact: "Alice Smith"' in content
    assert "Intro call." in content


@pytest.mark.integration
def test_process_export_dry_run_no_files_written(tmp_path):
    export = [
        {
            "contact_name": "Bob Test",
            "company": "Test Co",
            "interactions": [
                {
                    "id": "bbb-222",
                    "event_time": "2026-04-05T10:00:00Z",
                    "meeting_type": "call",
                    "body": "Called.",
                },
            ],
        }
    ]
    input_path = tmp_path / "export.json"
    input_path.write_text(json.dumps(export))
    output_dir = tmp_path / "chunks"

    contacts, chunks = cdi.process_export(input_path, output_dir, dry_run=True)
    assert contacts == 1
    assert chunks == 1
    # dry_run: output_dir should not be created
    assert not output_dir.exists()


@pytest.mark.integration
def test_process_export_multiple_contacts(tmp_path):
    export = [
        {
            "contact_name": "Carol One",
            "company": "CorpA",
            "interactions": [
                {
                    "id": "c1",
                    "event_time": "2026-04-01T08:00:00Z",
                    "meeting_type": "meeting",
                    "body": "Met.",
                },
                {
                    "id": "c2",
                    "event_time": "2026-04-03T10:00:00Z",
                    "meeting_type": "call",
                    "body": "Called.",
                },
            ],
        },
        {
            "contact_name": "Dave Two",
            "company": "CorpB",
            "interactions": [
                {
                    "id": "d1",
                    "event_time": "2026-04-05T09:00:00Z",
                    "meeting_type": "meeting",
                    "body": "Intro.",
                },
            ],
        },
    ]
    input_path = tmp_path / "export.json"
    input_path.write_text(json.dumps(export))
    output_dir = tmp_path / "chunks"

    contacts, chunks = cdi.process_export(input_path, output_dir)
    assert contacts == 2
    assert chunks == 3
    files = list(output_dir.glob("*.md"))
    assert len(files) == 3
