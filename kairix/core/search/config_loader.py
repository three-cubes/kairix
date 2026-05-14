"""
YAML configuration loader for kairix retrieval config.

Resolution order:
  1. KAIRIX_CONFIG_PATH env var → explicit path
  2. ./kairix.config.yaml → current working directory
  3. Built-in defaults → no file required

Missing file silently falls back to defaults.
YAML parse failure logs a warning and falls back to defaults.
Invalid config values raise ConfigValidationError — do NOT fall back silently,
as silent fallback can mask misconfiguration in production deployments.
Result is cached per process (lru_cache on resolved path).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from kairix.core.search.config import (
    EntityBoostConfig,
    ProceduralBoostConfig,
    RerankConfig,
    RetrievalConfig,
    TemporalBoostConfig,
)
from kairix.paths import config_path_override

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_FILENAME = "kairix.config.yaml"


class ConfigValidationError(ValueError):
    """Raised at startup when kairix.config.yaml contains out-of-range values.

    Unlike YAML parse errors (which fall back to defaults), validation errors
    are propagated to the caller — an invalid config should not silently produce
    unexpected retrieval behaviour in production.
    """


# Valid ranges for numeric config fields. Tuple is (min_inclusive, max_inclusive).
_VALID_RANGES: dict[str, tuple[float, float]] = {
    "entity.factor": (0.0, 10.0),
    "entity.cap": (1.0, 10.0),
    "procedural.factor": (1.0, 5.0),
    "temporal.date_path_boost_factor": (1.0, 5.0),
    "temporal.date_path_recency_window_days": (1.0, 3650.0),
    "temporal.chunk_date_decay_halflife_days": (1.0, 3650.0),
    "rerank.candidate_limit": (1.0, 100.0),
}


def _resolve_config_path(explicit: Path | str | None = None) -> Path | None:
    """Find the config file path.

    Resolution order:
      1. ``explicit`` kwarg if provided (test seam — F2-clean alternative to
         monkeypatching ``KAIRIX_CONFIG_PATH``).
      2. ``KAIRIX_CONFIG_PATH`` env var.
      3. ``kairix.config.yaml`` in the current working directory.
    """
    if explicit is not None:
        p = Path(explicit)
        if p.is_file():
            return p
        logger.warning("config_loader: explicit config path %r not found — using defaults", str(explicit))
        return None
    env_path = config_path_override()
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        logger.warning("config_loader: KAIRIX_CONFIG_PATH=%r not found — using defaults", env_path)
        return None
    cwd_path = Path.cwd() / _DEFAULT_CONFIG_FILENAME
    if cwd_path.is_file():
        return cwd_path
    return None


def _validate_config(cfg: RetrievalConfig) -> None:
    """Raise ConfigValidationError if any field is outside its valid range.

    Called after parsing, before caching. Does NOT fall back to defaults —
    invalid configuration should surface as an error so operators notice it.
    """
    checks = {
        "entity.factor": cfg.entity.factor,
        "entity.cap": cfg.entity.cap,
        "procedural.factor": cfg.procedural.factor,
        "temporal.date_path_boost_factor": cfg.temporal.date_path_boost_factor,
        "temporal.date_path_recency_window_days": float(cfg.temporal.date_path_recency_window_days),
        "temporal.chunk_date_decay_halflife_days": float(cfg.temporal.chunk_date_decay_halflife_days),
        "rerank.candidate_limit": float(cfg.rerank.candidate_limit),
    }
    errors: list[str] = []
    for field_name, value in checks.items():
        lo, hi = _VALID_RANGES[field_name]
        if not (lo <= value <= hi):
            errors.append(f"  {field_name}: {value} is outside valid range [{lo}, {hi}]")

    if errors:
        raise ConfigValidationError("kairix.config.yaml contains invalid values:\n" + "\n".join(errors))


@lru_cache(maxsize=1)
def _load_cached(config_path: Path | None) -> RetrievalConfig:
    """Load and cache RetrievalConfig from path. Returns defaults if path is None."""
    if config_path is None:
        return RetrievalConfig.defaults()
    # PyYAML is a hard dependency in pyproject.toml; the ImportError fallback
    # only fires in production builds where the optional extras are stripped.
    try:
        import yaml  # type: ignore[import-untyped] — PyYAML ships without type stubs upstream
    except ImportError:  # pragma: no cover — PyYAML is a hard dep in pyproject; only fires in stripped builds
        logger.warning("config_loader: PyYAML not installed — using defaults")
        return RetrievalConfig.defaults()

    try:
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("config_loader: failed to read %s — %s — using defaults", config_path, e)
        return RetrievalConfig.defaults()

    try:
        cfg = _parse_config(data)
        _validate_config(cfg)
        return cfg
    except ConfigValidationError:
        raise  # propagate — never fall back silently on invalid config
    except Exception as e:
        logger.warning("config_loader: failed to parse %s — %s — using defaults", config_path, e)
        return RetrievalConfig.defaults()


def load_config(config_path: Path | str | None = None) -> RetrievalConfig:
    """
    Load RetrievalConfig from YAML file or return defaults.

    Call this once at startup. Result is cached per process.

    Args:
        config_path: Optional explicit config-file path (test seam).
            When None, resolves via ``KAIRIX_CONFIG_PATH`` env var, then
            ``kairix.config.yaml`` in cwd.

    Raises:
        ConfigValidationError: if the config file contains out-of-range values.
    """
    path = _resolve_config_path(config_path)
    if path is not None:
        logger.info("config_loader: loading config from %s", path)
    return _load_cached(path)


def _parse_config(data: dict) -> RetrievalConfig:
    """Parse YAML dict into RetrievalConfig. Returns defaults for any missing/invalid section."""
    retrieval = data.get("retrieval", {}) or {}
    boosts = retrieval.get("boosts", {}) or {}

    defaults = RetrievalConfig.defaults()

    entity_cfg = _parse_entity(boosts.get("entity", {}) or {}) if boosts.get("entity") else defaults.entity
    procedural_cfg = (
        _parse_procedural(boosts.get("procedural", {}) or {}) if boosts.get("procedural") else defaults.procedural
    )
    temporal_cfg = _parse_temporal(boosts.get("temporal", {}) or {}) if boosts.get("temporal") else defaults.temporal
    rerank_cfg = _parse_rerank(retrieval.get("rerank", {}) or {}) if retrieval.get("rerank") else defaults.rerank

    # Fusion strategy + RRF k
    fusion = str(retrieval.get("fusion_strategy", defaults.fusion_strategy))
    if fusion not in ("bm25_primary", "rrf"):
        logger.warning("config_loader: unknown fusion_strategy %r — using default", fusion)
        fusion = defaults.fusion_strategy
    rrf_k = int(retrieval.get("rrf_k", defaults.rrf_k))
    vec_limit = int(retrieval.get("vec_limit", defaults.vec_limit))
    bm25_limit = int(retrieval.get("bm25_limit", defaults.bm25_limit))

    return RetrievalConfig(
        fusion_strategy=fusion,
        rrf_k=rrf_k,
        bm25_limit=bm25_limit,
        vec_limit=vec_limit,
        entity=entity_cfg,
        procedural=procedural_cfg,
        temporal=temporal_cfg,
        rerank=rerank_cfg,
    )


# ---------------------------------------------------------------------------
# Collections parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectionDef:
    """A configured document collection for search scoping.

    ``in_default`` controls whether this collection participates in the
    *default* search scopes (SHARED, SHARED_AGENT, ALL_AGENTS, EVERYTHING).
    Collections with ``in_default=False`` are still scanned, indexed, and
    reachable via an explicit ``--collection <name>`` lookup — they simply
    don't auto-join the default mix. The intended use is large or noisy
    corpora (reference libraries, archives) that should be opt-in.
    """

    name: str
    path: str  # relative to document_root
    glob: str = "**/*.md"
    in_default: bool = True
    retrieval_overrides: dict | None = None  # per-collection retrieval config (raw YAML dict)


@dataclass(frozen=True)
class CollectionsConfig:
    """Parsed collections configuration.

    ``shared`` is stored as a tuple — frozen at construction — so callers
    cannot mutate the collection list after the boundary parses YAML.
    Predicates (:meth:`default_collection_names`, :meth:`all_collection_names`)
    are the only public surface for membership questions; consumers should
    not iterate ``shared`` directly to filter on ``in_default``.
    """

    shared: tuple[CollectionDef, ...]
    agent_pattern: str = "{agent}-memory"
    agent_paths: dict[str, str] = field(default_factory=dict)

    def default_collection_names(self) -> list[str]:
        """Names of shared collections eligible for default search scopes.

        Excludes any collection whose ``in_default`` flag is False. This is
        the predicate the resolver consults — it is intentionally the only
        ``in_default``-aware code path in the codebase, so the policy lives
        with the data it describes.
        """
        return [c.name for c in self.shared if c.in_default]

    def all_collection_names(self) -> list[str]:
        """Names of every configured shared collection.

        Used for diagnostics and for callers that need to enumerate every
        configured collection regardless of default-scope eligibility (e.g.,
        validation that warns about unknown ``--collection`` arguments).
        """
        return [c.name for c in self.shared]


def _coerce_bool(value: object, *, key: str, default: bool) -> bool:
    """Strict bool coercion for YAML scalar fields.

    YAML's native scalar parser already produces ``True``/``False`` for
    canonical boolean keywords (``true``, ``false``, ``yes``, ``no``,
    ``on``, ``off``). Anything outside that set — for example an explicit
    string ``"false"`` — is rejected with :class:`ConfigValidationError`.

    Without this strictness, ``bool("false")`` evaluates to ``True``,
    which would silently route a collection into the *opposite* scope of
    the operator's intent. Better to raise at config-load than to ship a
    misconfigured search surface to production.

    Args:
        value:   The raw YAML value (may be missing, in which case the
                 caller passes ``None``).
        key:     The dotted yaml key for the error message (e.g. ``"collections.shared[3].in_default"``).
        default: Value to return when ``value is None``.

    Raises:
        ConfigValidationError: ``value`` is neither ``None`` nor ``bool``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigValidationError(
        f"kairix.config.yaml: {key}={value!r} must be a boolean (true/false), "
        f"not {type(value).__name__}. Use unquoted true or false in YAML."
    )


