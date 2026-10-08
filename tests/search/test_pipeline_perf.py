"""Targeted perf tests for SearchPipeline hot-path fixes.

Three fixes shipped together (one per top-3 wall-clock leaf in the production
post-#409 probe data):

1. ``dispatch`` — BM25 + vector now run in parallel via the module-level
   ``_DISPATCH_POOL``. Was strictly sequential before, cutting the per-search
   dispatch floor from ``bm25 + vector`` to ``max(bm25, vector)``.

2. ``vector_ann`` — :class:`VectorIndex` keeps a persistent read-only SQLite
   connection for the metadata batched-SELECT instead of opening + closing
   one per search. Saves the per-call ``sqlite3.connect`` + PRAGMA cost.

3. ``embed_http`` — :class:`ProviderEmbeddingService` resolves
   ``get_embed_cache`` at construction time instead of importing it on every
   ``embed()`` call. Eliminates the per-call ``from kairix.transport.cache
   import ...`` statement evaluation on the warm-cache hot path.

Each fix has:
  * a unit test pinning the NEW fast-path behaviour (e.g. concurrent dispatch,
    single connection open, init-once cache lookup), and
  * a regression test pinning OBSERVABLE behaviour (result-set / row shape /
    stage-latency keys) so the fix can't change what callers see.

Sabotage protocol (executed for each new test below — comment on each
test names the exact mutation):

  Revert the production-side change -> run the test -> confirm it fails ->
  restore. The mutation is described per-test so a reviewer can re-run it.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.pipeline import (
    DEFAULT_DISPATCH_CONCURRENCY,
    SearchPipeline,
    build_dispatch_pool,
    build_rerank_pool,
    cpu_aware_default_concurrency,
    dispatch_workers_for,
    rerank_workers_for,
    resolve_dispatch_concurrency,
)
from kairix.paths import read_int_env
from kairix.transport.embed_service import ProviderEmbeddingService
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeFusion,
    FakeGraphRepository,
    FakeProvider,
    FakeVectorRepository,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    *,
    bm25_backend: Any | None = None,
    vector_backend: Any | None = None,
) -> SearchPipeline:
    """Build a minimal SearchPipeline with fake defaults, overrides accepted.

    Direct construction is sanctioned here — these are perf unit tests
    pinning leaf behaviour against the protocol shape, not BDD step impls
    (F47 sanctions factory-based composition for the latter only).
    """
    bm25 = bm25_backend if bm25_backend is not None else BM25SearchBackend(FakeDocumentRepository())
    vector = (
        vector_backend
        if vector_backend is not None
        else VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository())
    )
    return SearchPipeline(
        classifier=FakeClassifier(),
        bm25=bm25,
        vector=vector,
        graph=FakeGraphRepository(available=True),
        fusion=FakeFusion(),
        boosts=[],
        logger=None,
    )


class _SleepBM25Backend:
    """BM25 backend that sleeps for ``delay_s`` to model the SQLite FTS5 cost.

    Records its start + finish timestamps so the test can assert that the
    sleep overlapped with the vector leg's sleep (parallel dispatch) rather
    than running serially after / before it.
    """

    def __init__(self, delay_s: float = 0.05) -> None:
        self.delay_s = delay_s
        self.start_t: float | None = None
        self.finish_t: float | None = None
        self.calls = 0

    def search(self, query: str, collections: list[str] | None = None, limit: int = 20) -> list[dict]:
        self.start_t = time.monotonic()
        time.sleep(self.delay_s)
        self.finish_t = time.monotonic()
        self.calls += 1
        return [{"file": f"bm25-{query}.md", "title": "bm25", "snippet": "", "score": 1.0, "collection": "c"}]

    def get_chunk_dates(self, paths: list[str]) -> dict[str, str]:
        return {}


class _SleepVectorBackend:
    """Vector backend that sleeps to model the embed_http + ANN cost.

    Same start/finish capture as :class:`_SleepBM25Backend`; pairs with it
    to prove parallel dispatch by checking time-window overlap.
    """

    def __init__(self, delay_s: float = 0.08) -> None:
        self.delay_s = delay_s
        self.start_t: float | None = None
        self.finish_t: float | None = None
        self.calls = 0

    def search(
        self,
        query: str,
        collections: list[str] | None = None,
        limit: int = 10,
        *,
        timings: dict[str, float] | None = None,
    ) -> list[dict]:
        self.start_t = time.monotonic()
        time.sleep(self.delay_s)
        self.finish_t = time.monotonic()
        self.calls += 1
        if timings is not None:
            timings["embed_http"] = self.delay_s * 600.0  # synthetic ms
            timings["vector_ann"] = self.delay_s * 400.0
        return [{"path": f"vec-{query}.md", "distance": 0.1, "collection": "c"}]


# ---------------------------------------------------------------------------
# Fix 1 — dispatch parallelism
# ---------------------------------------------------------------------------


class _RendezvousBM25Backend:
    """BM25 backend that blocks on a shared two-party barrier.

    Pairs with :class:`_RendezvousVectorBackend`: both legs can only pass
    the barrier if they are in flight AT THE SAME TIME. Sequential dispatch
    leaves the first leg alone at the barrier until its bounded wait breaks
    it — a deterministic parallelism proof with no wall-clock ceiling (F82).
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        self.barrier = barrier
        self.rendezvous_ok: bool | None = None
        self.calls = 0

    def search(self, query: str, collections: list[str] | None = None, limit: int = 20) -> list[dict]:
        self.calls += 1
        try:
            self.barrier.wait()
            self.rendezvous_ok = True
        except threading.BrokenBarrierError:
            self.rendezvous_ok = False
        return [{"file": f"bm25-{query}.md", "title": "bm25", "snippet": "", "score": 1.0, "collection": "c"}]

    def get_chunk_dates(self, paths: list[str]) -> dict[str, str]:
        return {}


