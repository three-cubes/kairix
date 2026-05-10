"""Unit tests for ``EmbedDependencies`` (refactored per #204).

The deps dataclass uses ``default_factory`` to bind real production
callables without making fields ``Optional``. Tests here cover the
public surface only (no imports from the private ``_deps_defaults``
sibling module — F5):

  - Direct construction with explicit fakes — the test-time path.
  - Default construction returns a dataclass whose fields are all
    callable production wrappers.
  - Production-default behaviour exercised by *calling* the
    auto-bound callables and observing pass-through to the
    embed/schema/paths modules. The wrappers are swapped at their
    underlying module's public attribute so the lazy imports inside
    the wrappers see our stand-in implementations.
"""

from __future__ import annotations

import sqlite3
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


# ── Default-callable behaviour — pass-through via module-attribute swap ──


@pytest.mark.unit
def test_default_get_azure_config_calls_through_to_embed_module() -> None:
    """The default ``get_azure_config`` wrapper delegates to
    ``kairix.core.embed.embed._get_azure_config`` (the function-local
    lazy import resolves freshly each call, so swapping the embed
    module attribute is sufficient).

    Sabotage proof: if the wrapper hard-coded a stale reference, the
    swap would not be observed and the assertion would fail.
    """
    from kairix.core.embed import embed as embed_mod

    real = embed_mod._get_azure_config
    embed_mod._get_azure_config = lambda: ("KEY", "https://e", "DEPLOY")  # type: ignore[assignment]  # module-attribute swap to drive failure paths; restored in finally
    try:
        deps = EmbedDependencies()
        result = deps.get_azure_config()
    finally:
        embed_mod._get_azure_config = real  # type: ignore[assignment]  # module-attribute swap to drive failure paths; restored in finally

    assert result == ("KEY", "https://e", "DEPLOY")


@pytest.mark.unit
def test_default_preflight_check_calls_through_to_embed_module() -> None:
    """The default ``preflight_check`` wrapper delegates to
    ``kairix.core.embed.embed.preflight_check`` and threads the
    (api_key, endpoint, deployment) tuple unchanged.
    """
    from kairix.core.embed import embed as embed_mod

    captured: dict[str, object] = {}

    def fake_preflight(api_key: str, endpoint: str, deployment: str, **_kw: object) -> int:
        captured["args"] = (api_key, endpoint, deployment)
        return 1536

    real = embed_mod.preflight_check
    embed_mod.preflight_check = fake_preflight  # type: ignore[assignment]  # module-attribute swap to drive failure paths; restored in finally
    try:
        deps = EmbedDependencies()
        result = deps.preflight_check("k", "e", "d")
    finally:
        embed_mod.preflight_check = real  # type: ignore[assignment]  # module-attribute swap to drive failure paths; restored in finally

    assert result == 1536
    assert captured["args"] == ("k", "e", "d")


@pytest.mark.unit
def test_default_embed_batch_calls_through_to_embed_module() -> None:
    """The default ``embed_batch`` wrapper passes texts + Azure config
    + dims through to ``kairix.core.embed.embed.embed_batch``.
    """
    from kairix.core.embed import embed as embed_mod

    captured: dict[str, object] = {}

    def fake_batch(
        texts: list[str],
        api_key: str,
        endpoint: str,
        deployment: str,
        dims: int,
        **_kw: object,
    ) -> list[list[float]]:
        captured["call"] = (list(texts), api_key, endpoint, deployment, dims)
        return [[0.1] * dims for _ in texts]

    real = embed_mod.embed_batch
    embed_mod.embed_batch = fake_batch  # type: ignore[assignment]  # module-attribute swap to drive failure paths; restored in finally
    try:
        deps = EmbedDependencies()
        result = deps.embed_batch(["hi"], "k", "e", "d", 4)
    finally:
        embed_mod.embed_batch = real  # type: ignore[assignment]  # module-attribute swap to drive failure paths; restored in finally

    assert result == [[0.1, 0.1, 0.1, 0.1]]
    assert captured["call"] == (["hi"], "k", "e", "d", 4)