def parse_collections(data: dict) -> CollectionsConfig | None:
    """Parse the collections: section from config. Returns None if not present."""
    collections = data.get("collections")
    if not collections:
        return None

    shared_raw = collections.get("shared", [])
    shared: list[CollectionDef] = []
    for index, item in enumerate(shared_raw):
        if not (isinstance(item, dict) and "name" in item):
            continue
        in_default = _coerce_bool(
            item.get("in_default"),
            key=f"collections.shared[{index}].in_default",
            default=True,
        )
        shared.append(
            CollectionDef(
                name=item["name"],
                path=item.get("path", "."),
                glob=item.get("glob", "**/*.md"),
                in_default=in_default,
                retrieval_overrides=item.get("retrieval"),
            )
        )

    return CollectionsConfig(
        shared=tuple(shared),
        agent_pattern=collections.get("agent_pattern", "{agent}-memory"),
        agent_paths=collections.get("agent_paths", {}),
    )


def load_collections() -> CollectionsConfig | None:
    """Load collections config from YAML. Returns None if not configured."""
    path = _resolve_config_path()
    if path is None:
        return None
    try:
        import yaml

        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return parse_collections(data)
    except Exception:
        return None


def _parse_entity(d: dict) -> EntityBoostConfig:
    defaults = EntityBoostConfig()
    return EntityBoostConfig(
        enabled=bool(d.get("enabled", defaults.enabled)),
        factor=float(d.get("factor", defaults.factor)),
        cap=float(d.get("cap", defaults.cap)),
    )