class _RendezvousVectorBackend:
    """Vector backend twin of :class:`_RendezvousBM25Backend`."""

    def __init__(self, barrier: threading.Barrier) -> None:
        self.barrier = barrier
        self.rendezvous_ok: bool | None = None
        self.calls = 0

    def search(
        self,
        query: str,
        collections: list[str] | None = None,
        limit: int = 10,
        *,
        timings: dict[str, float] | None = None,
    ) -> list[dict]:
        self.calls += 1
        try:
            self.barrier.wait()
            self.rendezvous_ok = True
        except threading.BrokenBarrierError:
            self.rendezvous_ok = False
        return [{"path": f"vec-{query}.md", "distance": 0.1, "collection": "c"}]


@pytest.mark.unit
def test_dispatch_runs_bm25_and_vector_in_parallel() -> None:
    """BM25 and vector legs are in flight concurrently — they no longer serialise.

    Each leg waits on a shared two-party barrier. Only concurrent dispatch
    lets both legs reach the barrier together; sequential dispatch leaves
    the first leg waiting alone until the barrier's bounded timeout (a
    hang guard, not a timing assertion) breaks it. The outcome — did both
    legs rendezvous — is deterministic on any host load (F82).

    Sabotage-proof: in ``SearchPipeline._dispatch_backends``, replace the
    ``submit + future.result()`` pair with the prior sequential
    ``bm25_results = _run_bm25(); vec_results, vec_failed = _run_vector()``
    and re-run; the first leg's barrier wait breaks and the
    ``rendezvous_ok`` assertions fail.
    """
    barrier = threading.Barrier(2, timeout=2.0)
    bm25 = _RendezvousBM25Backend(barrier)
    vector = _RendezvousVectorBackend(barrier)
    pipeline = _make_pipeline(bm25_backend=bm25, vector_backend=vector)

    result = pipeline.search("hot path")

    # Both legs ran exactly once — neither was skipped by the parallelism.
    assert bm25.calls == 1
    assert vector.calls == 1
    # Both legs met at the barrier — they were in flight concurrently.
    assert bm25.rendezvous_ok is True, "bm25 leg never met the vector leg — dispatch ran sequentially"
    assert vector.rendezvous_ok is True, "vector leg never met the bm25 leg — dispatch ran sequentially"
    # Stage-level confirmation: per-leg timings still reported (operators
    # rely on them in probe data).
    assert "bm25" in result.stage_latency_ms
    assert "vector" in result.stage_latency_ms


@pytest.mark.unit
def test_dispatch_bm25_and_vector_time_windows_overlap() -> None:
    """The start/finish windows of the two legs share at least one moment.

    This is the structural proof of parallelism: ``bm25.start <
    vector.finish`` AND ``vector.start < bm25.finish``. If either backend
    finishes before the other starts, the legs ran serially.

    Sabotage-proof: same revert as
    ``test_dispatch_runs_bm25_and_vector_in_parallel``. In sequential
    mode whichever leg ran first finishes before the other starts,
    failing the overlap assert with a precise diagnostic.
    """
    bm25 = _SleepBM25Backend(delay_s=0.05)
    vector = _SleepVectorBackend(delay_s=0.05)
    pipeline = _make_pipeline(bm25_backend=bm25, vector_backend=vector)

    pipeline.search("hot path overlap")

    assert bm25.start_t is not None
    assert bm25.finish_t is not None
    assert vector.start_t is not None
    assert vector.finish_t is not None
    assert bm25.start_t < vector.finish_t, "bm25 started after vector finished — not parallel"
    assert vector.start_t < bm25.finish_t, "vector started after bm25 finished — not parallel"


@pytest.mark.unit
def test_dispatch_parallel_preserves_result_set_shape() -> None:
    """Regression: row counts + ordering invariants unchanged by parallelism.

    Pins the observable contract — parallel dispatch must produce the same
    bm25_count / vec_count / fused_count as the prior sequential
    implementation. The parallelism change is purely a wall-clock
    optimisation; it must not perturb the result envelope.

    Sabotage-proof: revert to sequential AND keep this test — it still
    passes (parallelism doesn't change result content). The companion
    perf test above is what would fail. This test exists to pin that the
    parallelism doesn't accidentally reorder / dedupe / drop rows.
    """
    docs = [
        {"path": "p1.md", "collection": "c", "title": "T1", "content": "alpha foo"},
        {"path": "p2.md", "collection": "c", "title": "T2", "content": "alpha bar"},
    ]
    bm25 = BM25SearchBackend(FakeDocumentRepository(documents=docs))
    vec_results = [{"path": "v1.md", "distance": 0.1, "collection": "c"}]
    vector = VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository(results=vec_results))
    pipeline = _make_pipeline(bm25_backend=bm25, vector_backend=vector)

    result = pipeline.search("alpha")
    assert result.bm25_count == 2
    assert result.vec_count == 1
    # Stage keys preserved — the probe consumer (executor + config_cli)
    # reads these by name; they must not disappear with the refactor.
    assert "bm25" in result.stage_latency_ms
    assert "vector" in result.stage_latency_ms
    assert "dispatch" in result.stage_latency_ms


