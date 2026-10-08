"""SGO-109 — execute worker.py's ``_default_*`` DI-default seams (F86 + F7/F9).

Why this module exists
----------------------
``kairix/worker.py`` wires every maintenance task through a
``WorkerDeps`` (or task-local ``*Deps``) field whose ``default_factory``
binds a ``_default_*`` production seam — the lazy-import delegation a
real operator runs when no test injects a fake. Those seams ARE the
production path. The pre-existing worker tests deliberately inject fakes
for *every* field (the dispatch-via-deps pattern), so the ``_default_*``
seam BODIES were never executed by the unit suite: they showed
``hits=0`` in the per-file coverage report and were the bulk of
worker.py's sub-floor lines. That is precisely the escape-4 /
F86-dynamic shape — a production-default seam invisible to the floor
because no test runs it.

This module closes that gap with the **proven PR #605 pattern**
(``warm_retrieval_stack`` / ``WarmStackDeps``): rather than pad, pragma,
or patch internals, each test calls the **public** runner with
``deps=None`` so the production ``_default_*`` seam binds and EXECUTES,
then asserts on what the seam actually *did* in the unit environment
(graceful degradation — no provider secret, no live Neo4j, an empty
document root). No production code changes: the seams are already thin
DI defaults (the heavy ``run_default_drain_tick`` extraction landed in
commit 651942cb). This is the test side of that already-extracted seam.

Guardrails honoured (non-negotiable, see SGO-109):
  * **F1** — no ``patch("kairix...")`` / ``monkeypatch.setattr`` on any
    kairix attribute.
  * **F2** — no ``monkeypatch.setenv/delenv("KAIRIX_*")``. The
    ``isolated_worker_env`` fixture steers the paths boundary through the
    production ``kairix.config.yaml`` ``paths:`` mechanism (resolved via
    ``KairixPaths`` + ``kairix.paths.clear_cache``) under a tmp
    ``XDG_CONFIG_HOME`` — ``XDG_*`` is not a KAIRIX_* var, the same
    F2-clean redirect conftest's ``_hermetic_user_config`` uses. The two
    ambient provider/graph secrets are cleared by the conftest session
    baseline, so this module never writes a KAIRIX_* env key.
  * **F5** — no import of any ``_default_*`` private name. Every seam is
    reached through its public caller (``run_embed``, ``run_health_check``,
    ``run_neo4j_drain``, ``run_wal_checkpoint``, ``run_connector_sync``,
    ``run_entity_seed``, ``seed_canonical_entities_at_boot``).
  * **F86** — each test EXECUTES the production-default seam body
    (``deps=None`` binds it), so the seam is both pragma-free (static)
    and run-by-the-suite (dynamic).

Determinism: every assertion pins a *degraded* outcome that does not
depend on host services (no provider key → embed fails; no Neo4j
password → drain/seed report unavailable; empty doc root → seed finds
nothing). The one host-variable signal (how many onboarding checks
pass) is asserted only structurally — that the seam returned a real
list the runner counted — never on a specific count.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import kairix.paths
from kairix.worker import (
    CanonicalEntitySeedDeps,
    run_connector_sync,
    run_embed,
    run_entity_seed,
    run_health_check,
    run_neo4j_drain,
    run_wal_checkpoint,
    seed_canonical_entities_at_boot,
)

pytestmark = pytest.mark.unit

# Provider / graph secrets (KAIRIX_PROVIDER_LLM_API_KEY, KAIRIX_NEO4J_PASSWORD)
# whose ambient presence on a developer machine would flip the degraded
# outcomes these tests pin are cleared for the whole run by the session
# baseline in tests/conftest.py (``_hermetic_data_dirs`` /
# ``_OPERATOR_CREDENTIAL_ENV``) — the one place F2 allows KAIRIX_* env
# mutation. This module never writes a KAIRIX_* key itself.
#
# EXCEPT under ``KAIRIX_E2E=1``: the live-credential gate deliberately keeps
# the operator's real credentials in place. The credential-dependent tests
# below pin the NO-credential degraded path by executing the production
# ``_default_*`` seams (deps=None) — injecting a fake through the deps seam
# would bypass the very seam under test, and the process env must not be
# mutated (F2). So under KAIRIX_E2E=1 they are skipped rather than run the
# production defaults against a live provider / Neo4j account.
_skip_with_live_credentials = pytest.mark.skipif(
    os.environ.get("KAIRIX_E2E") == "1",
    reason=(
        "KAIRIX_E2E=1 keeps real provider/Neo4j credentials; this pins the no-credential path of a _default_* seam"
    ),
)


@pytest.fixture
def isolated_worker_env(tmp_path: Path) -> Iterator[Path]:
    """Point every host-touching default at a throwaway tmp tree — F2-clean.

    The ``_default_*`` seams open the real SQLite index, crawl the real
    document root, and try the real Neo4j/provider config. To keep the
    EXECUTED seam hermetic on a developer machine (CI's HOME is already
    clean) without the forbidden ``monkeypatch.setenv("KAIRIX_*")``
    (F2 / #139 boundary-only), we steer the paths boundary the way
    production does — a ``kairix.config.yaml`` ``paths:`` block — and let
    :func:`kairix.paths.document_root` / :func:`kairix.paths.db_path`
    resolve it through ``KairixPaths``. The config lands under a tmp
    ``XDG_CONFIG_HOME`` (``XDG_*`` is NOT a KAIRIX_* var, so this is the
    same F2-clean redirect conftest's ``_hermetic_user_config`` already
    uses), and :func:`kairix.paths.clear_cache` invalidates the cached
    resolution so the test's config wins.

    Result: the WAL checkpoint runs against the tmp DB, the entity-seed
    crawl walks an empty directory, and no developer document folder is
    touched — all through the production path-resolution boundary, no
    KAIRIX_* process-env mutation, no kairix attribute patching (F1/F2-clean).
    """
    data_dir = tmp_path / "data"
    docs_dir = tmp_path / "docs"
    config_dir = tmp_path / "xdg-config" / "kairix"
    data_dir.mkdir()
    docs_dir.mkdir()
    config_dir.mkdir(parents=True)

    # Production path-resolution boundary: a kairix.config.yaml paths block,
    # read through KairixPaths (the #139 boundary), steers document_root +
    # db_path at the tmp tree without any KAIRIX_* env mutation.
    db_file = data_dir / "kairix.db"
    (config_dir / "kairix.config.yaml").write_text(
        f"paths:\n  document_root: {docs_dir}\n  db_path: {db_file}\n",
        encoding="utf-8",
    )
    # XDG_CONFIG_HOME is NOT a KAIRIX_* var — F2-clean, the conftest pattern.
    # We set it directly (and restore) so the layered config loader resolves
    # the test config at $XDG_CONFIG_HOME/kairix/kairix.config.yaml.
    prev_xdg = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = str(tmp_path / "xdg-config")

    # Invalidate the cached path resolution so the test config is read.
    kairix.paths.clear_cache()
    try:
        yield tmp_path
    finally:
        # Restore env exactly and drop the test config from the cache.
        if prev_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = prev_xdg
        kairix.paths.clear_cache()


@_skip_with_live_credentials
def test_default_embed_seam_executes_and_returns_false_without_provider(
    isolated_worker_env: Path,
) -> None:
    """``run_embed()`` (deps=None) binds ``_default_embed``, which calls the
    REAL ``run_incremental_embed_pipeline``; with no provider key it raises
    and the runner degrades to ``False``.

    Behavioural assertion: the production embed seam ran end-to-end and
    the runner reported "no work / failed" as ``False`` — not a swallowed
    no-op.

    Sabotage proof: if the seam stopped delegating to the real pipeline
    (e.g. ``return EmbedPipelineResult(...)`` stub) ``run_embed`` would
    return ``True``/raise instead of the degraded ``False``; if the
    runner's ``except`` swallowed the failure as success this flips.
    """
    result = run_embed()  # deps=None → production _default_embed seam

    assert result is False, (
        "the production embed seam must run and, with no provider secret in "
        "the unit env, degrade to False (run_embed returns bool)"
    )


@_skip_with_live_credentials
def test_default_embed_seam_logs_provider_failure(isolated_worker_env: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The executed ``_default_embed`` seam surfaces the REAL pipeline's
    missing-provider-secret error through the runner's warning log.

    Behavioural assertion: the worker logged "embed pipeline raised" with
    the specific ``kairix-provider-llm-api-key`` secret-loader message —
    proving the seam reached the real ``run_incremental_embed_pipeline``
    and its provider-secret resolution path (not a fake, and not some
    unrelated failure), and the runner caught it to keep the worker alive.

    Sabotage proof: delete the ``run_embed`` ``except`` arm and the test
    raises instead of logging; replace the seam with a stub (whatever it
    returns or however it fails) and this specific provider-secret message
    never appears — only the real pipeline emits it.
    """
    with caplog.at_level(logging.WARNING, logger="kairix.worker"):
        run_embed()

    raised = [r.getMessage() for r in caplog.records if "embed pipeline raised" in r.getMessage()]
    assert raised, "the executed embed seam must surface its failure via the worker warning log"
    assert any("kairix-provider-llm-api-key" in m for m in raised), (
        "the logged failure must be the REAL pipeline's missing-provider-secret error, "
        "proving _default_embed reached run_incremental_embed_pipeline's secret-loader "
        f"path (not a stub). Got: {raised!r}"
    )


def test_default_health_check_seam_executes_and_runs_all_checks(
    isolated_worker_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``run_health_check()`` (deps=None) binds ``_default_health_check``,
    which calls the REAL ``run_all_checks`` and returns a list the runner
    counts.

    Behavioural assertion: the seam executed and produced a real
    ``passed/total`` line with a positive total (the onboarding check
    suite is non-empty). The count of *passing* checks is host-dependent
    (Docker/Neo4j up or not), so only the *total* and the structural
    "X/Y passed" shape are asserted — never a specific pass count, which
    would be a flaky / dishonest assertion.

    Sabotage proof: if the seam returned ``[]`` (stub) the total would be
    0; if the seam stopped delegating to ``run_all_checks`` the
    "health check N/M passed" record never appears.
    """
    with caplog.at_level(logging.INFO, logger="kairix.worker"):
        run_health_check()  # deps=None → production _default_health_check seam

    messages = [r.getMessage() for r in caplog.records]
    health_lines = [m for m in messages if "health check" in m and "passed" in m]
    assert health_lines, "the production health-check seam must run all checks and log a result"
    # Parse "worker: health check P/T passed" — T (total) must be > 0,
    # proving a real non-empty check suite ran. P is host-dependent: not asserted.
    summary = health_lines[0]
    fraction = summary.split("health check", 1)[1].split("passed", 1)[0].strip()
    passed_str, total_str = fraction.split("/")
    assert int(total_str) > 0, (
        f"the real onboarding check suite must be non-empty; got total={total_str!r}. "
        "A 0 total means the seam returned an empty list (stubbed), not run_all_checks()."
    )
    assert 0 <= int(passed_str) <= int(total_str)


@_skip_with_live_credentials
def test_default_neo4j_drain_seam_executes_and_reports_unavailable(
    isolated_worker_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``run_neo4j_drain()`` (deps=None) binds ``_default_neo4j_drain``, which
    runs the REAL ``run_default_drain_tick``; with a row pending and no Neo4j
    it returns ``neo4j_available=False`` and the runner logs the skip.

    A pending ``entity_signals`` row is staged first because the drain now
    short-circuits an EMPTY queue before it ever probes Neo4j (the PLA-331
    idle-skip — an idle vault must not spin up a driver every tick). With work
    pending, the real seam builds the client, finds it unreachable, and takes
    the "backend unavailable" branch.

    Behavioural assertion: the production drain seam ran the real tick and
    the runner took the ``neo4j_available is False`` branch (the
    "skipped — backend unavailable" log), not the success-summary branch.

    Sabotage proof: if the runner dropped the ``not neo4j_available``
    guard it would log a (bogus) "drain complete" line instead; if the
    seam stopped delegating to ``run_default_drain_tick`` no drain
    lifecycle log appears at all.
    """
    import sqlite3

    from kairix.core.db.schema import create_schema

    # Stage a pending row at the resolved (tmp) index so the drain reaches the
    # Neo4j client instead of idle-skipping — the idle-skip case is covered by
    # tests/unit/test_neo4j_drain.py.
    seed_conn = sqlite3.connect(str(kairix.paths.db_path()))
    try:
        create_schema(seed_conn)
        seed_conn.execute(
            "INSERT INTO entity_signals (kind, value, source_uri, modified_at, confidence, "
            "sensitivity, pushed_to_neo4j, push_attempt_count) "
            "VALUES ('person', 'agent-alpha', 'vault://x', '2026-05-25T10:00:00Z', 0.9, "
            "'internal', 0, 0)"
        )
        seed_conn.commit()
    finally:
        seed_conn.close()

    with caplog.at_level(logging.INFO, logger="kairix.worker"):
        run_neo4j_drain()  # deps=None → production _default_neo4j_drain seam

    messages = [r.getMessage() for r in caplog.records]
    assert any("starting neo4j drain" in m for m in messages), (
        "the drain runner must reach the seam (start log present)"
    )
    assert any("backend unavailable" in m for m in messages), (
        "with no Neo4j the executed drain seam must report neo4j_available=False "
        "and the runner must take the skip branch"
    )
    assert not any("drain complete" in m for m in messages), (
        "the unavailable branch must NOT fall through to the success summary"
    )


def test_default_wal_checkpoint_seam_executes_against_real_sqlite(
    isolated_worker_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``run_wal_checkpoint()`` (deps=None) binds ``_default_wal_checkpoint``,
    which opens the REAL SQLite index (here a tmp DB) and runs
    ``PRAGMA wal_checkpoint(TRUNCATE)``.

    Behavioural assertion: the seam opened a real connection, ran the
    ``PRAGMA wal_checkpoint(TRUNCATE)``, and the runner logged the
    structured ``busy/log_pages/checkpointed`` triple parsed from the
    pragma's real return tuple — proving the production checkpoint path
    executed end-to-end (against an isolated tmp DB, not the operator's
    index), returning the SQLite triple rather than raising.

    Sabotage proof: if the seam returned a non-dict / stub the runner's
    ``isinstance(result, dict)`` ternaries would log zeros without the
    three keys ever resolving from a real pragma; if the seam's
    ``sqlite3.connect(...).execute("PRAGMA wal_checkpoint...")`` body were
    removed the connection-open path is uncovered and (on a missing DB
    dir) the runner would log "wal checkpoint raised" instead of the
    "complete" triple this asserts.
    """
    with caplog.at_level(logging.INFO, logger="kairix.worker"):
        run_wal_checkpoint()  # deps=None → production _default_wal_checkpoint seam

    messages = [r.getMessage() for r in caplog.records]
    assert any("starting wal checkpoint" in m for m in messages)
    complete_lines = [m for m in messages if "wal checkpoint complete" in m]
    assert complete_lines, (
        "the production WAL-checkpoint seam must open the real SQLite DB, run the "
        "TRUNCATE pragma, and the runner must log the structured result (not raise)"
    )
    # The completion log carries the structured triple from the real
    # PRAGMA return tuple — proof the seam ran the pragma and produced a
    # dict the runner unpacked, not a swallowed error.
    summary = complete_lines[0]
    for key in ("busy=", "log_pages=", "checkpointed="):
        assert key in summary, (
            f"the executed checkpoint seam must surface the real SQLite pragma triple; missing {key!r} in: {summary!r}"
        )
    assert "wal checkpoint raised" not in " ".join(messages), (
        "the seam must complete cleanly against the isolated tmp DB, not raise"
    )


def test_default_connector_sync_seam_executes_and_returns_result(
    isolated_worker_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``run_connector_sync()`` (deps=None) binds ``_default_connector_sync``,
    which delegates to the REAL ``dispatch_connector_sync`` and returns a
    structured ``ConnectorSyncResult``.

    Behavioural assertion: the seam ran the real dispatcher and the runner
    logged the structured ``synced/failed/dead_letter_added`` completion
    line — proving the Wave-2 connector path executed (with no connectors
    configured it completes with zero counters, not by raising).

    Sabotage proof: if the seam regressed to raising ``NotImplementedError``
    the runner would log "not yet implemented" instead of the completion
    summary; if it returned ``None`` the ``result.synced`` access in the
    runner would raise and the "complete" line would be missing.
    """
    with caplog.at_level(logging.INFO, logger="kairix.worker"):
        run_connector_sync()  # deps=None → production _default_connector_sync seam

    messages = [r.getMessage() for r in caplog.records]
    assert any("starting connector sync" in m for m in messages)
    assert any("connector sync complete" in m for m in messages), (
        "the production connector-sync seam must run the real dispatcher and the "
        "runner must log the structured ConnectorSyncResult counters"
    )
    assert not any("not yet implemented" in m for m in messages), (
        "the Wave-2 seam must NOT take the legacy NotImplementedError branch"
    )


def test_default_entity_seed_seam_executes_against_empty_doc_root(
    isolated_worker_env: Path,
) -> None:
    """``run_entity_seed()`` (deps=None) binds ``_default_entity_seed``, which
    runs the REAL store-crawl CLI against the document root.

    Behavioural assertion: the seam executed the real crawl over an empty
    isolated document root and the runner returned without raising — the
    crawl found nothing to seed and degraded cleanly (the #270 discipline:
    a maintenance helper's ``SystemExit`` must not crash the worker).

    Sabotage proof: if the seam stopped delegating to the store crawl,
    the crawl-over-doc-root path is uncovered; if the runner dropped its
    ``(Exception, SystemExit)`` guard, a no-entities ``SystemExit`` from
    the crawl CLI would propagate and fail this test.
    """
    # Must not raise — the empty doc root yields a no-op crawl and the
    # runner's (Exception, SystemExit) guard absorbs any CLI sys.exit.
    # (run_entity_seed returns None by contract; the assertion is the
    # absence of a propagated exception, which a raise would turn red.)
    run_entity_seed()


@_skip_with_live_credentials
def test_default_neo4j_client_for_seed_seam_executes_when_canonicals_present(
    isolated_worker_env: Path,
) -> None:
    """``seed_canonical_entities_at_boot`` with canonicals present (but the
    Neo4j client default left in place) binds and EXECUTES
    ``_default_neo4j_client_for_seed`` → the REAL ``get_client``.

    Only the public ``load_canonical_entities_fn`` field is injected (to
    get past the empty-config early return); ``neo4j_client_fn`` keeps its
    production default, so the seam runs. With no ``KAIRIX_NEO4J_PASSWORD``
    the client reports unavailable and the boot helper degrades to 0
    seeded — never raising (it is failure-isolated by contract).

    Behavioural assertion: the production Neo4j-client seam ran and the
    boot helper returned 0 (degraded), not a crash.

    Sabotage proof: if ``_default_neo4j_client_for_seed`` stopped calling
    the real ``get_client`` the live-client construction path is
    uncovered; if ``seed_canonical_entities_at_boot`` lost its
    failure-isolation ``except`` the unavailable-Neo4j error would
    propagate and this test would raise instead of returning 0.
    """

    class _Canonical:
        """Minimal canonical-entity stand-in (public DTO shape: a ``name``)."""

        name = "Example Org"

    deps = CanonicalEntitySeedDeps(load_canonical_entities_fn=lambda: [_Canonical()])

    seeded = seed_canonical_entities_at_boot(deps)

    assert seeded == 0, (
        "with the production Neo4j-client seam bound and no Neo4j password, the "
        "boot seeder must degrade to 0 seeded (failure-isolated), not raise"
    )