def _parse_procedural(d: dict) -> ProceduralBoostConfig:
    defaults = ProceduralBoostConfig()
    patterns = d.get("path_patterns")
    return ProceduralBoostConfig(
        enabled=bool(d.get("enabled", defaults.enabled)),
        factor=float(d.get("factor", defaults.factor)),
        path_patterns=tuple(patterns) if patterns else defaults.path_patterns,
    )


def _parse_temporal(d: dict) -> TemporalBoostConfig:
    defaults = TemporalBoostConfig()
    date_path = d.get("date_path_boost", {}) or {}
    chunk_date = d.get("chunk_date_boost", {}) or {}
    return TemporalBoostConfig(
        date_path_boost_enabled=bool(date_path.get("enabled", defaults.date_path_boost_enabled)),
        date_path_boost_factor=float(date_path.get("factor", defaults.date_path_boost_factor)),
        date_path_recency_window_days=int(date_path.get("recency_window_days", defaults.date_path_recency_window_days)),
        chunk_date_boost_enabled=bool(chunk_date.get("enabled", defaults.chunk_date_boost_enabled)),
        chunk_date_decay_halflife_days=int(
            chunk_date.get("decay_halflife_days", defaults.chunk_date_decay_halflife_days)
        ),
        chunk_date_boost_guard_explicit_only=bool(
            chunk_date.get("guard_explicit_only", defaults.chunk_date_boost_guard_explicit_only)
        ),
    )


