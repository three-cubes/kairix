"""
E2E test: embed 50 real chunks via Azure, verify vector search returns correct docs.

Skipped unless KAIRIX_E2E=1 is set. Requires:
  - Real kairix index at ~/.cache/kairix/index.sqlite
  - KAIRIX_LLM_API_KEY + KAIRIX_LLM_ENDPOINT set

Run manually pre-deploy:
  KAIRIX_E2E=1 python3 -m pytest tests/e2e/ -v -s
"""

import os
import shutil
import sqlite3

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("KAIRIX_E2E") != "1", reason="E2E tests skipped unless KAIRIX_E2E=1")

_DATA_DIR = os.environ.get("KAIRIX_DATA_DIR", "/data")

# Known gold: (query, fragment that must appear in top-3 vsearch results)
GOLD_QUERIES = [
    ("Jordan Blake voice profile", "jordan-blake-voice-profile"),
    ("Arize Phoenix observability", "arize-observability-research"),
    ("SPF record duplicate permerror", "shared/rules"),
    ("SQLite lock crash", "shared/facts"),
]


@pytest.fixture(scope="module")
def embedded_db(tmp_path_factory):
    """
    Copy the live kairix DB to a temp path, embed 50 chunks via Azure,
    and return the path. Restores env after.
    """
    from kairix.core.db import get_db_path

    src = get_db_path()
    tmp_dir = tmp_path_factory.mktemp("kairix_e2e")
    tmp_db_path = tmp_dir / "index.sqlite"
    shutil.copy2(src, tmp_db_path)

    # Clear existing vectors in the copy
    db = sqlite3.connect(str(tmp_db_path))
    db.execute("DELETE FROM content_vectors")
    db.commit()
    db.close()

    # Run embed on the copy with --limit 50
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("KAIRIX_DB_PATH", str(tmp_db_path))
        from kairix.core.embed.embed import run_embed
        from kairix.core.embed.schema import validate_schema

        db = sqlite3.connect(str(tmp_db_path))
        validate_schema(db)
        result = run_embed(db, force=False, limit=50)
        db.close()

    assert result["embedded"] > 0, f"No chunks embedded: {result}"
    return tmp_db_path


# NOTE: This file references kairix.core.search.vector.vector_search which
# does not exist in the current codebase (the API is now VectorSearchBackend
# / UsearchVectorRepository). The tests are E2E-gated by KAIRIX_E2E=1 so
# they don't run in CI; if this E2E suite is revived, the test bodies need
# updating to drive the current vector-search API with an explicit
# UsearchVectorRepository(db_path=embedded_db) — no env-monkeypatch.
