"""Unit tests for ``EmbedDependencies`` (refactored per #204).

The deps dataclass uses ``default_factory`` to bind real production
callables without making fields ``Optional``. Tests here cover the
public surface only (no imports from the private ``_deps_defaults``
sibling module — F5):

  - Direct construction with explicit fakes — the test-time path.
  - Default construction returns a dataclass whose fields are all
    callable production wrappers.
  - Production-default behaviour exercised by *calling* the
    auto-bound callables against the real embed/schema/paths code —
    the network-bound ones through the embed module's ``client=`` seam.

The default-callable tests verify the wiring via the public renamed
helpers ``get_azure_config_from_credentials`` /
``open_default_usearch_index`` on ``kairix.core.embed.embed``.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from kairix.core.embed.deps import EmbedDependencies

# ── EmbedDependencies — explicit fake injection ───────────────────────


@pytest.mark.unit
def test_deps_accepts_explicit_callables_for_every_field() -> None:
    """Every field stores the exact callable the test passed in.

    Sabotage proof: if the dataclass started silently overriding a
    user-supplied callable, this would assert against a different
    object identity.
    """

    def get_cfg() -> tuple[str, str, str]:
        return ("k", "e", "d")

    def preflight(_a: str, _b: str, _c: str) -> int:
        return 1536

    def embed(_texts: list[str], *_a: object, **_kw: object) -> list[list[float]]:
        return []

    def open_idx() -> object | None:
        return None

    def migrate(_db: sqlite3.Connection) -> None:
        return None

    def doc_root() -> str | None:
        return "/fake/root"

    deps = EmbedDependencies(
        get_azure_config=get_cfg,
        preflight_check=preflight,
        embed_batch=embed,
        open_usearch_index=open_idx,
        migrate_content_vectors=migrate,
        get_document_root=doc_root,
    )

    assert deps.get_azure_config is get_cfg
    assert deps.preflight_check is preflight
    assert deps.embed_batch is embed
    assert deps.open_usearch_index is open_idx
    assert deps.migrate_content_vectors is migrate
    assert deps.get_document_root is doc_root


@pytest.mark.unit
def test_deps_with_no_args_binds_callable_production_defaults() -> None:
    """Default construction wires every field to a callable wrapper.

    Sabotage proof: a future commit that drops ``default_factory`` on
    one of these fields (e.g. reverts to ``Optional[Callable] = None``)
    would leave the field as ``None`` and ``callable(...)`` would fail.
    This is exactly the regression that broke ``mypy --strict``
    (see #204 commit ``afd07324``) — the new shape is verified here.
    """
    deps = EmbedDependencies()

    assert callable(deps.get_azure_config)
    assert callable(deps.preflight_check)
    assert callable(deps.embed_batch)
    assert callable(deps.open_usearch_index)
    assert callable(deps.migrate_content_vectors)
    assert callable(deps.get_document_root)


@pytest.mark.unit
def test_deps_partial_override_keeps_other_defaults() -> None:
    """Overriding one field leaves the others bound to production defaults.

    This is the realistic test-time pattern — most tests only swap
    ``embed_batch`` and ``preflight_check`` to avoid Azure, leaving the
    other fields as their default-factory production wrappers.
    """

    def fake_embed(_texts: list[str], *_a: object, **_kw: object) -> list[list[float]]:
        return [[0.5] * 1536]

    deps = EmbedDependencies(embed_batch=fake_embed)

    # The override sticks.
    assert deps.embed_batch is fake_embed
    # The non-overridden fields remain callable production wrappers
    # (different identity from ``fake_embed`` — sabotage proof against
    # accidental cross-binding).
    assert deps.get_azure_config is not fake_embed
    assert deps.preflight_check is not fake_embed
    assert deps.open_usearch_index is not fake_embed
    assert deps.migrate_content_vectors is not fake_embed
    assert deps.get_document_root is not fake_embed
    assert callable(deps.get_azure_config)
    assert callable(deps.preflight_check)


@pytest.mark.unit
def test_deps_two_instances_share_default_callables() -> None:
    """Two ``EmbedDependencies()`` instances see the same default
    callables (the ``default_factory`` returns the same module-level
    function reference each time).

    Sabotage proof: if a refactor wrapped ``default_factory`` so each
    instance got a fresh closure, the identity check would fail and
    the production default would not be a stable reference.
    """
    d1 = EmbedDependencies()
    d2 = EmbedDependencies()

    assert d1.get_azure_config is d2.get_azure_config
    assert d1.preflight_check is d2.preflight_check
    assert d1.embed_batch is d2.embed_batch
    assert d1.open_usearch_index is d2.open_usearch_index
    assert d1.migrate_content_vectors is d2.migrate_content_vectors
    assert d1.get_document_root is d2.get_document_root


# ── Default-callable behaviour — driven through the real production path ──
#
# Each default wrapper is exercised for real: the network-bound ones through
# the embed module's own ``client=`` seam (threaded via ``**kwargs``), the
# local ones against an in-memory SQLite / the hermetic session env. No
# kairix module attribute is swapped (F1).


class _RecordingEmbeddingItem:
    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class _RecordingEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, *, model: str, input: list[str], dimensions: int) -> Any:
        self.calls.append({"model": model, "input": list(input), "dimensions": dimensions})

        return SimpleNamespace(data=[_RecordingEmbeddingItem(i, [0.1] * dimensions) for i in range(len(input))])


class _RecordingOpenAIClient:
    """OpenAI-compatible fake for the embed module's ``client=`` seam."""

    def __init__(self) -> None:
        self.embeddings = _RecordingEmbeddings()


@pytest.mark.unit
def test_default_get_azure_config_resolves_through_embed_credentials() -> None:
    """The default ``get_azure_config`` wrapper resolves the ``embed``
    credentials chain. The hermetic session baseline clears every operator
    provider credential, so the real chain raises the typed
    ``SecretNotFoundError`` (a ``LookupError``) naming the missing secret.

    Sabotage proof: a wrapper that returned a hard-coded tuple instead of
    delegating would not raise and this assertion fails.
    """
    from kairix.secrets import SecretNotFoundError

    deps = EmbedDependencies()
    with pytest.raises(SecretNotFoundError, match="api-key"):
        deps.get_azure_config()


@pytest.mark.unit
def test_default_preflight_check_calls_through_to_embed_module() -> None:
    """The default ``preflight_check`` wrapper delegates to
    ``kairix.core.embed.embed.preflight_check`` — the deployment reaches the
    client and the returned dims are the embedding length.

    Sabotage proof: dropping the ``**kwargs`` pass-through (or the delegation)
    builds a real client against ``https://e`` instead of using the fake.
    """
    client = _RecordingOpenAIClient()
    deps = EmbedDependencies()
    result = deps.preflight_check("k", "https://e", "deploy-x", client=client)

    assert result == client.embeddings.calls[0]["dimensions"]
    assert client.embeddings.calls[0]["model"] == "deploy-x"
    assert client.embeddings.calls[0]["input"] == ["preflight check"]


@pytest.mark.unit
def test_default_embed_batch_calls_through_to_embed_module() -> None:
    """The default ``embed_batch`` wrapper passes texts + deployment + dims
    through to ``kairix.core.embed.embed.embed_batch``.
    """
    client = _RecordingOpenAIClient()
    deps = EmbedDependencies()
    result = deps.embed_batch(["hi"], "k", "https://e", "d", 4, client=client)

    assert result == [[0.1, 0.1, 0.1, 0.1]]
    assert client.embeddings.calls == [{"model": "d", "input": ["hi"], "dimensions": 4}]


@pytest.mark.unit
def test_default_open_usearch_index_is_none_when_worker_writes_disabled() -> None:
    """The default ``open_usearch_index`` wrapper returns what
    ``open_default_usearch_index`` returns — ``None`` under the default
    (unset) ``KAIRIX_WORKER_WRITES_VEC_INDEX`` gate (#335).
    """
    deps = EmbedDependencies()
    assert deps.open_usearch_index() is None


@pytest.mark.unit
def test_default_migrate_content_vectors_applies_schema_migration() -> None:
    """The default ``migrate_content_vectors`` wrapper runs the real
    ``kairix.core.embed.schema.migrate_content_vectors`` against the
    connection — the ``chunk_date`` column is added.

    Sabotage proof: a no-op wrapper leaves the column missing.
    """
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE content_vectors (hash_seq TEXT PRIMARY KEY, embedding BLOB)")
        EmbedDependencies().migrate_content_vectors(db)
        columns = {row[1] for row in db.execute("PRAGMA table_info(content_vectors)")}
    finally:
        db.close()

    assert "chunk_date" in columns


# ── default_get_document_root — tolerated-failure branch ─────────────


# An operator path override naming a home directory that does not exist
# (``~<no-such-user>/...``) makes the real kairix.paths resolver raise
# ``RuntimeError`` from ``Path.expanduser`` — a genuine paths-layer failure,
# driven through the ``env=`` seam instead of swapping ``kairix.paths`` in
# ``sys.modules`` (F1) or writing the process env (F2).
_UNRESOLVABLE_HOME_PATH = "~kairix-no-such-user-zz/kairix"


@pytest.mark.unit
def test_default_get_document_root_returns_none_when_paths_layer_raises() -> None:
    """When the paths layer raises, ``deps.get_document_root()`` returns
    ``None`` (and logs a warning) rather than propagating.

    The embed pipeline only uses the document root for chunk-date
    heuristics; a failing paths layer must not crash the run.

    Sabotage proof: dropping the ``try/except`` in the default wrapper lets
    the ``RuntimeError`` escape and this test errors.
    """
    deps = EmbedDependencies()
    result = deps.get_document_root(env={"KAIRIX_DOCUMENT_ROOT": _UNRESOLVABLE_HOME_PATH})

    assert result is None


@pytest.mark.unit
def test_default_get_document_root_returns_string_on_success() -> None:
    """The default wrapper stringifies the resolved ``kairix.paths.document_root()``.

    Sabotage proof: if the wrapper started returning the Path object
    directly (skipping ``str(...)``), downstream callers expecting a
    string would silently break; this asserts the string contract.
    """
    from kairix.paths import document_root

    result = EmbedDependencies().get_document_root()

    assert isinstance(result, str)
    assert result == str(document_root())


# ── default_get_reflib_index_mode — production-default wrapper (#475) ─


@pytest.mark.unit
def test_default_get_reflib_index_mode_reads_cwd_config(tmp_path: Any, monkeypatch: Any) -> None:
    """The default wrapper resolves ``reference_library.index`` through the
    config resolution chain — a cwd kairix.config.yaml here, same seam the
    validator's cwd test uses (no env mutation, F2-clean).

    Sabotage proof: hard-coding the wrapper to return "eager" fails this.
    """
    (tmp_path / "kairix.config.yaml").write_text(
        "reference_library:\n  index: skip\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    deps = EmbedDependencies()
    assert deps.get_reflib_index_mode() == "skip"


@pytest.mark.unit
def test_default_get_reflib_index_mode_defaults_to_eager_without_config(tmp_path: Any, monkeypatch: Any) -> None:
    """No config file anywhere in the chain → eager (today's behaviour)."""
    monkeypatch.chdir(tmp_path)

    deps = EmbedDependencies()
    assert deps.get_reflib_index_mode() == "eager"


@pytest.mark.unit
def test_deps_default_rate_limit_sleep_is_callable() -> None:
    """The 429 backoff sleeper defaults to a callable (time.sleep) so the
    retry wrapper never needs a None-guard."""
    deps = EmbedDependencies()
    assert callable(deps.rate_limit_sleep)
    assert callable(deps.get_reflib_index_mode)


# ── default_open_embedding_cache — production-default wrapper ─────────


@pytest.mark.unit
def test_default_open_embedding_cache_returns_none_under_pytest() -> None:
    """The default wrapper short-circuits to ``None`` under pytest so test
    fixtures that haven't explicitly injected a cache via
    ``EmbedDependencies(open_embedding_cache=...)`` don't leak cache
    files into the developer's real document root.
    """
    import os

    assert os.environ.get("PYTEST_CURRENT_TEST")  # confirm pytest sentinel is live
    deps = EmbedDependencies()
    result = deps.open_embedding_cache()
    assert result is None


@pytest.mark.unit
def test_default_open_embedding_cache_constructs_cache_outside_pytest(tmp_path) -> None:
    """When the pytest sentinel is absent, the default wrapper resolves
    the path via ``kairix.paths.embedding_cache_path`` and returns an
    open ``EmbeddingCache``.

    Driven through the ``env=`` seam: the explicit mapping carries no
    ``PYTEST_CURRENT_TEST`` (so the test-isolation guard passes) and points
    ``KAIRIX_CACHE_DIR`` at ``tmp_path`` (so the real resolver lands the
    cache there) — no ``sys.modules`` swap, no process-env write.
    """
    from kairix.core.embed.embedding_cache import EmbeddingCache

    deps = EmbedDependencies()
    result = deps.open_embedding_cache(env={"KAIRIX_CACHE_DIR": str(tmp_path)})
    try:
        assert isinstance(result, EmbeddingCache)
        assert result.path == tmp_path / "embedding_cache.sqlite"
    finally:
        if isinstance(result, EmbeddingCache):
            result.close()


@pytest.mark.unit
def test_default_open_embedding_cache_swallows_paths_layer_failure() -> None:
    """When the paths layer raises during cache-path resolution, the
    wrapper logs and returns ``None`` rather than crashing the embed
    pipeline.

    Sabotage proof: dropping the ``try/except`` lets the resolver's
    ``RuntimeError`` escape and this test errors.
    """
    deps = EmbedDependencies()
    result = deps.open_embedding_cache(env={"KAIRIX_CACHE_DIR": _UNRESOLVABLE_HOME_PATH})

    assert result is None
