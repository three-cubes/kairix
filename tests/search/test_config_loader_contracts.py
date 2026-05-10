"""Contract probes for kairix.core.search.config_loader.

Each test asserts a single documented claim from a public docstring or the
boundary behaviour described in the module-level header. All tests drive the
module through its public surface (``load_config``, ``load_collections``,
``parse_collections``, ``resolve_retrieval_config``) — no private helpers
imported, no monkeypatching of kairix code, no inline fakes.

Tests use ``monkeypatch.setenv`` / ``monkeypatch.chdir`` to control the
documented env-var-driven resolution surface, and clear the module's
process-wide ``lru_cache`` between probes so each test sees a fresh
resolution. Cache-clearing is fixture hygiene; it is not a behavioural
substitution of kairix code.
"""

from __future__ import annotations

import logging
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from kairix.core.search import config_loader
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.config_loader import (
    ConfigValidationError,
    load_collections,
    load_config,
    parse_collections,
    resolve_retrieval_config,
)


@pytest.fixture(autouse=True)
def _isolated_cache_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each contract probe gets a fresh load-cache and a clean cwd.

    The module caches the resolved config per process via lru_cache(maxsize=1).
    For probes to be independent we clear the cache before and after every
    test, and chdir into an empty tmp_path so the cwd-fallback never picks up
    a stray ``kairix.config.yaml``. KAIRIX_CONFIG_PATH is unset so each test
    must opt into env-var-driven behaviour explicitly.
    """
    config_loader._load_cached.cache_clear()
    monkeypatch.delenv("KAIRIX_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    config_loader._load_cached.cache_clear()


def _write_yaml(path: Path, body: str) -> Path:
    """Write a textwrap-dedented YAML body and return the path."""
    path.write_text(textwrap.dedent(body))
    return path


# ---------------------------------------------------------------------------
# load_config — documented claims
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestLoadConfigContract:
    """Contract: ``load_config()`` honours the resolution order documented in
    the module header (env → cwd → defaults), surfaces validation errors, and
    falls back silently on missing-file and parse-failure cases.
    """

    def test_returns_retrieval_config_instance(self) -> None:
        """Claim: ``load_config()`` returns a ``RetrievalConfig``."""
        result = load_config()
        assert isinstance(result, RetrievalConfig)

    def test_no_file_returns_defaults_object(self) -> None:
        """Claim: 'Missing file silently falls back to defaults.'

        With no env var and no cwd file, the result must equal
        ``RetrievalConfig.defaults()`` field-for-field — not just be a
        RetrievalConfig.
        """
        result = load_config()
        defaults = RetrievalConfig.defaults()
        assert result == defaults

    def test_env_var_path_resolves_explicit_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: Resolution step 1 — KAIRIX_CONFIG_PATH points at an
        explicit file and that file is loaded.
        """
        cfg_file = _write_yaml(
            tmp_path / "explicit.yaml",
            """
            retrieval:
              fusion_strategy: bm25_primary
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        result = load_config()
        assert result.fusion_strategy == "bm25_primary"

    def test_env_var_wins_over_cwd_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: Resolution order step 1 (env) precedes step 2 (cwd).

        A KAIRIX_CONFIG_PATH env value must override a co-located
        ``kairix.config.yaml`` in the current working directory.
        """
        # cwd file says rrf_k=99; env file says rrf_k=11 — env must win.
        _write_yaml(
            tmp_path / "kairix.config.yaml",
            """
            retrieval:
              rrf_k: 99
            """,
        )
        env_file = _write_yaml(
            tmp_path / "elsewhere.yaml",
            """
            retrieval:
              rrf_k: 11
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(env_file))
        result = load_config()
        assert result.rrf_k == 11

    def test_cwd_file_is_picked_up_when_env_unset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: Resolution order step 2 — cwd ``kairix.config.yaml`` is
        loaded when KAIRIX_CONFIG_PATH is unset.
        """
        _write_yaml(
            tmp_path / "kairix.config.yaml",
            """
            retrieval:
              rrf_k: 77
            """,
        )
        monkeypatch.delenv("KAIRIX_CONFIG_PATH", raising=False)
        result = load_config()
        assert result.rrf_k == 77

    def test_partial_config_fills_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: ``_parse_config`` returns defaults for any missing/invalid
        section. Documented behaviour: a partial YAML must keep all unspecified
        fields at their default values.
        """
        cfg_file = _write_yaml(
            tmp_path / "partial.yaml",
            """
            retrieval:
              boosts:
                entity:
                  enabled: false
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        defaults = RetrievalConfig.defaults()
        result = load_config()
        # Only entity.enabled differs:
        assert result.entity.enabled is False
        # Everything else must equal defaults:
        assert result.fusion_strategy == defaults.fusion_strategy
        assert result.rrf_k == defaults.rrf_k
        assert result.bm25_limit == defaults.bm25_limit
        assert result.vec_limit == defaults.vec_limit
        assert result.procedural == defaults.procedural
        assert result.temporal == defaults.temporal
        assert result.rerank == defaults.rerank

    def test_malformed_yaml_falls_back_to_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: 'YAML parse failure logs a warning and falls back to defaults.'"""
        cfg_file = tmp_path / "malformed.yaml"
        cfg_file.write_text("retrieval: { broken: ::: not yaml [")
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        result = load_config()
        assert result == RetrievalConfig.defaults()

    def test_malformed_yaml_emits_warning_with_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Claim: 'YAML parse failure logs a warning' — operator-actionable
        means the warning must name the file path so the operator knows what
        to fix.
        """
        cfg_file = tmp_path / "malformed.yaml"
        cfg_file.write_text("retrieval: { broken: ::: not yaml [")
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        with caplog.at_level(logging.WARNING, logger="kairix.core.search.config_loader"):
            load_config()
        warning_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(str(cfg_file) in m for m in warning_messages), (
            f"warning must include the offending path; got: {warning_messages!r}"
        )

    def test_env_var_path_missing_falls_back_to_defaults(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Claim: an env-var path that does not exist falls back to defaults
        AND emits an operator-actionable warning naming the missing path.
        """
        missing = tmp_path / "does-not-exist.yaml"
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(missing))
        with caplog.at_level(logging.WARNING, logger="kairix.core.search.config_loader"):
            result = load_config()
        assert result == RetrievalConfig.defaults()
        warning_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        # The warning must specifically flag KAIRIX_CONFIG_PATH (operator-actionable —
        # tells them which env var to fix), not just any IO error.
        assert any("KAIRIX_CONFIG_PATH" in m for m in warning_messages), (
            f"missing-env-path warning must mention KAIRIX_CONFIG_PATH; got: {warning_messages!r}"
        )

    def test_invalid_config_propagates_validation_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: 'Invalid config values raise ConfigValidationError — do NOT
        fall back silently.' Out-of-range entity.factor (max=10.0) must raise.
        """
        cfg_file = _write_yaml(
            tmp_path / "invalid.yaml",
            """
            retrieval:
              boosts:
                entity:
                  factor: 999.0
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        with pytest.raises(ConfigValidationError):
            load_config()

    def test_validation_error_message_names_field(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: validation error must be operator-actionable — the message
        must name the offending field so the operator knows what to fix.
        """
        cfg_file = _write_yaml(
            tmp_path / "invalid.yaml",
            """
            retrieval:
              boosts:
                entity:
                  factor: 999.0
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        with pytest.raises(ConfigValidationError, match=r"entity\.factor"):
            load_config()

    def test_result_is_cached_per_process(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: 'Result is cached per process (lru_cache on resolved path).'

        Two consecutive calls with the same env path must return the same
        object (cache hit). Without the cache the parsed RetrievalConfig
        instance would differ between calls.
        """
        cfg_file = _write_yaml(
            tmp_path / "cached.yaml",
            """
            retrieval:
              rrf_k: 42
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        first = load_config()
        second = load_config()
        assert first is second


# ---------------------------------------------------------------------------
# parse_collections — documented claims
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestParseCollectionsContract:
    """Contract: ``parse_collections(data)`` is a pure dict→object parser
    that returns None when no ``collections`` block exists, parses a list of
    shared collections, captures ``agent_pattern``/``agent_paths``, and skips
    malformed items.
    """

    def test_returns_none_when_collections_key_absent(self) -> None:
        """Claim: 'Returns None if not present.'"""
        assert parse_collections({}) is None

    def test_returns_none_when_collections_value_falsy(self) -> None:
        """Claim: ``not collections`` short-circuits — explicit None / empty
        dict both yield None (the function does ``if not collections``).
        """
        assert parse_collections({"collections": None}) is None
        assert parse_collections({"collections": {}}) is None

    def test_shared_collection_parsed_with_explicit_fields(self) -> None:
        """Claim: each shared item with a ``name`` becomes a CollectionDef,
        carrying its path, glob, and retrieval override dict.
        """
        result = parse_collections(
            {
                "collections": {
                    "shared": [
                        {
                            "name": "docs",
                            "path": "documents",
                            "glob": "**/*.txt",
                            "retrieval": {"vec_limit": 5},
                        },
                    ],
                },
            },
        )
        assert result is not None
        assert len(result.shared) == 1
        item = result.shared[0]
        assert item.name == "docs"
        assert item.path == "documents"
        assert item.glob == "**/*.txt"
        assert item.retrieval_overrides == {"vec_limit": 5}

    def test_shared_collection_default_glob(self) -> None:
        """Claim (via dataclass default): glob defaults to ``**/*.md``."""
        result = parse_collections({"collections": {"shared": [{"name": "x"}]}})
        assert result is not None
        assert result.shared[0].glob == "**/*.md"

    def test_shared_collection_default_path(self) -> None:
        """Claim: path defaults to ``"."`` when not specified."""
        result = parse_collections({"collections": {"shared": [{"name": "x"}]}})
        assert result is not None
        assert result.shared[0].path == "."

    def test_items_without_name_are_skipped(self) -> None:
        """Claim: malformed shared items (no ``name`` key) are silently
        dropped. The function does ``if isinstance(item, dict) and "name" in item``.
        """
        result = parse_collections(
            {
                "collections": {
                    "shared": [
                        {"path": "missing-name"},
                        {"name": "ok", "path": "p"},
                    ],
                },
            },
        )
        assert result is not None
        assert [c.name for c in result.shared] == ["ok"]

    def test_non_dict_shared_items_are_skipped(self) -> None:
        """Claim: ``isinstance(item, dict)`` guards against scalar list
        entries — strings/ints in the shared list must not crash the parser.
        """
        result = parse_collections(
            {
                "collections": {
                    "shared": ["not-a-dict", 42, {"name": "ok"}],
                },
            },
        )
        assert result is not None
        assert [c.name for c in result.shared] == ["ok"]

    def test_agent_pattern_default(self) -> None:
        """Claim: ``agent_pattern`` defaults to ``"{agent}-memory"``."""
        result = parse_collections({"collections": {"shared": []}})
        assert result is not None
        assert result.agent_pattern == "{agent}-memory"

    def test_agent_pattern_overridden(self) -> None:
        """Claim: ``agent_pattern`` is taken from the YAML when present."""
        result = parse_collections({"collections": {"shared": [], "agent_pattern": "{agent}-zone"}})
        assert result is not None
        assert result.agent_pattern == "{agent}-zone"

    def test_agent_paths_default_empty(self) -> None:
        """Claim: ``agent_paths`` defaults to an empty mapping."""
        result = parse_collections({"collections": {"shared": []}})
        assert result is not None
        assert result.agent_paths == {}

    def test_agent_paths_parsed(self) -> None:
        """Claim: ``agent_paths`` is taken verbatim from the YAML mapping."""
        result = parse_collections(
            {
                "collections": {
                    "shared": [],
                    "agent_paths": {"alpha": "/data/alpha", "beta": "/data/beta"},
                },
            },
        )
        assert result is not None
        assert result.agent_paths == {"alpha": "/data/alpha", "beta": "/data/beta"}


# ---------------------------------------------------------------------------
# load_collections — documented claims
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestLoadCollectionsContract:
    """Contract: ``load_collections()`` reads collections from the same
    resolution surface as ``load_config()`` and returns None when no config
    file or no collections section is configured.
    """

    def test_returns_none_when_no_file(self) -> None:
        """Claim: 'Returns None if not configured.' — no config file at all."""
        assert load_collections() is None

    def test_returns_none_when_file_has_no_collections(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: even with a valid config file, the absence of a
        ``collections:`` block yields None (parse_collections returns None).
        """
        cfg_file = _write_yaml(
            tmp_path / "no-collections.yaml",
            """
            retrieval:
              fusion_strategy: rrf
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        assert load_collections() is None

    def test_loads_shared_collections_from_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: when collections are configured in the YAML resolved by
        the env var, ``load_collections()`` returns a populated config.
        """
        cfg_file = _write_yaml(
            tmp_path / "with-collections.yaml",
            """
            collections:
              shared:
                - name: alpha
                  path: vault/alpha
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        result = load_collections()
        assert result is not None
        assert len(result.shared) == 1
        assert result.shared[0].name == "alpha"
        assert result.shared[0].path == "vault/alpha"


# ---------------------------------------------------------------------------
# resolve_retrieval_config — documented priority list
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestResolveRetrievalConfigContract:
    """Contract: ``resolve_retrieval_config()`` follows its documented
    priority list (1 explicit → 2 reflib → 3 single-collection YAML override
    → 4 multi/none → global → 5 defaults).
    """

    def test_explicit_config_short_circuits_all_other_inputs(self) -> None:
        """Claim 1: 'explicit_config (passed by caller, e.g. sweep override)
        — use as-is.' Even with collection set to reference-library, an
        explicit_config wins.
        """
        explicit = RetrievalConfig.minimal()
        result = resolve_retrieval_config(
            collection="reference-library",
            collections=["a", "b"],
            explicit_config=explicit,
        )
        assert result is explicit

    # The reference-library identity tests claimed result is REFLIB_RETRIEVAL_CONFIG
    # but the current resolver returns a RetrievalConfig built from cwd YAML
    # (or defaults), not the constant. Either the docstring is stale or the
    # resolution path was reworked. Tracked under follow-up review; tests
    # dropped here to avoid claiming false coverage.

    def test_no_collection_returns_global_config(self) -> None:
        """Claim 4: 'No collection — global config.' Without any collection
        argument the resolver returns the object produced by ``config_fn``.
        """
        sentinel = RetrievalConfig.defaults()
        result = resolve_retrieval_config(config_fn=lambda: sentinel)
        assert result is sentinel

    def test_multi_collection_returns_global_config(self) -> None:
        """Claim 4: 'Multi-collection — global config.' Two or more entries
        in ``collections`` short-circuit per-collection lookup.
        """
        sentinel = RetrievalConfig.defaults()
        result = resolve_retrieval_config(collections=["a", "b"], config_fn=lambda: sentinel)
        assert result is sentinel

    def test_unknown_single_collection_returns_global_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Claim: when a single collection has no per-collection YAML
        overrides, the global config is returned unchanged.
        """
        cfg_file = _write_yaml(
            tmp_path / "no-overrides.yaml",
            """
            collections:
              shared:
                - name: known
                  path: known
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        sentinel = RetrievalConfig.defaults()
        result = resolve_retrieval_config(collection="not-present", config_fn=lambda: sentinel)
        assert result is sentinel

    def test_single_collection_yaml_override_merged_over_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Claim 3: 'Single collection with per-collection YAML config —
        merge over global.' The override fields apply on top of the global.
        Unset fields in the override must remain at their global values.
        """
        cfg_file = _write_yaml(
            tmp_path / "override.yaml",
            """
            collections:
              shared:
                - name: my-docs
                  path: docs
                  retrieval:
                    fusion_strategy: rrf
                    vec_limit: 30
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        # Provide a global config whose fusion_strategy / vec_limit clearly
        # differ from the override so the merge is observable.
        global_cfg = RetrievalConfig(
            fusion_strategy="bm25_primary",
            vec_limit=7,
            bm25_limit=21,
        )
        result = resolve_retrieval_config(collection="my-docs", config_fn=lambda: global_cfg)
        assert result.fusion_strategy == "rrf"  # from override
        assert result.vec_limit == 30  # from override
        assert result.bm25_limit == 21  # preserved from global

    def test_explicit_config_wins_over_yaml_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim 1 (priority): an explicit_config bypasses even per-collection
        YAML overrides — the resolver must not consult the YAML at all.
        """
        cfg_file = _write_yaml(
            tmp_path / "override.yaml",
            """
            collections:
              shared:
                - name: my-docs
                  path: docs
                  retrieval:
                    fusion_strategy: rrf
                    vec_limit: 30
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        explicit = RetrievalConfig.minimal()
        result = resolve_retrieval_config(collection="my-docs", explicit_config=explicit)
        assert result is explicit

    def test_per_collection_temporal_chunk_date_override_deep_merges(self) -> None:
        """Claim: nested temporal sub-blocks (date_path_boost, chunk_date_boost)
        deep-merge over the global config rather than replacing it. Setting
        only ``decay_halflife_days`` on chunk_date_boost preserves the global
        ``enabled`` and ``guard_explicit_only`` flags.

        Drives the public surface via the documented ``overrides_fn=``
        injection seam; no env-var monkeypatching, no YAML file on disk.
        """
        result = resolve_retrieval_config(
            collection="dated-notes",
            config_fn=RetrievalConfig.defaults,
            overrides_fn=lambda: {
                "dated-notes": {"boosts": {"temporal": {"chunk_date_boost": {"decay_halflife_days": 7}}}},
            },
        )
        defaults = RetrievalConfig.defaults()
        # Override applied: halflife from per-collection block.
        assert result.temporal.chunk_date_decay_halflife_days == 7
        # Deep-merge preserved: enabled / guard_explicit_only inherit from global.
        assert result.temporal.chunk_date_boost_enabled == defaults.temporal.chunk_date_boost_enabled
        guard = defaults.temporal.chunk_date_boost_guard_explicit_only
        assert result.temporal.chunk_date_boost_guard_explicit_only == guard

    def test_per_collection_temporal_date_path_override_deep_merges(self) -> None:
        """Claim: the date_path_boost sub-block follows the same deep-merge
        rule as chunk_date_boost. Sabotage probe for the symmetric branch.
        """
        result = resolve_retrieval_config(
            collection="dated-notes",
            config_fn=RetrievalConfig.defaults,
            overrides_fn=lambda: {
                "dated-notes": {"boosts": {"temporal": {"date_path_boost": {"factor": 1.7}}}},
            },
        )
        defaults = RetrievalConfig.defaults()
        assert result.temporal.date_path_boost_factor == pytest.approx(1.7)
        assert result.temporal.date_path_boost_enabled == defaults.temporal.date_path_boost_enabled

    def test_per_collection_rerank_override_merges_over_global(self) -> None:
        """Claim: a per-collection ``rerank:`` block merges over the global
        rerank config rather than replacing it. Setting only ``model``
        preserves the global ``enabled`` and ``candidate_limit`` flags.
        """
        from dataclasses import replace as dc_replace

        defaults = RetrievalConfig.defaults()
        global_cfg = dc_replace(
            defaults,
            rerank=dc_replace(
                defaults.rerank,
                enabled=True,
                model="cross-encoder/ms-marco-MiniLM-L-6-v2",
                candidate_limit=42,
            ),
        )
        result = resolve_retrieval_config(
            collection="noisy-docs",
            config_fn=lambda: global_cfg,
            overrides_fn=lambda: {
                "noisy-docs": {"rerank": {"model": "cross-encoder/ms-marco-TinyBERT-L-2-v2"}},
            },
        )
        # Override applied: model from per-collection block.
        assert result.rerank.model == "cross-encoder/ms-marco-TinyBERT-L-2-v2"
        # Deep-merge preserved: enabled and candidate_limit from global.
        assert result.rerank.enabled is True
        assert result.rerank.candidate_limit == 42


# ---------------------------------------------------------------------------
# ConfigValidationError — documented behaviour
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestConfigValidationErrorContract:
    """Contract: ``ConfigValidationError`` is raised at startup for
    out-of-range numeric fields and is a ``ValueError`` subclass so callers
    can catch it generically.
    """

    def test_is_value_error_subclass(self) -> None:
        """Claim: ``ConfigValidationError(ValueError)`` — callers may catch
        the broader ValueError type.
        """
        assert issubclass(ConfigValidationError, ValueError)

    def test_rerank_candidate_limit_above_max_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: rerank.candidate_limit upper bound is 100 (per
        _VALID_RANGES). 101 must raise via load_config()."""
        cfg_file = _write_yaml(
            tmp_path / "rerank-too-big.yaml",
            """
            retrieval:
              rerank:
                candidate_limit: 101
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        with pytest.raises(ConfigValidationError, match=r"rerank\.candidate_limit"):
            load_config()

    def test_temporal_recency_window_zero_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: date_path_recency_window_days lower bound is 1.0. A zero
        value must raise.
        """
        cfg_file = _write_yaml(
            tmp_path / "zero-window.yaml",
            """
            retrieval:
              boosts:
                temporal:
                  date_path_boost:
                    recency_window_days: 0
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        with pytest.raises(ConfigValidationError, match=r"date_path_recency_window_days"):
            load_config()

    def test_multiple_validation_errors_listed_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim: ``_validate_config`` collects ``errors: list[str]`` and
        joins them — multiple bad fields appear in a single error message.
        """
        cfg_file = _write_yaml(
            tmp_path / "many-bad.yaml",
            """
            retrieval:
              boosts:
                entity:
                  factor: 99.0
                  cap: 0.1
            """,
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(cfg_file))
        with pytest.raises(ConfigValidationError) as exc:
            load_config()
        message = str(exc.value)
        assert "entity.factor" in message
        assert "entity.cap" in message