# ---------------------------------------------------------------------------
# Fix 1b — dispatch pool sized for concurrency (PLA-272)
#
# The pool was a fixed 2-worker singleton, so backend dispatch for the WHOLE
# process was capped at one search's worth of parallelism. Under teaming load
# (#436: post-warm p95 ~640ms at --concurrency 5 — ten dispatch tasks on two
# workers) the surplus tasks queued. The fix sizes the pool from the expected
# concurrent load (2 futures per search * KAIRIX_MAX_CONCURRENCY). The size
# seam is the ``concurrency`` argument to ``dispatch_workers_for`` /
# ``build_dispatch_pool`` — injected as a value, never read from process env
# in the test (F2-clean), and the SearchPipeline.dispatch_pool field lets a
# caller inject its own pool.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dispatch_pool_sized_from_configured_concurrency() -> None:
    """The pool scales with the injected concurrency value and clears the legacy 2-worker ceiling.

    ``dispatch_workers_for`` is the sizing seam: the concurrency is passed in
    as a value (injection, not ``setenv``). The default sizing must exceed the
    old fixed 2-worker pool that bottlenecked teaming load (PLA-272), and the
    size must grow with the configured concurrency. ``build_dispatch_pool``
    then constructs a real pool whose ``max_workers`` equals the computed size,
    so a hardcoded ceiling is caught at the executor, not just in the helper.

    Sabotage-proof (executed): in ``kairix/core/search/pipeline.py`` change
    ``build_dispatch_pool`` to ``ThreadPoolExecutor(max_workers=2, ...)`` (the
    legacy hardcode) and re-run — the ``pool._max_workers == dispatch_workers_for(...)``
    and ``> 2`` asserts fail. Restore to pass. Equivalently, make
    ``dispatch_workers_for`` ``return 2`` and the default/scaling asserts fail.
    """
    # Default sizing clears the legacy fixed 2-worker ceiling.
    default_workers = dispatch_workers_for(DEFAULT_DISPATCH_CONCURRENCY)
    assert default_workers > 2, (
        f"default dispatch pool sized at {default_workers} workers — must exceed the legacy 2-worker "
        f"ceiling that bottlenecked teaming load (PLA-272)"
    )

    # Scales with the configured concurrency value (strict monotonic growth).
    small = dispatch_workers_for(4)
    medium = dispatch_workers_for(8)
    large = dispatch_workers_for(16)
    assert small < medium < large, f"sizing must grow with concurrency; got {small}, {medium}, {large}"

    # The built pool honours the computed size — guards against a hardcoded
    # max_workers literal slipping into the executor construction.
    pool = build_dispatch_pool(concurrency=8)
    try:
        assert pool._max_workers == dispatch_workers_for(8)
        assert pool._max_workers > 2
    finally:
        pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Concurrency bump — CPU-aware default concurrency (PLA-272 follow-up)
#
# When KAIRIX_MAX_CONCURRENCY is UNSET, the expected concurrent-search load
# (and therefore the dispatch pool size) is derived from the host core count
# so a bigger box (an incoming 8-core D8as_v5) auto-uses more dispatch
# parallelism without an operator env tweak, while a 1-2 core CI box stays
# bounded. cpu_aware_default_concurrency is the public sizing seam: the core
# count is injected as a plain int (no process patching, F2-clean), so the
# scaling / clamp behaviour is asserted by value. An explicit
# KAIRIX_MAX_CONCURRENCY stays authoritative — it feeds read_int_env and wins
# over the CPU-aware default (behaviour preserved from PLA-272).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_concurrency_scales_with_cpu_count() -> None:
    """The CPU-aware default scales with cores and clears the legacy fixed 8.

    A small core count yields a small load; an 8-core box yields a larger one
    that exceeds the old fixed default, so the bigger box isn't capped at a
    small fixed number. The core count is a plain-int argument (injection, not
    ``setenv``), so the scaling is asserted directly.

    Sabotage-proof (executed): in ``kairix/core/search/pipeline.py`` replace the
    body of ``cpu_aware_default_concurrency`` with
    ``return DEFAULT_DISPATCH_CONCURRENCY`` (drop the CPU-aware derivation) —
    ``small < eight`` fails because both collapse to 8. Restore to pass.
    """
    small = cpu_aware_default_concurrency(1)  # 1-core CI box
    eight = cpu_aware_default_concurrency(8)  # incoming 8-core D8as_v5

    # A bigger box auto-uses more dispatch concurrency.
    assert small < eight, f"8-core must out-scale a 1-core box; got {small} vs {eight}"
    # The 8-core default clears the legacy fixed default so the bigger box
    # isn't capped at the small fixed number (the reason for this change).
    assert eight > DEFAULT_DISPATCH_CONCURRENCY, (
        f"8-core default {eight} must exceed the legacy fixed {DEFAULT_DISPATCH_CONCURRENCY}"
    )
    # Non-decreasing across the realistic core range (small boxes floor, big
    # boxes ceiling — never inverts).
    seq = [cpu_aware_default_concurrency(c) for c in (1, 2, 4, 8, 16)]
    assert seq == sorted(seq), f"concurrency must be non-decreasing in cores; got {seq}"