def _parse_rerank(d: dict) -> RerankConfig:
    defaults = RerankConfig()
    return RerankConfig(
        enabled=bool(d.get("enabled", defaults.enabled)),
        model=str(d.get("model", defaults.model)),
        candidate_limit=int(d.get("candidate_limit", defaults.candidate_limit)),
    )


# ---------------------------------------------------------------------------
# Per-collection config resolution
# ---------------------------------------------------------------------------


def merge_retrieval_config(base: RetrievalConfig, overrides: dict) -> RetrievalConfig:
    """Apply a partial YAML override dict on top of a base RetrievalConfig.

    Only keys present in the override dict are applied. Sub-configs (entity,
    procedural, temporal, rerank) are merged at their own level — overriding
    entity.factor does not reset entity.cap to its default.
    """
    from dataclasses import replace

    top_fields: dict = {}
    for key in ("fusion_strategy", "rrf_k", "bm25_limit", "vec_limit", "skip_vector"):
        if key in overrides:
            top_fields[key] = type(getattr(base, key))(overrides[key])

    # rerank_intents is a tuple[str, ...] — coerce list/None from YAML into the
    # right shape. Per-collection override (e.g. reference-library: only
    # 'conceptual' triggers rerank, not 'multi_hop') closes #74.
    if "rerank_intents" in overrides:
        intents = overrides["rerank_intents"] or []
        top_fields["rerank_intents"] = tuple(str(x) for x in intents)

    boosts = overrides.get("boosts", {}) or {}
    if "entity" in boosts:
        merged = {
            "enabled": base.entity.enabled,
            "factor": base.entity.factor,
            "cap": base.entity.cap,
            **boosts["entity"],
        }
        top_fields["entity"] = _parse_entity(merged)
    if "procedural" in boosts:
        merged = {
            "enabled": base.procedural.enabled,
            "factor": base.procedural.factor,
            **boosts["procedural"],
        }
        top_fields["procedural"] = _parse_procedural(merged)
    if "temporal" in boosts:
        # ``_parse_temporal`` expects the nested ``date_path_boost: {factor, ...}``
        # shape, not the flat field names — so the base-fallback dict has to
        # mirror that shape and per-key overrides have to deep-merge on top.
        base_temporal_dict = {
            "date_path_boost": {
                "enabled": base.temporal.date_path_boost_enabled,
                "factor": base.temporal.date_path_boost_factor,
                "recency_window_days": base.temporal.date_path_recency_window_days,
            },
            "chunk_date_boost": {
                "enabled": base.temporal.chunk_date_boost_enabled,
                "decay_halflife_days": base.temporal.chunk_date_decay_halflife_days,
                "guard_explicit_only": base.temporal.chunk_date_boost_guard_explicit_only,
            },
        }
        user_temporal = boosts["temporal"] or {}
        merged = dict(base_temporal_dict)
        for sub_key in ("date_path_boost", "chunk_date_boost"):
            if sub_key in user_temporal:
                merged[sub_key] = {**base_temporal_dict[sub_key], **user_temporal[sub_key]}
        top_fields["temporal"] = _parse_temporal(merged)

    rerank = overrides.get("rerank", {})
    if rerank:
        merged = {
            "enabled": base.rerank.enabled,
            "model": base.rerank.model,
            "candidate_limit": base.rerank.candidate_limit,
            **rerank,
        }
        top_fields["rerank"] = _parse_rerank(merged)

    return replace(base, **top_fields) if top_fields else base


