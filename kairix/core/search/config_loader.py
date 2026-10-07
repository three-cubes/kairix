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

from kairix.config_layers import (
    DEFAULT_IMAGE_BASE_PATH as _DEFAULT_IMAGE_BASE_PATH,
)
from kairix.config_layers import (
    deep_merge,
    resolve_layered_paths,
)
from kairix.config_layers import (
    load_yaml_mapping as _load_yaml_safe,
)
from kairix.core.search.config import (
    ContentQualityBoostConfig,
    EntityBoostConfig,
    ProceduralBoostConfig,
    RerankConfig,
    RetrievalConfig,
    SourceTier,
    SourceTierBoostConfig,
    TemporalBoostConfig,
)
from kairix.paths import config_path_override

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_FILENAME = "kairix.config.yaml"

# F17 — config keys repeated across loader, merger, and dict-build paths;
# extract so a YAML schema rename hits a single edit site.
_KEY_PROCEDURAL = "procedural"
_KEY_RERANK_INTENTS = "rerank_intents"
_KEY_DATE_PATH_BOOST = "date_path_boost"
_KEY_CHUNK_DATE_BOOST = "chunk_date_boost"


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
# The resolve + merge core (``deep_merge``, ``resolve_layered_paths``,
# ``load_yaml_mapping``) lives in :mod:`kairix.config_layers` so
# ``kairix.paths`` and the worker share the SAME layered read path the
# wizard writes through (#492). This module re-exports the names it has
# always shipped; schema-compat validation stays here because it is a
# retrieval-loader concern.


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


def reset_config_cache() -> None:
    """Clear the layered-config + legacy-single-file caches.

    Public surface so tests can clear the resolver's lru_cache without
    importing the private ``_load_cached_layered`` (F5). Production code
    has no reason to call this; cache invalidation is process-lifetime.
    """
    load_cached.cache_clear()
    _load_cached_layered.cache_clear()


@dataclass(frozen=True)
class _BoostOverlay:
    """Parsed boost configs extracted from a retrieval YAML block.

    Internal-only — holds the per-boost configs ``parse_config``
    threads into :class:`RetrievalConfig`. Pulled out so
    ``parse_config`` stays under the F16 cognitive-complexity ceiling.
    """

    entity: EntityBoostConfig
    procedural: ProceduralBoostConfig
    temporal: TemporalBoostConfig
    rerank: RerankConfig
    content_quality: ContentQualityBoostConfig
    source_tier: SourceTierBoostConfig


def _parse_boost_overlay(retrieval: dict, defaults: RetrievalConfig) -> _BoostOverlay:
    """Parse the ``retrieval.boosts`` and ``retrieval.rerank`` blocks.

    Every parser falls back to its default config when the block is
    absent so operators see byte-for-byte pre-feature behaviour
    without an explicit opt-in.
    """
    boosts = retrieval.get("boosts", {}) or {}
    return _BoostOverlay(
        entity=_parse_entity(boosts.get("entity", {}) or {}) if boosts.get("entity") else defaults.entity,
        procedural=_parse_procedural(boosts.get(_KEY_PROCEDURAL, {}) or {})
        if boosts.get(_KEY_PROCEDURAL)
        else defaults.procedural,
        temporal=_parse_temporal(boosts.get("temporal", {}) or {}) if boosts.get("temporal") else defaults.temporal,
        rerank=_parse_rerank(retrieval.get("rerank", {}) or {}) if retrieval.get("rerank") else defaults.rerank,
        content_quality=_parse_content_quality_boost(boosts.get("content_quality", {}) or {})
        if boosts.get("content_quality")
        else defaults.content_quality_boost,
        source_tier=_parse_source_tier_boost(boosts.get("source_tier", {}) or {})
        if boosts.get("source_tier")
        else defaults.source_tier_boost,
    )


def _resolve_fusion_strategy(retrieval: dict, defaults: RetrievalConfig) -> str:
    """Resolve + validate the ``fusion_strategy`` value with fallback to default."""
    fusion = str(retrieval.get("fusion_strategy", defaults.fusion_strategy))
    if fusion not in ("bm25_primary", "rrf"):
        logger.warning("config_loader: unknown fusion_strategy %r — using default", fusion)
        return defaults.fusion_strategy
    return fusion