@pytest.mark.unit
def test_default_open_usearch_index_calls_through_to_embed_module() -> None:
    """The default ``open_usearch_index`` wrapper returns whatever
    ``kairix.core.embed.embed._open_usearch_index`` returns (incl.
    ``None``).
    """
    from kairix.core.embed import embed as embed_mod

    sentinel = object()
    real = embed_mod._open_usearch_index
    embed_mod._open_usearch_index = lambda: sentinel  # type: ignore[assignment]  # module-attribute swap to drive failure paths; restored in finally
    try:
        deps = EmbedDependencies()
        result = deps.open_usearch_index()
    finally:
        embed_mod._open_usearch_index = real  # type: ignore[assignment]  # module-attribute swap to drive failure paths; restored in finally

    assert result is sentinel


@pytest.mark.unit
def test_default_migrate_content_vectors_calls_through_to_schema_module() -> None:
    """The default ``migrate_content_vectors`` wrapper threads the
    SQLite connection through to ``kairix.core.embed.schema.migrate_content_vectors``.
    """
    from kairix.core.embed import schema as schema_mod

    seen: list[sqlite3.Connection] = []

    def fake_migrate(db: sqlite3.Connection) -> None:
        seen.append(db)

    real = schema_mod.migrate_content_vectors
    schema_mod.migrate_content_vectors = fake_migrate  # type: ignore[assignment]  # module-attribute swap to drive failure paths; restored in finally
    try:
        db = sqlite3.connect(":memory:")
        try:
            deps = EmbedDependencies()
            deps.migrate_content_vectors(db)
        finally:
            db.close()
    finally:
        schema_mod.migrate_content_vectors = real  # type: ignore[assignment]  # module-attribute swap to drive failure paths; restored in finally

    assert len(seen) == 1


# ── default_get_document_root — tolerated-failure branch ─────────────


@pytest.mark.unit
def test_default_get_document_root_returns_none_when_paths_layer_raises() -> None:
    """When the paths layer is unavailable, ``deps.get_document_root()``
    returns ``None`` (and logs a warning) rather than propagating.

    The embed pipeline only uses the document root for chunk-date
    heuristics; a missing paths layer must not crash the run.

    Driven by replacing the ``kairix.paths`` module in ``sys.modules``
    with a sentinel whose ``document_root()`` raises. The default
    wrapper imports lazily so the swap is observed.
    """
    import sys
    import types

    fake_paths = types.ModuleType("kairix.paths")

    def _boom() -> str:
        raise RuntimeError("paths layer not initialised")

    fake_paths.document_root = _boom  # type: ignore[attr-defined]  # synthetic stand-in module; mypy doesn't know our test attrs
    real_paths = sys.modules.get("kairix.paths")
    sys.modules["kairix.paths"] = fake_paths
    try:
        deps = EmbedDependencies()
        result = deps.get_document_root()
    finally:
        if real_paths is not None:
            sys.modules["kairix.paths"] = real_paths
        else:
            sys.modules.pop("kairix.paths", None)

    assert result is None


@pytest.mark.unit
def test_default_get_document_root_returns_string_on_success(tmp_path: Any) -> None:
    """When ``document_root()`` returns a Path, the default wrapper
    stringifies it.

    Sabotage proof: if the wrapper started returning the Path object
    directly (skipping ``str(...)``), downstream callers expecting a
    string would silently break; this asserts the string contract.
    """
    import sys
    import types

    fake_paths = types.ModuleType("kairix.paths")
    fake_paths.document_root = lambda: tmp_path  # type: ignore[attr-defined]  # synthetic stand-in module; mypy doesn't know our test attrs
    real_paths = sys.modules.get("kairix.paths")
    sys.modules["kairix.paths"] = fake_paths
    try:
        deps = EmbedDependencies()
        result = deps.get_document_root()
    finally:
        if real_paths is not None:
            sys.modules["kairix.paths"] = real_paths
        else:
            sys.modules.pop("kairix.paths", None)

    assert isinstance(result, str)
    assert result == str(tmp_path)