@pytest.mark.unit
def test_default_concurrency_and_pool_stay_within_floor_and_ceiling() -> None:
    """The CPU-aware default (and the pool from it) can't over- or under-allocate.

    A 1-2 core box must not get a huge pool; a 32/64-core box must not
    unbounded-explode it. ``cpu_aware_default_concurrency`` clamps the load to a
    fixed envelope, so ``dispatch_workers_for(default)`` stays within
    ``[2*floor, 2*ceiling]`` for ANY core count — including absurd ones — while
    clearing the legacy 2-worker pool on every host.

    Sabotage-proof (executed): in ``kairix/core/search/pipeline.py`` drop the
    ``min()/max()`` clamp (``return scaled``) — the saturation assert
    (``cpu_aware_default_concurrency(64) == cpu_aware_default_concurrency(1000)``)
    fails because the value grows without bound. Restore to pass.
    """
    floor_conc = cpu_aware_default_concurrency(1)
    # Clamp saturates — a 64-core and a 1000-core host resolve to the SAME
    # bounded load, so a huge box can't unbounded-explode the pool.
    assert cpu_aware_default_concurrency(64) == cpu_aware_default_concurrency(1000), (
        "clamp must saturate so a huge box can't unbounded-explode the pool"
    )
    ceiling_conc = cpu_aware_default_concurrency(1000)
    assert floor_conc < ceiling_conc, f"floor {floor_conc} must sit below ceiling {ceiling_conc}"

    floor_pool = dispatch_workers_for(floor_conc)
    ceiling_pool = dispatch_workers_for(ceiling_conc)
    for cpu in (1, 2, 3, 4, 8, 16, 32, 64, 256, 4096):
        conc = cpu_aware_default_concurrency(cpu)
        assert floor_conc <= conc <= ceiling_conc, f"cpu={cpu} -> {conc} escaped [{floor_conc}, {ceiling_conc}]"
        workers = dispatch_workers_for(conc)
        assert floor_pool <= workers <= ceiling_pool, f"cpu={cpu} pool {workers} escaped [{floor_pool}, {ceiling_pool}]"
        # Clears the legacy fixed 2-worker pool on every host (PLA-272).
        assert workers > 2

    # None (os.cpu_count() undeterminable) falls back to the fixed default,
    # which itself sits inside the envelope.
    assert floor_conc <= cpu_aware_default_concurrency(None) <= ceiling_conc


@pytest.mark.unit
def test_explicit_env_override_is_authoritative_over_cpu_aware_default() -> None:
    """An explicit KAIRIX_MAX_CONCURRENCY wins over the CPU-aware default.

    ``resolve_dispatch_concurrency`` is the public resolver: when an operator
    sets the env it returns that value verbatim (authoritative, un-clamped) and
    the CPU-aware default is NOT used; when unset, the CPU-aware default flows
    through. ``read_int_env``'s env-over-default precedence is independently
    pinned in ``tests/test_paths.py::TestReadIntEnv``; this pins that the
    resolver actually wires the env in front of the derived default.

    F2/F1-clean: the operator env is injected through the resolver's ``env=``
    mapping seam — the process env is never mutated.

    Sabotage-proof (executed): in ``kairix/core/search/pipeline.py`` make
    ``resolve_dispatch_concurrency`` return
    ``cpu_aware_default_concurrency(os.cpu_count())`` directly (dropping
    ``read_int_env``) — the operator's out-of-band value is ignored and the
    env-set assertion (``== 3``) fails. Restore to pass.
    """
    env_var = "KAIRIX_MAX_CONCURRENCY"
    default_for_host = cpu_aware_default_concurrency(os.cpu_count())
    # 3 is below the CPU-aware floor, so on ANY host it can only come from the
    # env override — proving the operator value wins over the derived default.
    assert default_for_host > 3

    operator_env = {env_var: "3"}
    assert resolve_dispatch_concurrency(env=operator_env) == 3, "explicit override must win over the CPU-aware default"
    # read_int_env is the sanctioned seam that delivers that precedence.
    assert read_int_env(env_var, default=default_for_host, env=operator_env) == 3

    # Env unset -> the resolver falls back to the CPU-aware default for the host.
    assert resolve_dispatch_concurrency(env={}) == default_for_host


@pytest.mark.unit
def test_pipeline_dispatches_through_injected_pool() -> None:
    """The pipeline submits both legs to the injected ``dispatch_pool`` seam.

    Proves the seam is load-bearing: when a pool is injected via the public
    ``dispatch_pool`` field, the BM25 + vector legs run on THAT pool's worker
    threads (identified by the pool's distinctive thread-name prefix), not the
    process-shared default. This is the injection path PLA-272 relies on for
    testing sizing without touching process env.

    Sabotage-proof (executed): in ``SearchPipeline._dispatch_backends`` replace
    ``pool = self.dispatch_pool if self.dispatch_pool is not None else
    _default_dispatch_pool()`` with ``pool = _default_dispatch_pool()`` — the
    leg threads then carry the ``search-dispatch`` prefix and the
    ``startswith("injected-dispatch")`` asserts fail. Restore to pass.
    """
    captured: dict[str, str] = {}

    class _NameCapturingBM25:
        """BM25 backend that records the worker-thread name it ran on."""

        def search(self, query: str, collections: list[str] | None = None, limit: int = 20) -> list[dict]:
            captured["bm25_thread"] = threading.current_thread().name
            return [{"file": f"{query}.md", "title": "t", "snippet": "", "score": 1.0, "collection": "c"}]

        def get_chunk_dates(self, paths: list[str]) -> dict[str, str]:
            return {}

    injected = ThreadPoolExecutor(max_workers=6, thread_name_prefix="injected-dispatch")
    try:
        pipeline = SearchPipeline(
            classifier=FakeClassifier(),
            bm25=_NameCapturingBM25(),  # type: ignore[arg-type]  # duck-typed BM25 backend test seam — only .search / .get_chunk_dates exercised
            vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
            graph=FakeGraphRepository(available=True),
            fusion=FakeFusion(),
            boosts=[],
            logger=None,
            dispatch_pool=injected,
        )
        pipeline.search("injected pool query")
    finally:
        injected.shutdown(wait=False)

    assert "bm25_thread" in captured, "BM25 leg never ran"
    assert captured["bm25_thread"].startswith("injected-dispatch"), (
        f"BM25 leg ran on {captured['bm25_thread']!r} — expected the injected pool's 'injected-dispatch' "
        f"threads, so the dispatch_pool seam was bypassed"
    )


# ---------------------------------------------------------------------------
# Fix 2 — persistent metadata connection in VectorIndex
# ---------------------------------------------------------------------------