def _resolve_provider_name(data: dict) -> str | None:
    """Pull + sanitise the top-level ``provider:`` field."""
    raw_provider = data.get("provider")
    provider_name = str(raw_provider).strip() if raw_provider else None
    return provider_name or None


def parse_config(data: dict) -> RetrievalConfig:
    """Parse YAML dict into RetrievalConfig. Returns defaults for any missing/invalid section.

    Top-level ``provider:`` is honoured as the configured provider plugin
    name (see ``docs/architecture/provider-plugin-architecture.md``). A
    missing / blank value yields ``provider=None``; callers that depend
    on a configured provider (``kairix.core.factory.build_search_pipeline``)
    surface a typed ValueError listing the installed plugins.
    """
    retrieval = data.get("retrieval", {}) or {}
    defaults = RetrievalConfig.defaults()
    overlay = _parse_boost_overlay(retrieval, defaults)

    return RetrievalConfig(
        provider=_resolve_provider_name(data),
        fusion_strategy=_resolve_fusion_strategy(retrieval, defaults),
        rrf_k=int(retrieval.get("rrf_k", defaults.rrf_k)),
        bm25_limit=int(retrieval.get("bm25_limit", defaults.bm25_limit)),
        vec_limit=int(retrieval.get("vec_limit", defaults.vec_limit)),
        entity=overlay.entity,
        procedural=overlay.procedural,
        temporal=overlay.temporal,
        rerank=overlay.rerank,
        content_quality_boost=overlay.content_quality,
        source_tier_boost=overlay.source_tier,
        fact_layer_min_floor=float(retrieval.get("fact_layer_min_floor", defaults.fact_layer_min_floor)),
        chunk_layer_min_floor=float(retrieval.get("chunk_layer_min_floor", defaults.chunk_layer_min_floor)),
        cross_layer_dedup_enabled=bool(retrieval.get("cross_layer_dedup_enabled", defaults.cross_layer_dedup_enabled)),
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

    ``tier`` (Issue #432) classifies the collection for source-tier-aware
    ranking. Allowed values: ``"canonical"``, ``"active_standard"``,
    ``"vault_active"``, ``"reference"``, ``"archived"``. When absent (the
    default), :class:`SourceTierBoost` falls back to
    :attr:`SourceTierBoostConfig.default_tier` (``vault_active``,
    multiplier x1.0) — preserves pre-#432 ranking byte-for-byte.
    """

    name: str
    path: str  # relative to document_root
    glob: str = "**/*.md"
    exclude: tuple[str, ...] = ()
    in_default: bool = True
    retrieval_overrides: dict | None = None  # per-collection retrieval config (raw YAML dict)
    tier: str | None = None  # Issue #432 — source-tier classification


@dataclass(frozen=True)
class CollectionsConfig:
    """Parsed collections configuration.

    ``shared`` is stored as a tuple — frozen at construction — so callers
    cannot mutate the collection list after the boundary parses YAML. The
    FS document-scanner (``kairix/core/embed/use_cases.py``) reads
    ``shared`` to drive the scan walk (name / path / glob per collection);
    default-scope membership is now governed by the canonical topology
    scope-profile resolver, not by a predicate on this config.
    """

    shared: tuple[CollectionDef, ...]
    agent_pattern: str = "{agent}-memory"
    agent_paths: dict[str, str] = field(default_factory=dict)


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


def _coerce_str_tuple(value: object, *, key: str) -> tuple[str, ...]:
    """Strictly parse an optional YAML list of strings."""
    if value is None:
        return ()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ConfigValidationError(
        f"kairix.config.yaml: {key}={value!r} must be a list of strings, "
        f"not {type(value).__name__}. Use YAML list syntax, e.g. `exclude: [archive/]`."
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
                exclude=_coerce_str_tuple(
                    item.get("exclude"),
                    key=f"collections.shared[{index}].exclude",
                ),
                in_default=in_default,
                retrieval_overrides=item.get("retrieval"),
                tier=item.get("tier"),  # Issue #432 — source-tier metadata
            )
        )

    return CollectionsConfig(
        shared=tuple(shared),
        agent_pattern=collections.get("agent_pattern", "{agent}-memory"),
        agent_paths=collections.get("agent_paths", {}),
    )


def load_collections(config_path: Path | str | None = None) -> CollectionsConfig | None:
    """Load collections config from YAML. Returns None if not configured.

    Args:
        config_path: Optional explicit path to a YAML config file (F2-clean
            test seam). When ``None``, the env / cwd resolution chain in
            :func:`resolve_config_path` applies — preserving production
            behaviour for callers that omit the kwarg.
    """
    path = resolve_config_path(config_path)
    if path is None:
        return None
    try:
        import yaml

        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return parse_collections(data)
    except Exception:
        return None


# ── Reference-library index mode (#475) ─────────────────────────────────────

REFLIB_INDEX_EAGER = "eager"
REFLIB_INDEX_LAZY = "lazy"
REFLIB_INDEX_SKIP = "skip"
VALID_REFLIB_INDEX_MODES = (REFLIB_INDEX_EAGER, REFLIB_INDEX_LAZY, REFLIB_INDEX_SKIP)

# F17 — the YAML key appears in parser, loader, and error strings.
_KEY_REFERENCE_LIBRARY = "reference_library"


@dataclass(frozen=True)
class ReferenceLibraryConfig:
    """Parsed ``reference_library:`` block from kairix.config.yaml (#475).

    ``index`` controls how the bundled reference library participates in
    the embed/ingest walk:

    * ``eager`` (default — today's behaviour): the library is scanned and
      embedded alongside the operator's own documents. User documents
      still embed first within a run (user-docs-first ordering).
    * ``lazy``: the library's chunks embed only in a run where no user
      documents are pending — a first boot embeds the user's documents,
      and the library catches up on subsequent runs.
    * ``skip``: the library never joins the scan walk and is never
      embedded.

    Default-safe: an absent block parses to ``eager`` so existing
    deployments are unchanged.
    """

    index: str = REFLIB_INDEX_EAGER


def parse_reference_library(data: dict) -> ReferenceLibraryConfig:
    """Parse the ``reference_library:`` block. Absent block → eager default.

    Raises :class:`ConfigValidationError` (with an F21-shaped remediation
    message) when the block is present but malformed — per this module's
    contract, invalid declared values must surface loudly rather than
    silently falling back: an operator who asked for ``skip`` and got a
    silent ``eager`` would re-embed the entire bundled library.
    """
    block = data.get(_KEY_REFERENCE_LIBRARY)
    if block is None:
        return ReferenceLibraryConfig()
    if not isinstance(block, dict):
        raise ConfigValidationError(
            f"{_DEFAULT_CONFIG_FILENAME}: {_KEY_REFERENCE_LIBRARY} must be a mapping. "
            f"fix: declare it as a block with an `index:` key, e.g.\n"
            f"  {_KEY_REFERENCE_LIBRARY}:\n    index: {REFLIB_INDEX_EAGER}\n"
            "next: kairix config validate. "
            "run: kairix config validate"
        )
    mode = block.get("index", REFLIB_INDEX_EAGER)
    if mode not in VALID_REFLIB_INDEX_MODES:
        raise ConfigValidationError(
            f"{_DEFAULT_CONFIG_FILENAME}: {_KEY_REFERENCE_LIBRARY}.index={mode!r} is not a valid mode — "
            f"valid options: {REFLIB_INDEX_EAGER} | {REFLIB_INDEX_LAZY} | {REFLIB_INDEX_SKIP}. "
            f"fix: set index to {REFLIB_INDEX_EAGER} (bundled library embeds with your documents — default), "
            f"{REFLIB_INDEX_LAZY} (your documents embed first; the library follows on later runs), or "
            f"{REFLIB_INDEX_SKIP} (the library is never embedded). "
            "next: kairix config validate. "
            "run: docker compose restart kairix kairix-worker"
        )
    return ReferenceLibraryConfig(index=mode)


def load_reference_library(config_path: Path | str | None = None) -> ReferenceLibraryConfig:
    """Load the reference-library config from YAML (#475).

    Mirrors :func:`load_collections`: ``config_path`` is the F2-clean
    test seam; ``None`` follows the env / cwd resolution chain. A missing
    or unreadable file returns the eager default. A present-but-invalid
    block raises :class:`ConfigValidationError` (see
    :func:`parse_reference_library`).
    """
    path = resolve_config_path(config_path)
    if path is None:
        return ReferenceLibraryConfig()
    try:
        import yaml

        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return ReferenceLibraryConfig()
    return parse_reference_library(data)


def load_canonical_entities(config_path: Path | str | None = None) -> list:
    """Resolve the canonical entities to seed (#431 + #467).

    Merges the built-in first-party seed (``Kairix`` + ``Three Cubes``,
    always present so ``facts_about('Kairix')`` resolves on a fresh
    install — #467) UNDER the operator's ``canonical_entities:`` YAML
    block. The operator's declarations win on name conflict, so an
    operator can override a built-in summary but never loses ``Kairix``.

    Never raises — an absent, missing, or malformed config degrades to
    the built-in floor. The worker boot stage feeds the result straight
    to :func:`seed_canonical_entities` which is itself failure-isolated.

    The ``config_path`` kwarg is the F2-clean test seam mirroring
    :func:`load_collections`.
    """
    from kairix.knowledge.entities.canonical import (
        merge_canonical_entities,
        parse_canonical_entities,
    )

    operator_declared: list = []
    path = resolve_config_path(config_path)
    if path is not None:
        try:
            import yaml

            with path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            operator_declared = parse_canonical_entities(data.get("canonical_entities"))
        except Exception as exc:
            logger.warning("load_canonical_entities: failed to read %s — %s", path, exc)
            operator_declared = []
    return merge_canonical_entities(operator_declared)


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
    date_path = d.get(_KEY_DATE_PATH_BOOST, {}) or {}
    chunk_date = d.get(_KEY_CHUNK_DATE_BOOST, {}) or {}
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


def _parse_content_quality_boost(d: dict) -> ContentQualityBoostConfig:
    """Parse ``retrieval.boosts.content_quality:`` YAML block (#458).

    Operators flip ``enabled: true`` and optionally tune the three
    signal shapes. All bounded ranges are honoured at the config-level
    default so an out-of-range YAML override falls back to the
    canonical bounds.
    """
    defaults = ContentQualityBoostConfig()
    return ContentQualityBoostConfig(
        enabled=bool(d.get("enabled", defaults.enabled)),
        length_sigmoid_midpoint_chars=int(
            d.get("length_sigmoid_midpoint_chars", defaults.length_sigmoid_midpoint_chars)
        ),
        length_sigmoid_scale_chars=int(d.get("length_sigmoid_scale_chars", defaults.length_sigmoid_scale_chars)),
        length_stub_floor=float(d.get("length_stub_floor", defaults.length_stub_floor)),
        length_substantive_ceiling=float(d.get("length_substantive_ceiling", defaults.length_substantive_ceiling)),
        structure_log_scale=float(d.get("structure_log_scale", defaults.structure_log_scale)),
        structure_ceiling=float(d.get("structure_ceiling", defaults.structure_ceiling)),
        recency_decay_halflife_days=int(d.get("recency_decay_halflife_days", defaults.recency_decay_halflife_days)),
        recency_floor=float(d.get("recency_floor", defaults.recency_floor)),
        recency_neutral=float(d.get("recency_neutral", defaults.recency_neutral)),
    )


def _parse_source_tier_override(entry: dict) -> tuple[str, SourceTier, float] | None:
    """Parse one ``per_intent_overrides`` entry into ``(intent, tier, multiplier)``.

    Returns ``None`` (after a warning) when a required field is missing,
    the tier is unknown, or the multiplier is not a number — the caller
    skips the entry.
    """
    intent_value = str(entry.get("intent", "")).strip()
    tier_value = str(entry.get("tier", "")).strip()
    multiplier_value = entry.get("multiplier")
    if not intent_value or not tier_value or multiplier_value is None:
        logger.warning(
            "config_loader: source_tier per_intent_overrides entry missing required field — skipping: %r",
            entry,
        )
        return None
    try:
        tier_enum = SourceTier(tier_value)
    except ValueError:
        logger.warning(
            "config_loader: source_tier per_intent_overrides unknown tier %r — skipping",
            tier_value,
        )
        return None
    try:
        multiplier_float = float(multiplier_value)
    except (TypeError, ValueError):
        logger.warning(
            "config_loader: source_tier per_intent_overrides bad multiplier %r — skipping",
            multiplier_value,
        )
        return None
    return intent_value, tier_enum, multiplier_float


def _parse_source_tier_boost(d: dict) -> SourceTierBoostConfig:
    """Parse ``retrieval.boosts.source_tier:`` YAML block (#432).

    Knobs honoured:
      * ``enabled`` (bool) — flip the boost on/off.
      * ``canonical_filename_allowlist`` (list[str]) — file-suffix
        overrides that force the chunk to the ``canonical`` tier
        regardless of its collection.
      * ``per_intent_overrides`` (list[dict]) — per-intent multiplier
        table. Each entry: ``{intent: str, tier: str, multiplier: float}``.

    The base multiplier table + default tier stay at canonical
    defaults — tuning every multiplier per deployment is a future
    slice; operators today get the binary on/off + the two
    discriminating overrides.
    """
    defaults = SourceTierBoostConfig()
    allowlist_raw = d.get("canonical_filename_allowlist") or ()
    allowlist = tuple(str(x) for x in allowlist_raw if x)

    overrides_raw = d.get("per_intent_overrides") or []
    overrides: list[tuple[str, SourceTier, float]] = []
    for entry in overrides_raw:
        if not isinstance(entry, dict):
            continue
        parsed = _parse_source_tier_override(entry)
        if parsed is not None:
            overrides.append(parsed)

    return SourceTierBoostConfig(
        enabled=bool(d.get("enabled", defaults.enabled)),
        multipliers=defaults.multipliers,
        default_tier=defaults.default_tier,
        canonical_filename_allowlist=allowlist,
        per_intent_overrides=tuple(overrides),
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
    if _KEY_RERANK_INTENTS in overrides:
        intents = overrides[_KEY_RERANK_INTENTS] or []
        out[_KEY_RERANK_INTENTS] = tuple(str(x) for x in intents)
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
        _KEY_DATE_PATH_BOOST: {
            "enabled": base.temporal.date_path_boost_enabled,
            "factor": base.temporal.date_path_boost_factor,
            "recency_window_days": base.temporal.date_path_recency_window_days,
        },
        _KEY_CHUNK_DATE_BOOST: {
            "enabled": base.temporal.chunk_date_boost_enabled,
            "decay_halflife_days": base.temporal.chunk_date_decay_halflife_days,
            "guard_explicit_only": base.temporal.chunk_date_boost_guard_explicit_only,
        },
    }
    user_temporal = override or {}
    merged: dict[str, Any] = dict(base_temporal_dict)
    for sub_key in (_KEY_DATE_PATH_BOOST, _KEY_CHUNK_DATE_BOOST):
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
    if _KEY_PROCEDURAL in boosts:
        top_fields[_KEY_PROCEDURAL] = _merge_procedural_boost(base, boosts[_KEY_PROCEDURAL])
    if "temporal" in boosts:
        top_fields["temporal"] = _merge_temporal_boost(base, boosts["temporal"])

    rerank = overrides.get("rerank", {})
    if rerank:
        top_fields["rerank"] = _merge_rerank(base, rerank)

    return replace(base, **top_fields) if top_fields else base


def _topology_collection_overrides() -> dict[str, dict]:
    """Load per-collection retrieval override dicts from the canonical topology.

    Delegates to :func:`kairix.core.factory.derive_collection_overrides`,
    which reads ``topology.collections[*].retrieval`` from the
    overlay-aware merged config — the same canonical source the
    ranking-tier map derives from (canonical-collapse). The import is lazy
    to avoid a config_loader → factory import cycle (factory imports
    config_loader at resolution time). Returns
    ``{collection_name: override_dict}``; empty when no collection
    declares a ``retrieval:`` block so ``resolve_retrieval_config`` falls
    back to the global config.
    """
    from kairix.core.factory import derive_collection_overrides

    return derive_collection_overrides()


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
    overrides_fn: Callable[[], dict[str, dict]] = field(default_factory=lambda: _topology_collection_overrides)


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