def _get_collection_overrides() -> dict[str, dict]:
    """Load per-collection retrieval override dicts from config YAML."""
    collections_cfg = load_collections()
    if not collections_cfg:
        return {}
    return {c.name: c.retrieval_overrides for c in collections_cfg.shared if c.retrieval_overrides}


@dataclass
class ResolveConfigDeps:
    """Injectable dependencies for ``resolve_retrieval_config``.

    Both fields are typed as concrete callables (no ``Optional``) so mypy
    sees a real type at every call site. Production callers leave
    ``deps=None`` — the dataclass wires the real loader and override lookup
    via ``default_factory``. Tests construct
    ``ResolveConfigDeps(config_fn=..., overrides_fn=...)``.
    """

    config_fn: Callable[[], RetrievalConfig] = field(default_factory=lambda: load_config)
    overrides_fn: Callable[[], dict[str, dict]] = field(default_factory=lambda: _get_collection_overrides)


def resolve_retrieval_config(
    collection: str | None = None,
    collections: list[str] | None = None,
    explicit_config: RetrievalConfig | None = None,
    deps: ResolveConfigDeps | None = None,
) -> RetrievalConfig:
    """Resolve the retrieval config for a search request.

    Priority:
      1. explicit_config (passed by caller, e.g. sweep override) — use as-is
      2. Single collection with per-collection YAML config — merge over global
      3. Multi-collection or no collection — global config
      4. No config file — RetrievalConfig.defaults()

    The reference-library baseline is no longer baked into this function.
    The shipped example yaml carries an explicit ``retrieval:`` block on
    the reference-library entry whose values match the historical
    ``REFLIB_RETRIEVAL_CONFIG`` baseline; operators who deviate are taking
    deliberate ownership of that retrieval shape. The constant remains in
    ``kairix/core/search/config.py`` for code that wants to compare against
    the known-good baseline.

    Args:
        collection:      Single collection name (legacy parameter shape).
        collections:     List of collection names; per-collection override
                         applies only when this list is length 1.
        explicit_config: Direct override; bypasses all other lookup.
        deps:            Injectable dependencies (config_fn, overrides_fn).
                         Production callers leave None; tests pass a
                         ``ResolveConfigDeps`` with fakes. The default
                         factories wire the real loader and per-collection
                         override lookup.
    """
    if explicit_config is not None:
        return explicit_config

    d = deps or ResolveConfigDeps()
    global_cfg = d.config_fn()

    # Determine target collection (only for single-collection searches)
    target = collection
    if target is None and collections and len(collections) == 1:
        target = collections[0]

    if target is None:
        return global_cfg

    # Per-collection YAML overrides
    overrides = d.overrides_fn().get(target)
    if overrides:
        return merge_retrieval_config(global_cfg, overrides)

    return global_cfg