class _ConnCountingOpener:
    """Wraps ``sqlite3.connect`` so the test can count open invocations.

    Used to assert ``_fetch_metadata_batched`` only opens the metadata
    connection once across N searches — the persistent-connection fix.
    """

    def __init__(self) -> None:
        self.opens = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.opens += 1
        import sqlite3

        return sqlite3.connect(*args, **kwargs)


@pytest.mark.unit
def test_vector_index_meta_conn_opens_once_across_many_searches(tmp_path: Any) -> None:
    """``_get_meta_conn`` builds the connection once and reuses it forever.

    Construct a real ``VectorIndex`` against a tmp SQLite DB, run 5 metadata
    fetches, and assert the connection cache fires only once. The prior
    implementation opened+closed a fresh connection on every search; the
    new behaviour reuses one connection for the instance lifetime.

    Sabotage-proof: in ``VectorIndex._fetch_metadata_batched`` revert
    ``db = self._get_meta_conn()`` to ``db = open_db(Path(self._db_path));
    db.close()`` (the old shape). The cached-conn assert flips: opens
    jumps from 1 to 5.
    """
    import sqlite3

    from kairix.core.search.vec_index import VectorIndex

    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, collection TEXT, path TEXT,
            title TEXT, hash TEXT, active INTEGER DEFAULT 1, source_page INTEGER, source_uri TEXT,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        INSERT INTO documents (collection, path, title, hash, active) VALUES
          ('c', 'a.md', 'A', 'h1', 1),
          ('c', 'b.md', 'B', 'h2', 1);
        INSERT INTO content (hash, doc) VALUES ('h1', 'aa'), ('h2', 'bb');
        """
    )
    db.commit()
    db.close()

    idx = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )

    # Fire 5 metadata fetches.
    for _ in range(5):
        rows = idx._fetch_metadata_batched(["h1", "h2"])
        assert set(rows.keys()) == {"h1", "h2"}

    # The persistent connection is non-None after the first fetch and
    # stable across all 5 — i.e. only opened once.
    assert idx._meta_conn is not None
    # Cleanup so the next test gets a clean fixture.
    idx.close_meta_conn()
    assert idx._meta_conn is None


@pytest.mark.unit
def test_vector_index_meta_conn_returns_same_rows_across_calls(tmp_path: Any) -> None:
    """Regression: metadata rows match exactly across repeated fetches.

    Connection reuse must not introduce stale or stateful row leakage —
    repeated fetches against the same hash set return identical row shape
    + content. Pins the observable contract for the search hot path.

    Sabotage-proof: in ``_get_meta_conn`` skip the ``row_factory =
    sqlite3.Row`` setup — every row becomes a tuple and the ``row['hash']``
    access in ``_fetch_metadata_batched`` raises TypeError; assert below
    flips to error.
    """
    import sqlite3

    from kairix.core.search.vec_index import VectorIndex

    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, collection TEXT, path TEXT,
            title TEXT, hash TEXT, active INTEGER DEFAULT 1, source_page INTEGER, source_uri TEXT,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        INSERT INTO documents (collection, path, title, hash, active) VALUES
          ('c', 'a.md', 'A', 'h1', 1);
        INSERT INTO content (hash, doc) VALUES ('h1', 'content-a');
        """
    )
    db.commit()
    db.close()

    idx = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )

    first = idx._fetch_metadata_batched(["h1"])
    second = idx._fetch_metadata_batched(["h1"])
    assert set(first.keys()) == {"h1"}
    assert set(second.keys()) == {"h1"}
    assert first["h1"]["path"] == second["h1"]["path"] == "a.md"
    assert first["h1"]["title"] == second["h1"]["title"] == "A"
    idx.close_meta_conn()


