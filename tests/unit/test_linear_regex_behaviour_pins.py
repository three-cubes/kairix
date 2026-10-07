"""Behaviour pins for the regexes rewritten to run in linear time (PLA-472).

SonarCloud ``python:S8786`` flagged 23 regexes whose overlapping adjacent
quantifiers (``\\s+(.+)``, ``\\s*\\??\\s*``, ``[A-Z_]+(.*)`` …) let the engine
backtrack super-linearly. Each was rewritten so consecutive quantifiers match
disjoint character sets. These tests pin the observable behaviour of every
rewritten pattern through its PUBLIC caller — captured from the original
regexes before the rewrite — so the rewrite is proven behaviour-preserving.

Pinning exposed a handful of genuine bugs in the original grammar, which were
fixed alongside the rewrite; the tests marked "BUG FIX" pin the corrected
behaviour (each failed against the original pattern):

* heading scanners let ``\\s+`` run across a newline, so an empty heading
  (``"# "``) took the NEXT line as its title (``extract_title``,
  ``split_at_headings``, ``extract_from_headings``);
* the sheet / slide header lines did the same — an unnamed sheet swallowed
  its table header (losing every row) and an untitled slide took its first
  body line as the title;
* the markdown chunker stripped a trailing ``#`` that was part of the title
  (``## C#`` → ``C``) — CommonMark only treats a ``#`` run as a closing
  sequence when a space precedes it;
* the bootstrap resolver cut a vault path at its FIRST ``(`` instead of
  dropping only the trailing ``(note)``;
* the inline-HTML stripper matched tag-name PREFIXES (``<b`` stripped
  ``<blockquote>``, ``<u`` stripped ``<ul>``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.chunkers.docx_heading import make_chunker as make_docx_chunker
from kairix.chunkers.email_thread import make_chunker as make_email_chunker
from kairix.chunkers.markdown_structural import make_chunker as make_markdown_chunker
from kairix.chunkers.sheet_row import make_chunker as make_sheet_chunker
from kairix.chunkers.slide import make_chunker as make_slide_chunker
from kairix.core.search.intent import QueryIntent, classify
from kairix.core.temporal.chunker import chunk_board, chunk_memory_log
from kairix.knowledge.entities.overrides import load_entity_overrides
from kairix.knowledge.reflib.extract import RawEntity, RawRelationship, extract_from_headings
from kairix.knowledge.reflib.frontmatter import extract_existing_frontmatter
from kairix.knowledge.reflib.markdown import strip_html_tags
from kairix.knowledge.reflib.splitter import split_at_headings
from kairix.knowledge.store.crawler import parse_frontmatter
from kairix.knowledge.wikilinks.resolver import load_entities_from_bootstrap
from kairix.text import extract_title, strip_frontmatter

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# email_thread chunker — reply preamble + header line
# ---------------------------------------------------------------------------

_EMAIL_PREFIX = "Subject: s\n\nKeep me.\n"
_EMAIL_KEPT = "Subject: s\n\nKeep me."


@pytest.mark.parametrize(
    ("preamble", "cuts"),
    [
        ("On Mon, Jan 1, 2026 at 9:00 AM, agent-beta wrote:", True),
        ("on tuesday agent-beta WROTE:", True),  # case-insensitive
        ("On\tx\twrote:", True),  # any whitespace separator
        ("On   wrote:", True),  # three spaces satisfy ``\s+.+\s+``
        ("On  wrote:", False),  # two spaces do not
        ("Only agent-beta wrote:", False),
        ("On x wrote: more", False),
        ("On x wrote", False),
    ],
)
def test_email_reply_preamble_cut(preamble: str, cuts: bool) -> None:
    text = f"{_EMAIL_PREFIX}{preamble}\nOlder quoted text.\n"
    chunks = make_email_chunker().chunk(text=text, section_kind="text", source_uri="m")
    expected = _EMAIL_KEPT if cuts else f"{_EMAIL_KEPT}\n{preamble}\nOlder quoted text."
    assert [c.text for c in chunks] == [expected]


def test_email_header_values_are_trimmed_and_unspaced_names_end_the_block() -> None:
    chunker = make_email_chunker()
    text = "Subject:   Spaced value  \nFrom:agent-alpha@example.com\nX-Custom_1: v\nTo: \nDate: 2026-01-01\n\nBody.\n"
    chunks = chunker.chunk(text=text, section_kind="text", source_uri="m")
    assert [c.text for c in chunks] == [
        "Subject: Spaced value\nFrom: agent-alpha@example.com\nDate: 2026-01-01\n\nBody."
    ]

    aborted = chunker.chunk(text="Subject: s\nBad Header: v\nrest\n", section_kind="text", source_uri="m")
    assert [c.text for c in aborted] == ["Subject: s\n\nBad Header: v\nrest"]


# ---------------------------------------------------------------------------
# markdown_structural + docx_heading chunkers — ATX heading lines
# ---------------------------------------------------------------------------


def test_markdown_heading_titles_strip_closing_hashes() -> None:
    text = (
        "# Title #\n\nintro\n\n## C#\n\nbody a\n\n### Closing  ###  \n\nbody b\n\n"
        "####### seven\n\n#NoSpace\n\n##  \n\nend\n"
    )
    chunks = make_markdown_chunker().chunk(text=text, section_kind="text", source_uri="m")
    assert [(c.metadata["heading_path"], c.text) for c in chunks] == [
        ("Title", "# Title\n\nintro"),
        # BUG FIX: ``C#`` keeps its ``#`` — no space before it, so it is not a
        # closing sequence (the original grammar produced ``C``).
        ("Title > C#", "# Title > C#\n\nbody a"),
        (
            "Title > C# > Closing",
            "# Title > C# > Closing\n\nbody b\n\n####### seven\n\n#NoSpace\n\n##  \n\nend",
        ),
    ]


@pytest.mark.parametrize(
    ("heading", "heading_path"),
    [
        ("#### # #", "Root > #"),
        ("#### Foo  #  #", "Root > Foo  #"),
        ("#### Foo\t##", "Root > Foo"),
        ("####   Spaced   out   ", "Root > Spaced   out"),
        ("#### x", "Root > x"),
        # BUG FIX: a ``#`` run with no space before it is part of the title
        # (the original grammar stripped it: ``a###`` → ``a``, ``F#`` → ``F``).
        ("#### a###", "Root > a###"),
        ("#### F#", "Root > F#"),
        # BUG FIX: an all-``#`` heading is an EMPTY heading (CommonMark), so it
        # opens no section (the original grammar invented the title ``#``).
        ("#### ###", "Root"),
    ],
)
def test_markdown_heading_title_edge_cases(heading: str, heading_path: str) -> None:
    body = "z" * 40
    text = f"# Root\n\n{body}\n\n{heading}\n\n{body}\n"
    chunks = make_markdown_chunker().chunk(text=text, section_kind="text", source_uri="m")
    assert chunks[-1].metadata["heading_path"] == heading_path


def test_docx_heading_sections_and_non_headings() -> None:
    text = "# Top\n\npara\n\n##   Second   \n\npara2\n\n####### seven\n\n#nospace\n\n### \n\ntail\n"
    chunks = make_docx_chunker().chunk(text=text, section_kind="text", source_uri="d")
    assert [(c.metadata["section_path"], c.text) for c in chunks] == [
        ("Top", "[Section: Top]\n\n# Top\n\npara"),
        (
            "Top > Second",
            "[Section: Top > Second]\n\n##   Second\n\npara2\n\n####### seven\n\n#nospace\n\n###\n\ntail",
        ),
    ]


# ---------------------------------------------------------------------------
# sheet_row + slide chunkers — extractor header lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "sheet_names"),
    [
        ("## Sheet:   Budget 2026  \n\n| a | b |\n|---|---|\n| 1 | 2 |\n", ["Budget 2026"]),
        ("## Sheet:Q1\n| a |\n|---|\n| 1 |\n", ["Q1"]),
        ("| a |\n|---|\n| 1 |\n", [""]),
        # BUG FIX: an unnamed sheet keeps its table. The original ``\s*`` ran
        # across the newline, took the table header as the sheet name and
        # dropped every row.
        ("## Sheet:\n\n| a |\n|---|\n| 1 |\n", [""]),
        ("## Sheet:   \n| a |\n|---|\n| 1 |\n| 2 |\n", [""]),
        ("## Sheet:   \n", []),
        ("## Sheet:", []),
    ],
)
def test_sheet_header_name(text: str, sheet_names: list[str]) -> None:
    chunks = make_sheet_chunker().chunk(text=text, section_kind="table", source_uri="s")
    assert [c.metadata["sheet_name"] for c in chunks] == sheet_names


@pytest.mark.parametrize(
    ("text", "slides"),
    [
        (
            "## Slide 1: Intro  \nhello\n## Slide 2:Plan\nbody\n",
            [("1", "Intro", "## Slide 1: Intro  \nhello"), ("2", "Plan", "## Slide 2:Plan\nbody")],
        ),
        (
            # BUG FIX: an untitled slide has an empty title — the original
            # ``\s*`` ran across the newline and took the first body line.
            "## Slide 3:\nfirst body line\n## Slide 4: Next\nx\n",
            [("3", "", "## Slide 3:\nfirst body line"), ("4", "Next", "## Slide 4: Next\nx")],
        ),
        (
            # BUG FIX: ...and so swallowed the NEXT slide's header whole.
            "## Slide 6:\n\n## Slide 7: Seven\nbody\n",
            [("6", "", "## Slide 6:"), ("7", "Seven", "## Slide 7: Seven\nbody")],
        ),
        ("## Slide 5:   ", [("5", "", "## Slide 5:")]),
        ("no header text", [("1", "", "no header text")]),
    ],
)
def test_slide_header_number_and_title(text: str, slides: list[tuple[str, str, str]]) -> None:
    chunks = make_slide_chunker().chunk(text=text, section_kind="slide", source_uri="p")
    assert [(c.metadata["slide_number"], c.metadata["slide_title"], c.text) for c in chunks] == slides


# ---------------------------------------------------------------------------
# intent classifier — attribute-fact shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "intent"),
    [
        ("address of Acme?", QueryIntent.ATTRIBUTE_FACT),
        ("address of Acme ?  ", QueryIntent.ATTRIBUTE_FACT),
        ("home town of Acme", QueryIntent.ATTRIBUTE_FACT),
        ("the home town of Acme?", QueryIntent.ATTRIBUTE_FACT),
        ("address of acme??", QueryIntent.SEMANTIC),
        ("Acme's address?", QueryIntent.ATTRIBUTE_FACT),
        ("Acme's address ? ", QueryIntent.ATTRIBUTE_FACT),
        ("Acme's address??", QueryIntent.KEYWORD),
        ("what did agent-alpha research?", QueryIntent.ATTRIBUTE_FACT),
        ("  What did agent-alpha research ?  ", QueryIntent.ATTRIBUTE_FACT),
        ("what did agent-alpha research??", QueryIntent.SEMANTIC),
        ("what did agent-alpha research today?", QueryIntent.TEMPORAL),
    ],
)
def test_attribute_fact_trailing_question_mark(query: str, intent: QueryIntent) -> None:
    assert classify(query) is intent


# ---------------------------------------------------------------------------
# wikilinks resolver — bootstrap table rows + trailing path notes
# ---------------------------------------------------------------------------


def test_bootstrap_rows_trim_cells_and_drop_trailing_notes(tmp_path: Path) -> None:
    index = tmp_path / "bootstrap.md"
    index.write_text(
        "## Clients\n"
        "| Entity | Link | Path |\n"
        "|---|---|---|\n"
        "| Acme-Corp | `[[Acme-Corp]]` | `02-Areas/Clients/Acme-Corp/` |\n"
        "|   Gamma Systems   | `[[Gamma-Systems\\|Gamma Systems]]` |  `02-Areas/Clients/Gamma-Systems/ (legacy)`  |\n"
        "|Beta| `[[Beta]]` | `b/(x)/c (note) (more)` |\n"
        "|   | `[[Blank]]` | `blank/` |\n"
        "| Delta | `[[Delta]]` | `(only note)` |\n"
        "| Eps | `[[Eps]]` | `e/path (unclosed` |\n"
        "| Zeta | `[[Zeta]]` | `z/a) b (c)` |\n",
        encoding="utf-8",
    )
    entities = load_entities_from_bootstrap(str(index))
    assert [(e.name, e.vault_path, e.link) for e in entities] == [
        ("Acme-Corp", "02-Areas/Clients/Acme-Corp/", "[[Acme-Corp]]"),
        ("Gamma Systems", "02-Areas/Clients/Gamma-Systems/", "[[Gamma-Systems|Gamma Systems]]"),
        # BUG FIX: only the final trailing ``(note)`` is dropped; the original
        # cut at the FIRST ``(`` and returned ``b/``.
        ("Beta", "b/(x)/c (note)", "[[Beta]]"),
        ("Eps", "e/path (unclosed", "[[Eps]]"),
        ("Zeta", "z/a) b", "[[Zeta]]"),
    ]


# ---------------------------------------------------------------------------
# temporal chunker — ``## `` section headings
# ---------------------------------------------------------------------------


def test_board_and_memory_log_section_headings(tmp_path: Path) -> None:
    board = tmp_path / "board.md"
    board.write_text(
        "---\nkanban-plugin: basic\n---\n\n## Done\n- [x] card one\n##   In Progress  \n- [ ] card two\n"
        "##  \n- [ ] card three\n##\n- card four\n",
        encoding="utf-8",
    )
    assert [(c.text, c.metadata["column"]) for c in chunk_board(str(board))] == [
        ("- [x] card one", "Done"),
        ("- [ ] card two", "In Progress"),
        ("- [ ] card three", ""),  # ``##  `` (two spaces) is a heading with an empty title
    ]

    log = tmp_path / "2026-01-02.md"
    log.write_text(
        "# Log\n\nintro\n\n## Morning\nmet agent-alpha\n## \nempty heading body\n##Afternoon\nnot a heading\n",
        encoding="utf-8",
    )
    assert [(c.text, c.metadata["section_heading"]) for c in chunk_memory_log(str(log))] == [
        ("# Log\n\nintro", None),
        # ``## `` (one space) and ``##Afternoon`` are NOT headings.
        ("## Morning\nmet agent-alpha\n## \nempty heading body\n##Afternoon\nnot a heading", "Morning"),
    ]


# ---------------------------------------------------------------------------
# frontmatter blocks — kairix.text, reflib.frontmatter, store.crawler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "parsed", "body"),
    [
        ("---\ntitle: A\nsource: B\n---\nbody\n", {"title": "A", "source": "B"}, "body\n"),
        ("---  \ntitle: A\n---   \nbody\n", {"title": "A"}, "body\n"),
        ("---\n\n\ntitle: A\n---\nbody\n", {"title": "A"}, "body\n"),
        ("---\n\n---\nbody\n", {}, "body\n"),
        ("---\ntitle: A\n---", None, "---\ntitle: A\n---"),
        ("---\n---\nbody\n", None, "---\n---\nbody\n"),
        ("no fm\n", None, "no fm\n"),
    ],
)
def test_frontmatter_extract_and_strip(text: str, parsed: dict[str, str] | None, body: str) -> None:
    assert extract_existing_frontmatter(text) == (parsed, body)
    assert strip_frontmatter(text) == body


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("---\ntitle: A\ntags:\n  - x\n---\nbody\n", {"title": "A", "tags": ["x"]}),
        ("---\ntitle: A\n---", {"title": "A"}),  # lenient: no newline after the closer
        ("---\n\ntitle: B\n---\n", {"title": "B"}),
        ("none\n", {}),
    ],
)
def test_crawler_parse_frontmatter(tmp_path: Path, text: str, parsed: dict[str, object]) -> None:
    note = tmp_path / "note.md"
    note.write_text(text, encoding="utf-8")
    assert parse_frontmatter(note) == parsed


# ---------------------------------------------------------------------------
# heading scans — kairix.text title, reflib splitter + extract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("---\ntitle: FM Title\n---\n# Heading\n", "FM Title"),
        ("---\nauthor: x\n---\n\nintro\n### Deep [link](http://x) title  \n", "Deep link title"),
        ("---  \ntitle: Spaced Fence\n---\n# H\n", "Spaced Fence"),
        # BUG FIX: an empty heading is skipped. The original ``\s+`` ran across
        # the newline and took the next line (``"## Real"``) as the title.
        ("# \n\n## Real\n", "Real"),
        ("##\n# Next line title\n", "Next line title"),
        ("#### too deep\n", "File Name"),
    ],
)
def test_extract_title_first_heading(text: str, title: str) -> None:
    assert extract_title(text, Path("/x/file-name.md")) == title


def test_split_at_headings_sections() -> None:
    body = (
        "pre " * 400
        + "\n# One\n"
        + "a " * 3000
        + "\n## Two  \n"
        + "b " * 3000
        + "\n### Three\n"
        + "c " * 10
        + "\n##\nNotHeading\n"
        + "d " * 3000
    )
    parts = split_at_headings(body, "doc", max_size=4000)
    # BUG FIX: the bare ``##`` line is not a heading, so ``NotHeading`` stays
    # in the ``Two`` section (the original grammar split it out as a heading).
    assert [(stem, len(content), content[:14]) for stem, content in parts] == [
        ("doc-part-01-preamble", 1599, "pre pre pre pr"),
        ("doc-part-02-one", 6005, "# One\na a a a "),
        ("doc-part-03-two", 12054, "## Two  \nb b b"),
    ]


def test_extract_from_headings_levels_and_titles() -> None:
    entities: list[RawEntity] = []
    relationships: list[RawRelationship] = []
    extract_from_headings(
        "# Agile Framework\n## Scrum Method  \n### \n#### Deep\n##\nKanban System\n",
        "r.md",
        "d",
        "Parent",
        entities,
        relationships,
    )
    assert [(e.name, e.entity_type) for e in entities] == [
        ("Agile Framework", "Framework"),
        ("Scrum Method", "Framework"),
    ]
    assert [(r.from_name, r.to_name, r.kind) for r in relationships] == [
        ("Parent", "Agile Framework", "TEACHES"),
        ("Parent", "Scrum Method", "TEACHES"),
        ("Scrum Method", "Agile Framework", "PART_OF"),
        # BUG FIX: ``### `` / ``##`` with no title are not headings; the
        # original grammar borrowed the next line (``#### Deep``,
        # ``Kanban System``) as their titles.
    ]


# ---------------------------------------------------------------------------
# entity overrides — entry head (label + tail)
# ---------------------------------------------------------------------------


def test_entity_override_entry_label_and_tail(tmp_path: Path) -> None:
    overrides_file = tmp_path / "overrides.md"
    overrides_file.write_text(
        '- "Acme": ORG\n'
        '  -   "agent alpha"  :  PERSON , case_insensitive: true\n'
        '- "Beta": ORGx\n'
        '- "Gamma": ORG trailing\n'
        '- "Delta": org\n'
        '- "Eps": PRODUCT,case_insensitive:false\n'
        '- "Zed": GPE   \n',
        encoding="utf-8",
    )
    overrides = load_entity_overrides(overrides_file)
    assert [(row["text"], row["label"]) for row in overrides.allowlist] == [
        ("Acme", "ORG"),
        ("AGENT ALPHA", "PERSON"),
        ("Agent Alpha", "PERSON"),
        ("agent alpha", "PERSON"),
        ("Eps", "PRODUCT"),
        ("Zed", "GPE"),
    ]


# ---------------------------------------------------------------------------
# reflib markdown — inline HTML tags + anchors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "markdown"),
    [
        (
            '<div class="x">kept</div><span>s</span><br/><b >bold</b><hr>',
            "keptsbold",
        ),
        # BUG FIX: whole tag names only — ``<b`` / ``<u`` / ``<i`` no longer
        # strip ``<blockquote>`` / ``<ul>`` / ``<iframe>`` (the original matched
        # the tag-name prefix).
        ("<blockquote>q</blockquote><ul><li>x</li></ul>", "<blockquote>q</blockquote><ul><li>x</li></ul>"),
        (
            '<a href="https://e.com">E</a> and <a class="c" href=\'/p\' target="_blank">P</a>',
            "[E](https://e.com) and [P](/p)",
        ),
        ('<a  href="1" x href="2">two</a>', "[two](2)"),
        ('<a href="1" title="t" href="2">dup</a>', "[dup](2)"),
        ('<a\nhref="u">multi\nline</a>', "[multi\nline](u)"),
        ('<a name="x">no href</a>', '<a name="x">no href</a>'),
        ('<A HREF="U">Upper</A>', "[Upper](U)"),
        ('<a data-href="d" href="h">D</a>', "[D](h)"),
    ],
)
def test_strip_html_tags_anchors_and_block_tags(html: str, markdown: str) -> None:
    assert strip_html_tags(html) == markdown
