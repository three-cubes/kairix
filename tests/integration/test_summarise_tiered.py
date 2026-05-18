"""End-to-end integration tests for tiered summary generation.

Wires the summary pipeline through real components:
  - ``generate_summaries`` (batch driver in
    ``kairix.knowledge.summaries.generate``)
  - ``write_summary`` / ``get_summary`` (DB roundtrip via
    ``kairix.knowledge.summaries.staleness``)
  - ``SummariesDeps`` for chat boundary injection

What's covered here that unit + BDD don't catch:
  - L0 and L1 produced for the SAME source — both land in the SAME DB
    row (idempotent upsert on ``path``).
  - L0's token cap (150) is smaller than L1's (600). The recorded chat
    seam calls confirm both caps and the tier metadata round-trips
    intact through the DB.
  - Multiple files in a batch all hit the DB; the staleness store
    reflects that.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kairix.knowledge.summaries.generate import (
    SummariesDeps,
    generate_summaries,
)
from kairix.knowledge.summaries.staleness import (
    SUMMARIES_DB,  # noqa: F401 — import probes the module's lazy path resolution
    get_summary,
    init_summaries_db,
    write_summary,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Boundary fakes
# ---------------------------------------------------------------------------


class _TieredChat:
    """Routes to a different canned reply based on the system prompt.

    The summary module's system prompts contain ``"1-2 sentences"`` for
    L0 and ``"structured overview"`` for L1. Matching on that lets the
    fake answer the right call without inspecting internal helper names.
    """

    def __init__(self, l0: str, l1: str) -> None:
        self._l0 = l0
        self._l1 = l1
        self.calls: list[dict[str, Any]] = []

    def __call__(self, messages: list[dict[str, Any]], max_tokens: int = 0) -> str:
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        if "structured overview" in system:
            return self._l1
        return self._l0


def _make_doc(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def summaries_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """In-tmp_path sqlite DB with the summaries schema applied."""
    db = sqlite3.connect(str(tmp_path / "summaries.db"))
    init_summaries_db(db)
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_summarise_l0_only_writes_short_abstract_and_no_l1(tmp_path: Path, summaries_db: sqlite3.Connection) -> None:
    """L0-only path: ``include_l1=False`` produces one chat call per file
    (the L0 system prompt) and the DB row has ``l1=None``. L0 cap is 150
    tokens (verifies the smaller cap is threaded through).

    Sabotage: if ``include_l1=False`` accidentally triggered the L1
    branch, ``len(chat.calls) == 1`` would fail (would be 2) and the
    DB row would carry a non-None L1.
    """
    doc = _make_doc(tmp_path, "alpha.md", "Alpha doc body explaining the topic of alphabetical ordering.")
    chat = _TieredChat(l0="Short abstract for alpha.", l1="SHOULD-NOT-FIRE")

    results = generate_summaries(
        paths=[str(doc)],
        api_key="",
        endpoint="",
        deployment="gpt-4o-mini",
        include_l1=False,
        batch_size=10,
        sleep_ms=0,
        deps=SummariesDeps(chat=chat),
    )

    assert len(results) == 1
    res = results[0]
    assert res.l0 == "Short abstract for alpha."
    assert res.l1 is None
    assert res.model == "gpt-4o-mini"
    assert res.generated_at  # ISO timestamp populated.

    # Persist + roundtrip.
    write_summary(res, summaries_db)
    fetched = get_summary(str(doc), summaries_db)
    assert fetched is not None
    assert fetched.l0 == "Short abstract for alpha."
    assert fetched.l1 is None

    # Exactly one chat call — the L0 prompt — capped at 150 tokens.
    assert len(chat.calls) == 1
    assert chat.calls[0]["max_tokens"] == 150


def test_summarise_l0_and_l1_for_same_file_share_one_db_row(tmp_path: Path, summaries_db: sqlite3.Connection) -> None:
    """``include_l1=True`` produces both L0 (≤150 tok) and L1 (≤600 tok)
    in the same SummaryResult. After writing once, the staleness DB has
    a single row whose l0 < l1 in size and both have content.

    Sabotage: if the batch driver wrote L0 and L1 to separate rows
    (e.g. one per tier), ``COUNT(*)`` would be 2 not 1; or if the L1
    max_tokens were equal to L0's, the cap-assertion comparison would
    fail.
    """
    doc = _make_doc(tmp_path, "beta.md", "Beta doc covering the second letter and its uses.")
    chat = _TieredChat(
        l0="Beta abstract.",
        l1=(
            "Main topic: the letter beta.\n"
            "- Used as a placeholder name.\n"
            "- Marks pre-release software.\n"
            "Status: in active use."
        ),
    )

    results = generate_summaries(
        paths=[str(doc)],
        api_key="",
        endpoint="",
        deployment="gpt-4o-mini",
        include_l1=True,
        batch_size=10,
        sleep_ms=0,
        deps=SummariesDeps(chat=chat),
    )

    assert len(results) == 1
    res = results[0]
    assert res.l0 == "Beta abstract."
    assert res.l1 is not None
    # L0 strictly shorter than L1 (token-cap → content-size invariant).
    assert len(res.l0) < len(res.l1)

    write_summary(res, summaries_db)
    rows = summaries_db.execute("SELECT COUNT(*) FROM summaries WHERE path = ?", (str(doc),)).fetchone()
    assert rows[0] == 1, "L0 + L1 must land in a SINGLE upserted row, not two"

    fetched = get_summary(str(doc), summaries_db)
    assert fetched is not None
    assert fetched.l0 and fetched.l1
    assert fetched.l0 != fetched.l1

    # Two chat calls — one per tier — with the documented per-tier caps.
    assert len(chat.calls) == 2
    caps = sorted(c["max_tokens"] for c in chat.calls)
    assert caps == [150, 600]


def test_summarise_batch_lands_every_file_in_the_store(tmp_path: Path, summaries_db: sqlite3.Connection) -> None:
    """A 3-file batch produces 3 SummaryResults; each writes a row.
    The staleness store reports 3 entries with L0 populated.

    Sabotage: if the batch driver short-circuited after the first file
    (e.g. ``break`` instead of ``continue`` on a per-file exception
    path), only 1 row would land and the row-count assertion fails.
    """
    docs = [
        _make_doc(tmp_path, "one.md", "First document body — long enough to summarise meaningfully."),
        _make_doc(tmp_path, "two.md", "Second document body — covers a different topic entirely."),
        _make_doc(tmp_path, "three.md", "Third document body — yet another angle on the corpus."),
    ]
    chat = _TieredChat(l0="L0 abstract.", l1="L1 overview.")

    results = generate_summaries(
        paths=[str(d) for d in docs],
        api_key="",
        endpoint="",
        deployment="gpt-4o-mini",
        include_l1=False,
        batch_size=10,
        sleep_ms=0,
        deps=SummariesDeps(chat=chat),
    )

    assert len(results) == 3
    for res in results:
        write_summary(res, summaries_db)

    rows = summaries_db.execute("SELECT COUNT(*) FROM summaries WHERE l0 IS NOT NULL AND l0 != ''").fetchone()
    assert rows[0] == 3

    # Each file's row reachable via get_summary.
    for d in docs:
        fetched = get_summary(str(d), summaries_db)
        assert fetched is not None
        assert fetched.l0 == "L0 abstract."
