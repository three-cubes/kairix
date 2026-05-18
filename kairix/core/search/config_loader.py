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
from typing import Any

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

# The path the Docker image bundles its canonical config at. Operators
# overlay sparse host-side overrides via ``KAIRIX_CONFIG_OVERLAY_PATH``;
# the layered loader reads BASE from this location unless
# ``KAIRIX_CONFIG_BASE_PATH`` is set to point elsewhere.
_DEFAULT_IMAGE_BASE_PATH = Path("/opt/kairix/kairix.config.yaml")


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


# ---------------------------------------------------------------------------
# Layered config loader — base + sparse operator overlay
# ---------------------------------------------------------------------------


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``overlay`` ON TOP OF ``base``; returns a new dict.

    Semantics:
      - dict + dict  → recursive merge (operator's nested key wins at the
        leaf; siblings at every level survive from base)
      - list + list  → overlay REPLACES base (operator declaring their own
        ``collections.shared`` gets exactly their list, not a concat)
      - scalar / type-mismatch → overlay wins

    Neither input is mutated; callers can safely reuse both.
    """
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        # Top-level call should always pass dicts; defensive return supports
        # recursive descent into mixed-type values.
        return overlay
    result: dict[str, Any] = {}
    for key in {*base.keys(), *overlay.keys()}:
        if key in overlay:
            if key in base and isinstance(base[key], dict) and isinstance(overlay[key], dict):
                result[key] = deep_merge(base[key], overlay[key])
            else:
                result[key] = overlay[key]
        else:
            result[key] = base[key]
    return result


def _resolve_layered_base(base_value: str, image_base_default: Path) -> Path | None:
    """Resolve the base path for layered mode.

    Explicit ``KAIRIX_CONFIG_BASE_PATH`` wins; otherwise the image-bundled
    default applies when it exists. Missing files log a warning and yield
    ``None`` so the caller can degrade gracefully.
    """
    if base_value:
        base_p = Path(base_value)
        if base_p.is_file():
            return base_p
        logger.warning("config_loader: KAIRIX_CONFIG_BASE_PATH=%r not found", base_value)
        return None
    return image_base_default if image_base_default.is_file() else None


def _resolve_layered_overlay(overlay_value: str) -> Path | None:
    """Resolve the overlay path for layered mode.

    Empty string → ``None`` (base-only layered mode). Missing file logs a
    warning and yields ``None`` so the caller still loads the base alone.
    """
    if not overlay_value:
        return None
    overlay_p = Path(overlay_value)
    if overlay_p.is_file():
        return overlay_p
    logger.warning(
        "config_loader: KAIRIX_CONFIG_OVERLAY_PATH=%r not found — loading base alone",
        overlay_value,
    )
    return None


def _resolve_legacy_or_cwd(env: dict[str, str]) -> tuple[Path | None, Path | None]:
    """Resolve the legacy single-file mode or cwd-discovery fallback."""
    legacy_value = env.get("KAIRIX_CONFIG_PATH", "").strip()
    if legacy_value:
        legacy_p = Path(legacy_value)
        if legacy_p.is_file():
            return legacy_p, None
        logger.warning("config_loader: KAIRIX_CONFIG_PATH=%r not found — using defaults", legacy_value)
        return None, None

    cwd_p = Path.cwd() / _DEFAULT_CONFIG_FILENAME
    if cwd_p.is_file():
        return cwd_p, None
    return None, None


def resolve_layered_paths(
    *,
    env: dict[str, str] | None = None,
    image_base_default: Path = _DEFAULT_IMAGE_BASE_PATH,
) -> tuple[Path | None, Path | None]:
    """Return ``(base_path, overlay_path)`` — F2-clean env resolution.

    Resolution matrix:
      - ``KAIRIX_CONFIG_OVERLAY_PATH`` set → layered mode:
          base ← ``KAIRIX_CONFIG_BASE_PATH`` or ``image_base_default``,
          overlay ← env var.
      - ``KAIRIX_CONFIG_PATH`` set (and overlay not set) → legacy
        single-file mode: ``(single_path, None)``.
      - ``./kairix.config.yaml`` exists → cwd-discovery: ``(cwd_path, None)``.
      - Otherwise → ``(None, None)`` — caller falls back to defaults.

    The ``env`` kwarg makes this F2-clean: tests pass an explicit dict
    instead of mutating ``os.environ`` via monkeypatch.setenv.
    """
    if env is None:
        import os

        env = dict(os.environ)

    overlay_value = env.get("KAIRIX_CONFIG_OVERLAY_PATH", "").strip()
    base_value = env.get("KAIRIX_CONFIG_BASE_PATH", "").strip()

    if overlay_value or base_value:
        return _resolve_layered_base(base_value, image_base_default), _resolve_layered_overlay(overlay_value)

    return _resolve_legacy_or_cwd(env)


def validate_schema_compat(base_data: dict[str, Any], overlay_data: dict[str, Any] | None) -> None:
    """Refuse to load when ``overlay._schema_version_required_min`` exceeds
    ``base._schema_version``.

    Operator-facing error: actionable, with F21 markers, points at the
    upgrade runbook. Base without ``_schema_version`` is treated as
    version 0 — so any positive ``_schema_version_required_min`` against
    such a base raises.
    """
    if overlay_data is None:
        return
    required_min = overlay_data.get("_schema_version_required_min")
    if required_min is None:
        return
    try:
        required_min_int = int(required_min)
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(
            f"config overlay: _schema_version_required_min must be an integer; got {required_min!r}\n"
            f"fix: set _schema_version_required_min to a positive integer (e.g. 1)\n"
            f"next: re-run kairix once the overlay is corrected."
        ) from exc
    base_version = int(base_data.get("_schema_version", 0))
    if required_min_int > base_version:
        raise ConfigValidationError(
            f"config overlay: requires _schema_version >= {required_min_int} but the "
            f"image-bundled base ships _schema_version = {base_version}.\n"
            f"fix: upgrade the kairix image to a release shipping _schema_version "
            f">= {required_min_int}, OR remove `_schema_version_required_min` from "
            f"your overlay if you've manually verified compatibility.\n"
            f"next: see docs/operations/runbooks/config-upgrade.md for the supported "
            f"upgrade path.\n"
            f"run: kairix probe-config to inspect the merged config the running "
            f"container would see."
        )


def _load_yaml_safe(path: Path | None) -> dict[str, Any]:
    """Load YAML file → dict; empty dict on missing path or parse failure."""
    if path is None:
        return {}
    try:
        import yaml  # type: ignore[import-untyped] — PyYAML ships without type stubs upstream
    except ImportError:  # pragma: no cover — PyYAML is a hard dep in pyproject; only fires in stripped builds
        logger.warning("config_loader: PyYAML not installed — empty config")
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("config_loader: failed to read %s — %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("config_loader: %s root is not a mapping — empty config", path)
        return {}
    return data


def load_layered_yaml(
    *,
    env: dict[str, str] | None = None,
    image_base_default: Path = _DEFAULT_IMAGE_BASE_PATH,
) -> dict[str, Any]:
    """Public: read base + overlay YAML and return the merged dict.

    Schema-version compat is enforced before merge: an overlay declaring
    a required-min higher than the base's shipped version raises
    :class:`ConfigValidationError` (operator must upgrade the image or
    drop the constraint). The merged dict is what
    :func:`parse_config` and :func:`parse_collections` then consume.
    """
    base_path, overlay_path = resolve_layered_paths(env=env, image_base_default=image_base_default)
    base_data = _load_yaml_safe(base_path)
    overlay_data = _load_yaml_safe(overlay_path) if overlay_path is not None else None
    if overlay_data:
        validate_schema_compat(base_data, overlay_data)
        return deep_merge(base_data, overlay_data)
    return base_data


def resolve_config_path(explicit: Path | str | None = None) -> Path | None:
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


def validate_config(cfg: RetrievalConfig) -> None:
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
def load_cached(config_path: Path | None) -> RetrievalConfig:
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
        cfg = parse_config(data)
        validate_config(cfg)
        return cfg
    except ConfigValidationError:
        raise  # propagate — never fall back silently on invalid config
    except Exception as e:
        logger.warning("config_loader: failed to parse %s — %s — using defaults", config_path, e)
        return RetrievalConfig.defaults()


def load_config(
    config_path: Path | str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> RetrievalConfig:
    """
    Load RetrievalConfig from layered YAML (base + overlay) or return defaults.

    Call this once at startup. The layered loader merges the image-bundled
    base config (``KAIRIX_CONFIG_BASE_PATH`` or
    ``/opt/kairix/kairix.config.yaml``) with a sparse operator overlay
    (``KAIRIX_CONFIG_OVERLAY_PATH``). When no overlay is configured the
    legacy single-file paths still resolve (``KAIRIX_CONFIG_PATH``, or
    ``./kairix.config.yaml`` in cwd).

    Args:
        config_path: Optional explicit single-file path (test seam).
            When provided, takes precedence over env-driven resolution —
            useful for unit tests that want to drive a known file without
            building an env dict.
        env: Optional explicit env dict (F2-clean test seam). When None,
            ``os.environ`` is consulted. Tests pass a dict to drive the
            layered/legacy/cwd resolution matrix without monkey-patching
            the process environment.

    Raises:
        ConfigValidationError: if the merged config contains out-of-range
            values, or the overlay declares a schema-version higher than
            the base ships.
    """
    if config_path is not None:
        path = resolve_config_path(config_path)
        if path is not None:
            logger.info("config_loader: loading config from %s", path)
        return load_cached(path)

    base_path, overlay_path = resolve_layered_paths(env=env)
    return _load_cached_layered(base_path, overlay_path)


@lru_cache(maxsize=1)
def _load_cached_layered(base_path: Path | None, overlay_path: Path | None) -> RetrievalConfig:
    """Load + merge + parse + validate the layered config. Cached per (base, overlay) pair.

    ``lru_cache(maxsize=1)`` matches the legacy ``load_cached`` semantics:
    the process-shared singleton invalidates whenever the resolved-path
    tuple changes (which it doesn't in production — only in tests). The
    cache key is hashable because ``Path`` is hashable. Object identity
    on repeated calls is the documented contract pinned by
    ``test_result_is_cached_per_process``.
    """
    if base_path is None and overlay_path is None:
        return RetrievalConfig.defaults()
    base_data = _load_yaml_safe(base_path)
    overlay_data = _load_yaml_safe(overlay_path) if overlay_path is not None else None
    if overlay_data:
        validate_schema_compat(base_data, overlay_data)
        merged = deep_merge(base_data, overlay_data)
    else:
        merged = base_data
    if not merged:
        return RetrievalConfig.defaults()
    try:
        cfg = parse_config(merged)
        validate_config(cfg)
    except ConfigValidationError:
        raise
    except Exception as exc:
        logger.warning("config_loader: failed to parse merged config — %s — using defaults", exc)
        return RetrievalConfig.defaults()
    return cfg


def parse_config(data: dict) -> RetrievalConfig:
    """Parse YAML dict into RetrievalConfig. Returns defaults for any missing/invalid section.

    Top-level ``provider:`` is honoured as the configured provider plugin
    name (see ``docs/architecture/provider-plugin-architecture.md``). A
    missing / blank value yields ``provider=None``; callers that depend
    on a configured provider (``kairix.core.factory.build_search_pipeline``)
    surface a typed ValueError listing the installed plugins.
    """
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

    # Top-level ``provider:`` — names the plugin loaded by
    # ``kairix.providers.get_provider``. ``None`` propagates when the
    # field is absent or blank so the factory's typed error surfaces
    # with the installed-plugins list.
    raw_provider = data.get("provider")
    provider_name = str(raw_provider).strip() if raw_provider else None
    if provider_name == "":
        provider_name = None

    return RetrievalConfig(
        provider=provider_name,
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
    path = resolve_config_path()
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


def _merge_top_level_scalars(base: RetrievalConfig, overrides: dict) -> dict:
    """Coerce + return the override scalar fields (fusion/rrf_k/limits/skip)."""
    out: dict = {}
    for key in ("fusion_strategy", "rrf_k", "bm25_limit", "vec_limit", "skip_vector"):
        if key in overrides:
            out[key] = type(getattr(base, key))(overrides[key])
    # rerank_intents is a tuple[str, ...] — coerce list/None from YAML into
    # the right shape (per-collection override).
    if "rerank_intents" in overrides:
        intents = overrides["rerank_intents"] or []
        out["rerank_intents"] = tuple(str(x) for x in intents)
    return out


def _merge_entity_boost(base: RetrievalConfig, override: dict) -> Any:
    return _parse_entity(
        {
            "enabled": base.entity.enabled,
            "factor": base.entity.factor,
            "cap": base.entity.cap,
            **override,
        }
    )


def _merge_procedural_boost(base: RetrievalConfig, override: dict) -> Any:
    return _parse_procedural(
        {
            "enabled": base.procedural.enabled,
            "factor": base.procedural.factor,
            **override,
        }
    )


def _merge_temporal_boost(base: RetrievalConfig, override: dict) -> Any:
    """Deep-merge nested ``date_path_boost`` / ``chunk_date_boost`` blocks.

    ``_parse_temporal`` expects the nested shape, not the flat field names.
    """
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
    user_temporal = override or {}
    merged: dict[str, Any] = dict(base_temporal_dict)
    for sub_key in ("date_path_boost", "chunk_date_boost"):
        if sub_key in user_temporal:
            merged[sub_key] = {**base_temporal_dict[sub_key], **user_temporal[sub_key]}
    return _parse_temporal(merged)


def _merge_rerank(base: RetrievalConfig, override: dict) -> Any:
    return _parse_rerank(
        {
            "enabled": base.rerank.enabled,
            "model": base.rerank.model,
            "candidate_limit": base.rerank.candidate_limit,
            **override,
        }
    )


def merge_retrieval_config(base: RetrievalConfig, overrides: dict) -> RetrievalConfig:
    """Apply a partial YAML override dict on top of a base RetrievalConfig.

    Only keys present in the override dict are applied. Sub-configs (entity,
    procedural, temporal, rerank) are merged at their own level — overriding
    entity.factor does not reset entity.cap to its default.
    """
    from dataclasses import replace

    top_fields: dict = _merge_top_level_scalars(base, overrides)

    boosts = overrides.get("boosts", {}) or {}
    if "entity" in boosts:
        top_fields["entity"] = _merge_entity_boost(base, boosts["entity"])
    if "procedural" in boosts:
        top_fields["procedural"] = _merge_procedural_boost(base, boosts["procedural"])
    if "temporal" in boosts:
        top_fields["temporal"] = _merge_temporal_boost(base, boosts["temporal"])

    rerank = overrides.get("rerank", {})
    if rerank:
        top_fields["rerank"] = _merge_rerank(base, rerank)

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