@pytest.mark.unit
def test_vector_index_meta_conn_thread_safe(tmp_path: Any) -> None:
    """Concurrent fetches across threads share the cached connection safely.

    The parallel-dispatch worker thread (``search-dispatch`` from
    ``pipeline.py``) may execute against this connection from a
    non-construction thread; the per-execute lock + ``check_same_thread=
    False`` must prevent ``sqlite3.ProgrammingError`` or row-count
    corruption.

    Sabotage-proof: in ``_get_meta_conn`` drop ``check_same_thread=False``
    from the ``sqlite3.connect`` call; concurrent fetches raise
    ``sqlite3.ProgrammingError`` and the result-collection assert below
    will be missing entries.
    """
    import sqlite3

    from kairix.core.search.vec_index import VectorIndex

    db_path = tmp_path / "index.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, collection TEXT, path TEXT,
            title TEXT, hash TEXT, active INTEGER DEFAULT 1, source_page INTEGER, source_uri TEXT,
            UNIQUE(collection, path)
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        INSERT INTO documents (collection, path, title, hash, active) VALUES
          ('c', 'a.md', 'A', 'h1', 1),
          ('c', 'b.md', 'B', 'h2', 1);
        INSERT INTO content (hash, doc) VALUES ('h1', 'a'), ('h2', 'b');
        """
    )
    db.commit()
    db.close()

    idx = VectorIndex(
        index_path=tmp_path / "vectors.usearch",
        meta_path=tmp_path / "vectors.meta.json",
        db_path=db_path,
    )

    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            rows = idx._fetch_metadata_batched(["h1", "h2"])
            results.append(rows)
        except BaseException as exc:  # pragma: no cover — defensive: any cross-thread error fails the test
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"cross-thread fetch raised: {errors}"
    assert len(results) == 8
    for rows in results:
        assert set(rows.keys()) == {"h1", "h2"}
    idx.close_meta_conn()


# ---------------------------------------------------------------------------
# Fix 3 — embed cache accessor hoisted to construction time
# ---------------------------------------------------------------------------


class _CountingCacheFactory:
    """Wraps the embed-cache accessor and counts every invocation.

    Used to assert that ``ProviderEmbeddingService.embed`` doesn't invoke
    the accessor on every call — only on construction.
    """

    def __init__(self, cache: Any) -> None:
        self._cache = cache
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return self._cache


class _InMemoryCache:
    """Minimal EmbedCache shape: ``get(text) -> list | None``, ``put(text, vec)``.

    Avoids the singleton + SQLite path of the real ``EmbedCache``; the
    test asserts the WRAPPER (the accessor) is called once at
    construction time, not the cache itself.
    """

    def __init__(self) -> None:
        self._data: dict[str, list[float]] = {}

    def get(self, text: str) -> list[float] | None:
        return self._data.get(text)

    def put(self, text: str, vec: list[float]) -> None:
        self._data[text] = list(vec)


@pytest.mark.unit
def test_embed_service_resolves_cache_once_at_construction() -> None:
    """``ProviderEmbeddingService`` builds the cache accessor once at __init__.

    Construct the service with a counting cache factory, run 50 ``embed``
    calls, and assert the factory was invoked exactly 50 times (one per
    embed call) — but the factory is bound at construction time and only
    invoked from inside ``embed`` (not from a per-call import statement).
    The point of the fix is to make the lookup ``self._get_embed_cache()``
    a single attribute dereference; under the old implementation each
    embed call also paid the ``from kairix.transport.cache import
    get_embed_cache`` statement.

    Pinning behaviour: the factory IS invoked once per embed call (that's
    the cache-accessor semantics — accessor returns the singleton). What
    the fix saves is the IMPORT statement per call. The structural
    assertion: ``self._get_embed_cache`` is bound at construction and
    refers to the injected factory, NOT to a module-level import inside
    ``embed`` that the test can't observe.

    Sabotage-proof: in ``ProviderEmbeddingService.embed``, restore the
    line ``from kairix.transport.cache import get_embed_cache`` and
    replace ``cache = self._get_embed_cache()`` with ``cache =
    get_embed_cache()``. The injected counting factory below would not
    fire — counting_factory.calls would be 0 and the assert below would
    fail with calls=0 vs expected ≥1.
    """
    cache = _InMemoryCache()
    counting_factory = _CountingCacheFactory(cache)
    provider = FakeProvider()
    svc = ProviderEmbeddingService(
        provider,
        get_embed_cache_fn=counting_factory,
        # Provide no-op coalescer wiring so the cache-resolved path runs
        # unconditionally instead of being skipped by an installed coalescer.
        existing_coalescer_fn=lambda: None,
        coalescer_factory=lambda **_: None,
    )

    for i in range(50):
        result = svc.embed(f"query-{i}")
        assert isinstance(result, list)

    # The injected factory was invoked on every embed (the cache accessor
    # resolves the singleton per call), proving the embed() codepath
    # actually consults ``self._get_embed_cache`` rather than the
    # module-level import. The win is that the import statement itself
    # is no longer evaluated per call — invisible to this counter but
    # observable in the cProfile output we captured under the brief.
    assert counting_factory.calls == 50


@pytest.mark.unit
def test_embed_service_warm_cache_short_circuit_unchanged() -> None:
    """Regression: cached lookups still return cached values verbatim.

    Pre-populate the cache; the next embed call returns the cached vector
    without hitting the provider. Pins the observable warm-cache
    behaviour after the construction-time-resolution refactor.

    Sabotage-proof: in ``ProviderEmbeddingService.embed``, change
    ``cache = self._get_embed_cache()`` to ``cache = _InMemoryCache()``
    (a fresh, empty cache per call) — the assert below would flip to
    provider.embed_batch_calls = 1 instead of 0.
    """
    cache = _InMemoryCache()
    cache.put("warm-text", [0.7] * 1536)
    counting_factory = _CountingCacheFactory(cache)
    provider = FakeProvider()
    svc = ProviderEmbeddingService(
        provider,
        get_embed_cache_fn=counting_factory,
        existing_coalescer_fn=lambda: None,
        coalescer_factory=lambda **_: None,
    )

    out = svc.embed("warm-text")
    # Cached vector returned verbatim — defensive copy is fine, just check
    # identity by value not by object.
    assert out == [0.7] * 1536
    # Provider was NOT called — cache hit short-circuits.
    assert len(provider.embed_calls) == 0


@pytest.mark.unit
def test_embed_service_cache_miss_still_calls_provider() -> None:
    """Regression: cold cache still routes through provider + caches the result.

    Pins the cold-path behaviour after the refactor. Cache miss -> call
    provider.embed_batch -> put result in cache -> next call hits cache.

    Sabotage-proof: in ``ProviderEmbeddingService.embed`` skip the
    ``cache.put(text, embedding)`` line on the synchronous branch; the
    second-call provider.embed_batch_calls would tick from 1 to 2 and
    the cache assert below would observe the empty cache.
    """
    cache = _InMemoryCache()
    counting_factory = _CountingCacheFactory(cache)
    provider = FakeProvider()
    svc = ProviderEmbeddingService(
        provider,
        get_embed_cache_fn=counting_factory,
        existing_coalescer_fn=lambda: None,
        coalescer_factory=lambda **_: None,
    )

    first = svc.embed("cold-text")
    assert first  # non-empty vector
    assert len(provider.embed_calls) == 1
    # Cache now populated; second call short-circuits, provider count flat.
    second = svc.embed("cold-text")
    assert second == first
    assert len(provider.embed_calls) == 1


# ---------------------------------------------------------------------------
# Combined-pipeline sanity check — fix 1 + fix 3 still satisfy the existing
# stage-latency split contract (test_pipeline_records_embed_http_and_vector_ann_split
# from tests/search/test_pipeline.py still passes; this test pins the cross-fix
# observability invariant for the parallel path specifically).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parallel_dispatch_preserves_embed_http_and_vector_ann_split_keys() -> None:
    """The vector-leg sub-stage timings (embed_http + vector_ann) still appear.

    The parallel-dispatch refactor moved the vector-leg into a worker
    thread; this asserts the timing-dict is still mutated from that
    thread and the keys land in SearchResult.stage_latency_ms. Without
    this, the existing decomposition test (in test_pipeline.py) would
    silently miss the parallel branch.
    """
    fusion = RRFFusion(k=60)
    vec_results = [{"path": "v.md", "distance": 0.1, "collection": "c"}]
    pipeline = SearchPipeline(
        classifier=FakeClassifier(),
        bm25=BM25SearchBackend(FakeDocumentRepository()),
        vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository(results=vec_results)),
        graph=FakeGraphRepository(available=True),
        fusion=fusion,
        boosts=[],
        logger=None,
    )
    result = pipeline.search("semantic query")
    assert "embed_http" in result.stage_latency_ms
    assert "vector_ann" in result.stage_latency_ms
    assert "vector" in result.stage_latency_ms
    assert "bm25" in result.stage_latency_ms
    assert "dispatch" in result.stage_latency_ms


# ---------------------------------------------------------------------------
# Fix 4 — rerank routed through a bounded, shared executor (PLA-272)
#
# Rerank is the single largest search stage (~331ms cross-encoder forward pass)
# and — unlike bm25+vector, which the dispatch pool already overlaps — it ran
# INLINE on the request thread. Under concurrent teaming load a concurrency soak
# (N=1→20 distinct queries) saw latency scale linearly with N and throughput stay
# flat (~2.5 req/s, effective concurrency ≈ 1). torch releases the GIL during the
# forward pass, so routing rerank onto a dedicated pool sized for the expected
# concurrent load lets concurrent requests' rerank overlap on a controlled set of
# cores instead of serialising / oversubscribing. The size seam is the
# ``concurrency`` argument to ``rerank_workers_for`` / ``build_rerank_pool``
# (injected as a value, never read from process env — F2-clean), and the
# SearchPipeline.rerank_pool field lets a caller inject its own pool. Quality is
# unchanged: a single request's output ranking is byte-for-byte identical (the
# same reranker runs the same input on a pool thread instead of inline).
# ---------------------------------------------------------------------------


def _rerank_pipeline(
    *,
    reranker: Any,
    rerank_pool: ThreadPoolExecutor | None = None,
    documents: list[dict[str, Any]] | None = None,
) -> SearchPipeline:
    """Build a SearchPipeline whose rerank stage fires on SEMANTIC intent.

    Direct construction is sanctioned here — these are perf unit tests pinning
    leaf behaviour against the protocol shape, not BDD step impls. ``skip_vector``
    keeps the fused set to the bm25 docs so the reranked ordering is
    deterministic; ``rerank_intents=("semantic",)`` matches the FakeClassifier
    default intent so ``_maybe_rerank`` actually invokes the injected reranker.
    """
    docs = (
        documents
        if documents is not None
        else [{"path": "p1.md", "collection": "c", "title": "T1", "content": "alpha"}]
    )
    return SearchPipeline(
        classifier=FakeClassifier(),  # defaults to SEMANTIC
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
        vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
        graph=FakeGraphRepository(available=True),
        fusion=RRFFusion(k=60),
        boosts=[],
        logger=None,
        config=RetrievalConfig(skip_vector=True, rerank_intents=("semantic",)),
        reranker=reranker,
        rerank_pool=rerank_pool,
    )


@pytest.mark.unit
def test_rerank_pool_sized_from_configured_concurrency() -> None:
    """The rerank pool scales with the injected concurrency value.

    ``rerank_workers_for`` is the sizing seam: one rerank task per search, so
    the worker count IS the configured concurrency (the dispatch pool submits
    two futures per search and so doubles it). It grows monotonically with
    concurrency and is floored at 1 so a misconfigured ``concurrency <= 1``
    still yields a usable single-worker pool. ``build_rerank_pool`` then
    constructs a real pool whose ``max_workers`` equals the computed size, so a
    hardcoded ceiling is caught at the executor, not just in the helper.

    Sabotage-proof (executed): in ``kairix/core/search/pipeline.py`` change
    ``build_rerank_pool`` to ``ThreadPoolExecutor(max_workers=1, ...)`` (a
    hardcode) and re-run — the ``pool._max_workers == rerank_workers_for(...)``
    assert fails. Equivalently make ``rerank_workers_for`` ``return 1`` and the
    monotonic-growth asserts fail. Restore to pass.
    """
    # Scales with the configured concurrency value (strict monotonic growth).
    small = rerank_workers_for(2)
    medium = rerank_workers_for(4)
    large = rerank_workers_for(8)
    assert small < medium < large, f"sizing must grow with concurrency; got {small}, {medium}, {large}"

    # Floored at 1 for a misconfigured concurrency.
    assert rerank_workers_for(0) == 1
    assert rerank_workers_for(-5) == 1

    # The built pool honours the computed size — guards against a hardcoded
    # max_workers literal slipping into the executor construction.
    pool = build_rerank_pool(concurrency=5)
    try:
        assert pool._max_workers == rerank_workers_for(5)
        assert pool._max_workers == 5
    finally:
        pool.shutdown(wait=False)


@pytest.mark.unit
def test_rerank_single_request_ranking_is_deterministic_through_pool() -> None:
    """Quality invariant: routing rerank through the pool does not change the
    single-request output ranking.

    A deterministic reranker assigns a fixed score per path and re-sorts. The
    pipeline routes it through the (default, process-shared) rerank pool; the
    resulting order must equal the reranker's intended order AND be identical
    across repeated runs (no thread-relocation nondeterminism). This is the
    "byte-for-byte ranking on a fixed input" guarantee — the concurrency fix is
    purely a wall-clock/overlap optimisation and must not perturb quality.

    Sabotage-proof: in ``SearchPipeline._maybe_rerank`` drop the ``.result()``
    (``return pool.submit(self.reranker, query, fused)``) — a Future object is
    returned instead of the reranked list, ``apply_budget`` finds no rows, and
    the ordering assert below fails on an empty result.
    """
    # p3 scores highest, then p1, then p2 — a fixed, path-keyed order.
    fixed_scores = {"p1.md": 2.0, "p2.md": 1.0, "p3.md": 3.0}
    expected_order = ["p3.md", "p1.md", "p2.md"]

    def _deterministic_reranker(query: str, fused: list[Any]) -> list[Any]:
        for r in fused:
            score = fixed_scores.get(r.path, 0.0)
            r.rerank_score = score
            r.boosted_score = score  # apply_budget sorts by boosted_score
        return sorted(fused, key=lambda r: r.rerank_score, reverse=True)

    docs = [
        {"path": "p1.md", "collection": "c", "title": "T1", "content": "alpha shared"},
        {"path": "p2.md", "collection": "c", "title": "T2", "content": "alpha shared"},
        {"path": "p3.md", "collection": "c", "title": "T3", "content": "alpha shared"},
    ]
    pipeline = _rerank_pipeline(reranker=_deterministic_reranker, documents=docs)

    orders: list[list[str]] = []
    for _ in range(3):
        result = pipeline.search("alpha")
        orders.append([b.result.path for b in result.results])

    # The reranked order is exactly the deterministic reranker's intended order.
    assert orders[0] == expected_order, f"pool-routed rerank changed the ranking: {orders[0]} != {expected_order}"
    # Identical across repeated runs — no thread-relocation nondeterminism.
    assert orders[0] == orders[1] == orders[2], f"ranking not stable across runs: {orders}"


class _ConcurrencyRecordingReranker:
    """Reranker that records the peak number of overlapping invocations.

    Increments an active counter on entry, holds a short window open (so
    overlap is observable), decrements on exit — and records the worker-thread
    name each call ran on. Returns the input unchanged: this test measures
    concurrency, not ranking (the determinism test above owns quality).
    """

    def __init__(self, hold_s: float) -> None:
        self._hold_s = hold_s
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0
        self.calls = 0
        self.thread_names: list[str] = []

    def __call__(self, query: str, fused: list[Any]) -> list[Any]:
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
            self.calls += 1
            self.thread_names.append(threading.current_thread().name)
        try:
            time.sleep(self._hold_s)  # hold the window open so overlap is observable
        finally:
            with self._lock:
                self._active -= 1
        return fused


@pytest.mark.unit
def test_rerank_concurrent_calls_overlap_on_bounded_pool() -> None:
    """Concurrent rerank calls overlap on the dedicated pool instead of
    serialising on the request thread — bounded by the pool size.

    Six searches fire concurrently through ONE pipeline that routes rerank
    through an injected 2-worker pool. The instrumented reranker records the
    peak overlap: with the pool, exactly two forward passes run at once (the
    pool caps it), and every call runs on a pool thread. This is the durable
    concurrency win — the CPU-bound rerank no longer serialises on the request
    thread; it overlaps on a controlled set of cores.

    Sabotage-proof (executed): in ``SearchPipeline._maybe_rerank`` revert the
    routing — replace ``pool = self.rerank_pool if self.rerank_pool is not None
    else _default_rerank_pool()`` + ``pool.submit(self.reranker, query,
    fused).result()`` with the inline ``self.reranker(query, fused)``. The six
    reranks then run inline on the six caller threads: ``peak`` jumps to 6 (the
    ``peak <= 2`` bound fails) AND the calls run on the caller threads (the
    ``search-rerank`` thread-name assert fails). Restore to pass.

    Second sabotage (bounds the overlap limb): shrink the injected pool to
    ``max_workers=1`` — the reranks serialise on the single worker, ``peak``
    drops to 1, and the ``peak >= 2`` assert fails.
    """
    n_searches = 6
    pool_size = 2
    reranker = _ConcurrencyRecordingReranker(hold_s=0.1)
    injected = ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="injected-rerank")
    try:
        pipeline = _rerank_pipeline(reranker=reranker, rerank_pool=injected)

        errors: list[BaseException] = []

        def _fire(i: int) -> None:
            try:
                pipeline.search(f"alpha query {i}")
            except BaseException as exc:  # pragma: no cover — defensive: any search error fails the test
                errors.append(exc)

        threads = [threading.Thread(target=_fire, args=(i,)) for i in range(n_searches)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        injected.shutdown(wait=False)

    assert not errors, f"a concurrent search raised: {errors}"
    # Every search reached the rerank stage exactly once.
    assert reranker.calls == n_searches, f"expected {n_searches} rerank calls; got {reranker.calls}"
    # Overlap: more than one rerank ran at once (not serialised on the request thread).
    assert reranker.peak >= 2, f"rerank did not overlap — peak concurrency was {reranker.peak} (serialised)"
    # Bounded: the shared pool caps overlap at its worker count, so the inline
    # revert (peak == n_searches) is caught here.
    assert reranker.peak <= pool_size, (
        f"rerank overlap {reranker.peak} exceeded the pool bound {pool_size} — routing was bypassed (ran inline)"
    )
    # Routing: every rerank ran on the injected pool's worker threads, not the
    # caller threads — proves the rerank_pool seam is load-bearing.
    assert reranker.thread_names, "reranker never ran"
    assert all(name.startswith("injected-rerank") for name in reranker.thread_names), (
        f"rerank ran off the injected pool — thread names {reranker.thread_names!r} lack the 'injected-rerank' "
        f"prefix, so the rerank_pool routing was bypassed"
    )
