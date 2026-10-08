"""Recommender use case — rank capabilities for a described task.

The hot path of the capability recommender (Spec A,
``docs/architecture/capability-recommender/recommender-mvp-design.md``).
Given a task description, ``run_recommend`` retrieves over the
capability-bearing search collections (BM25 + vector RRF + force-enabled
cross-encoder rerank), maps each hit to a ranked
:class:`CapabilityRecommendation` carrying a ready-to-call invocation, and
returns a :class:`RecommendOutput`. No LLM sits between the agent and its
tools.

The corpus is fed by two feeders, each writing to its *natural*
collection: Feeder 1 (kairix's own ``tool_capabilities()`` catalogue) ->
the ``capabilities`` collection; Feeder 2 (the local ``skills`` connector
over ``~/.claude``) -> the ``skills`` collection (the connector framework
names a connector's collection after the connector). The recommender ranks
over BOTH (:data:`_CAPABILITY_COLLECTIONS`) so external skills are
reachable in production, not just kairix caps. This module only *reads*
the collections; the surfaces (CLI ``kairix recommend`` + the
``recommend_capabilities`` MCP tool) are thin adapters added later.

Two scope namespaces appear in capability ids:

* ``capability://kairix/<name>`` — a kairix tool (CLI and/or MCP). The
  invocation (``mcp_tool`` / ``cli``) + ``when_to_use`` are enriched from
  the in-process catalogue keyed by name.
* ``capability://{skill,command,agent}/<name>`` — an external local
  capability. ``when_to_use`` comes from the retrieval snippet/content;
  there is no kairix ``mcp_tool`` / ``cli`` binding.

The use case **never raises** — every failure mode populates
:attr:`RecommendOutput.error` and returns an otherwise-empty result, per
the use-cases-never-raise contract.

Sabotage-proof log (executed mutate -> fail -> restore): see the test
module ``tests/use_cases/test_recommend.py``
(``test_run_recommend_records_explicit_collection_contract``).
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

logger = logging.getLogger(__name__)

# The feature flag gating both recommender surfaces. The gate lives at the
# ADAPTER level (CLI ``main`` + MCP ``tool_recommend_capabilities``), NOT inside
# ``run_recommend`` — the use case stays flag-agnostic so it composes the
# same way in tests and behind either surface. When the flag is OFF, the
# adapter returns this disabled message WITHOUT calling ``run_recommend``.
_RECOMMENDER_FLAG = "recommender"
# Reads as a deployment state directed at the operator, NOT an instruction to
# the agent (agents can't flip flags; an operator turns it on). Keeps the
# "recommender is disabled" lead so an agent that gets this envelope treats the
# capability as absent-here rather than live. PLA-299: stop advertising the
# recommender as a live capability while its flag defaults OFF.
RECOMMENDER_DISABLED_ERROR = (
    "recommender is disabled — not enabled on this deployment "
    "(an operator turns it on via the 'recommender' feature flag)"
)

# The capability-bearing collections the recommender retrieves over. The
# two feeders write to their *natural* collections: kairix's own caps
# (Feeder 1, ``CapabilityCatalogueBuilder``) land in ``capabilities``; the
# external skills connector (Feeder 2) lands in ``skills`` — the connector
# framework routes a connector's output to a collection named after the
# connector (``run_connector_sync_pipeline`` ->
# ``resolve_chunk_writer_for_entry(db, name="skills")`` ->
# ``legacy_chunk_writer(db, collection="skills")``). The recommender ranks
# over BOTH so external skills are reachable in production, not just kairix
# caps. ``agent=None`` makes the pipeline use this list verbatim (both
# collections are globally readable and need not be registered in
# collections.yaml). ``_CAPABILITIES_COLLECTION`` stays the canonical name
# for the kairix-native half (the corpus builder + the ``--db-path`` seam
# write there).
_CAPABILITIES_COLLECTION = "capabilities"
_SKILLS_COLLECTION = "skills"
_CAPABILITY_COLLECTIONS = [_CAPABILITIES_COLLECTION, _SKILLS_COLLECTION]

# Capability-id scheme: ``capability://<scope>/<name>`` (an optional
# ``#<seq>`` chunk suffix is stripped). The ``kairix`` scope maps to a
# kairix tool; the others are external local capabilities.
_CAPABILITY_URI_PREFIX = "capability://"
_KAIRIX_SCOPE = "kairix"
_KIND_TOOL = "tool"
_SURFACE_EXTERNAL = "external"

# The recommender excludes itself from its own results (self-reference
# guard) — an agent asking "which tool fits this task" never wants
# "use the recommender" back.
_SELF_REFERENCE_NAME = "recommend"

# F17 — envelope keys appear in the per-recommendation projection AND the
# top-level envelope writer; extract so a rename is a single edit site.
_KEY_NAME = "name"
_KEY_KIND = "kind"
_KEY_SURFACE = "surface"
_KEY_WHEN_TO_USE = "when_to_use"
_KEY_SCORE = "score"
_KEY_MCP_TOOL = "mcp_tool"
_KEY_CLI = "cli"
_KEY_SOURCE = "source"


# ---------------------------------------------------------------------------
# Result + deps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityRecommendation:
    """One ranked capability, ready to call.

    Attributes:
        name: The capability's short name (the agent's call target).
        kind: ``"tool"`` (kairix) | ``"skill"`` | ``"command"`` | ``"agent"``.
        surface: ``"mcp"`` | ``"cli"`` | ``"both"`` (kairix tools) |
            ``"external"`` (local skills/commands/agents).
        when_to_use: Task-conditioned trigger text — why reach for this.
        score: The retrieval/rerank score (best-first ordering).
        mcp_tool: The MCP tool binding, empty when not applicable.
        cli: The CLI invocation (e.g. ``"kairix search"``), empty when N/A.
        source: Plugin/version provenance for external caps, else empty.
    """

    name: str
    kind: str
    surface: str
    when_to_use: str
    score: float
    mcp_tool: str = ""
    cli: str = ""
    source: str = ""


@dataclass(frozen=True)
class RecommendOutput:
    """Outcome of one ``run_recommend`` invocation.

    Attributes:
        task: The caller's task description, unchanged.
        recommendations: Up to ``limit`` ranked capabilities, best-first.
        correlation_id: A per-call id (forward-compat: Spec B keys its
            outcome log on this). Minted even on failure so the shape is
            stable.
        error: Empty on success; an operator/agent-actionable message or a
            structured ``"<Class>: <msg>"`` on a top-level failure.
    """

    task: str = ""
    recommendations: tuple[CapabilityRecommendation, ...] = ()
    correlation_id: str = ""
    error: str = ""


def recommender_config(base: Any) -> Any:
    """Return ``base`` with cross-encoder rerank force-enabled.

    Cross-encoder rerank is force-enabled for the ``capabilities``
    collection (precision@1-3 over a small corpus matters more than
    recall). ``replace`` over a frozen :class:`RetrievalConfig` yields a
    distinct value, which the pipeline factory caches in its own bucket —
    no collision with the main search pipeline. Public so the
    force-rerank contract is unit-pinnable without a provider.
    """
    from dataclasses import replace

    from kairix.core.search.config import RerankConfig

    return replace(base, rerank=RerankConfig(enabled=True))


def _pipeline_search(pipeline: Any, **kwargs: Any) -> Any:
    """Call a real ``SearchPipeline.search`` with only the kwargs it accepts.

    ``run_recommend`` calls its ``search_fn`` with ``query`` / ``collections``
    / ``agent`` / ``limit``; the real :meth:`SearchPipeline.search` has no
    ``limit`` parameter (top-k truncation is applied downstream in
    ``_map_results``). Forward only the production-signature kwargs so the
    real-pipeline seams compose without a ``TypeError``. ``FakeSearchPipeline``
    accepts ``**kwargs`` directly, so this shim is only on the real path.
    """
    return pipeline.search(
        query=kwargs["query"],
        collections=kwargs.get("collections"),
        agent=kwargs.get("agent"),
    )


def _default_search(**kwargs: Any) -> Any:
    """Lazy DI default: the production search pipeline, rerank force-on.

    ``load_config()`` returns a ``RetrievalConfig`` (verified:
    ``kairix/core/search/config_loader.py`` ``load_config`` ->
    ``_load_cached_layered``/``load_cached``, both ``-> RetrievalConfig``),
    so :func:`recommender_config`'s ``replace(...)`` is valid.
    """
    from kairix.core.factory import build_search_pipeline
    from kairix.core.search.config_loader import load_config

    cfg = recommender_config(load_config())
    return _pipeline_search(build_search_pipeline(config=cfg), **kwargs)


def _default_catalogue() -> list[dict[str, Any]]:
    """Lazy DI default: kairix's own capability catalogue (name->row enrich)."""
    from kairix.agents.mcp.server import tool_capabilities

    return list(tool_capabilities()["capabilities"])


def _default_correlation_id() -> str:
    """Lazy DI default: a fresh uuid4 hex (tests inject a fixed value)."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class RecommendDeps:
    """Injectable dependencies for ``run_recommend``.

    Non-Optional fields wired to production defaults via
    ``default_factory`` — the #204 frozen-deps pattern (never
    ``Optional[Callable]``). Tests construct
    ``RecommendDeps(search_fn=fake, ...)`` with explicit overrides;
    ``RecommendDeps()`` resolves to the module-level ``_default_*``
    callables.
    """

    search_fn: Callable[..., Any] = field(default_factory=lambda: _default_search)
    catalogue_fn: Callable[[], list[dict[str, Any]]] = field(default_factory=lambda: _default_catalogue)
    correlation_id_fn: Callable[[], str] = field(default_factory=lambda: _default_correlation_id)


# ---------------------------------------------------------------------------
# Hit -> recommendation mapping
# ---------------------------------------------------------------------------


def _parse_capability_uri(path: str) -> tuple[str, str] | None:
    """Parse ``capability://<scope>/<name>`` -> ``(scope, name)``.

    A trailing ``#<seq>`` chunk suffix is stripped. Returns ``None`` for a
    path that isn't a capability URI (the caller drops it rather than
    failing the whole call).
    """
    if not path.startswith(_CAPABILITY_URI_PREFIX):
        return None
    rest = path[len(_CAPABILITY_URI_PREFIX) :].split("#", 1)[0]
    scope, sep, name = rest.partition("/")
    if not sep or not scope or not name:
        return None
    return scope, name


def _row_score(inner: Any) -> float:
    """Best available score: rerank > boosted > rrf (all default 0.0)."""
    score = (
        getattr(inner, "rerank_score", 0.0) or getattr(inner, "boosted_score", 0.0) or getattr(inner, "rrf_score", 0.0)
    )
    return float(score or 0.0)


def _hit_text(budgeted: Any, inner: Any) -> str:
    """The retrieval text for a hit — budget-trimmed content, then snippet."""
    return str(getattr(budgeted, "content", "") or getattr(inner, "snippet", "") or "")


def _kairix_recommendation(name: str, score: float, catalogue: dict[str, dict[str, Any]]) -> CapabilityRecommendation:
    """Build a kairix-tool recommendation, enriched from the catalogue row."""
    row = catalogue.get(name, {})
    mcp_tool = str(row.get("mcp_tool") or "")
    cli = str(row.get("cli") or "")
    return CapabilityRecommendation(
        name=name,
        kind=_KIND_TOOL,
        surface=_surface_for(mcp_tool, cli),
        when_to_use=str(row.get("when_to_use", "")),
        score=score,
        mcp_tool=mcp_tool,
        cli=cli,
    )


def _external_recommendation(scope: str, name: str, score: float, when_to_use: str) -> CapabilityRecommendation:
    """Build an external-capability recommendation (skill/command/agent)."""
    return CapabilityRecommendation(
        name=name,
        kind=scope,
        surface=_SURFACE_EXTERNAL,
        when_to_use=when_to_use,
        score=score,
    )


def _surface_for(mcp_tool: str, cli: str) -> str:
    """Derive a kairix tool's binding surface from its invocations."""
    if mcp_tool and cli:
        return "both"
    if mcp_tool:
        return "mcp"
    return "cli"


def _hit_to_recommendation(budgeted: Any, catalogue: dict[str, dict[str, Any]]) -> CapabilityRecommendation | None:
    """Map one ``BudgetedResult`` to a recommendation, or ``None`` to drop it.

    Drops the hit when the path isn't a capability URI or when it is the
    recommender's own self-reference.
    """
    inner = getattr(budgeted, "result", None)
    if inner is None:
        return None
    parsed = _parse_capability_uri(str(getattr(inner, "path", "")))
    if parsed is None:
        return None
    scope, name = parsed
    if name == _SELF_REFERENCE_NAME:
        return None
    score = _row_score(inner)
    if scope == _KAIRIX_SCOPE:
        return _kairix_recommendation(name, score, catalogue)
    return _external_recommendation(scope, name, score, _hit_text(budgeted, inner))


def _map_results(
    results: Any, catalogue: dict[str, dict[str, Any]], limit: int
) -> tuple[CapabilityRecommendation, ...]:
    """Map the pipeline's hits to up to ``limit`` recommendations."""
    out: list[CapabilityRecommendation] = []
    for budgeted in results:
        rec = _hit_to_recommendation(budgeted, catalogue)
        if rec is not None:
            out.append(rec)
        if len(out) >= limit:
            break
    return tuple(out)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def run_recommend(
    task: str,
    *,
    agent: str | None = None,
    limit: int = 5,
    deps: RecommendDeps | None = None,
) -> RecommendOutput:
    """Rank capabilities for a described task.

    Never raises — failures populate :attr:`RecommendOutput.error` and
    return an otherwise-empty result.

    Args:
        task: Natural-language description of what the agent wants to do.
        agent: Accepted for parity / forward use (an agent's available
            toolset may differ); v1 does not personalise ranking. The
            *capability query* always runs with ``agent=None`` so the
            unregistered capability-bearing collections
            (:data:`_CAPABILITY_COLLECTIONS`) are used verbatim.
        limit: Maximum number of recommendations returned.
        deps: Injectable dependencies; production callers leave None.
    """
    d = deps or RecommendDeps()
    correlation_id = d.correlation_id_fn()
    # ``agent`` is accepted for surface parity + forward use (Spec B keys its
    # outcome log on the requesting agent). v1 records it but does not
    # personalise ranking — the capability query always runs with
    # ``agent=None`` so the unregistered capability-bearing collections
    # (``_CAPABILITY_COLLECTIONS``) are used verbatim.
    logger.debug("run_recommend: agent=%s correlation_id=%s", agent, correlation_id)
    try:
        sr = d.search_fn(query=task, collections=_CAPABILITY_COLLECTIONS, agent=None, limit=limit)
        catalogue = {row["name"]: row for row in d.catalogue_fn()}
        recommendations = _map_results(getattr(sr, "results", []), catalogue, limit)
        return RecommendOutput(task=task, recommendations=recommendations, correlation_id=correlation_id)
    except Exception as exc:  # never raise — surface via .error
        # exc_info=True so the stack trace reaches the logs on this swallow
        # path; ``error`` only carries type+message. Pinned by a caplog test.
        logger.warning("run_recommend failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        return RecommendOutput(task=task, correlation_id=correlation_id, error=f"{type(exc).__name__}: {exc}")


def recommend_output_to_envelope(out: RecommendOutput) -> dict[str, Any]:
    """Project a ``RecommendOutput`` to the JSON envelope callers receive.

    Both adapters (the CLI ``--json`` path and the MCP ``tool_recommend_capabilities``)
    serialise from this helper so the envelope shape has one definition.
    """
    return {
        "task": out.task,
        "recommendations": [
            {
                _KEY_NAME: r.name,
                _KEY_KIND: r.kind,
                _KEY_SURFACE: r.surface,
                _KEY_WHEN_TO_USE: r.when_to_use,
                _KEY_SCORE: r.score,
                _KEY_MCP_TOOL: r.mcp_tool,
                _KEY_CLI: r.cli,
                _KEY_SOURCE: r.source,
            }
            for r in out.recommendations
        ],
        "correlation_id": out.correlation_id,
        "error": out.error,
    }


# ---------------------------------------------------------------------------
# Adapter-level flag gate (CLI + MCP)
# ---------------------------------------------------------------------------


def default_recommender_flag_reader() -> bool:
    """Production flag-reader for the ``recommender`` flag.

    Thin wrapper around :func:`kairix.core.features.resolver.flag` so the
    adapters (CLI :func:`main` + MCP ``tool_recommend_capabilities``) inject a fake
    reader without monkey-patching the resolver module (F1/F2-clean).
    Cloned from
    :func:`kairix.core.search.boosts.default_entity_first_routing_flag_reader`.
    """
    from kairix.core.features.resolver import flag

    return flag(_RECOMMENDER_FLAG)


def recommender_disabled_output(task: str) -> RecommendOutput:
    """The disabled-state result both adapters return when the flag is OFF.

    Single source for the disabled envelope so the CLI and MCP surfaces
    stay byte-identical. Carries the empty recommendation tuple +
    :data:`RECOMMENDER_DISABLED_ERROR`; no ``correlation_id`` is minted
    because no recommendation call happened.
    """
    return RecommendOutput(task=task, error=RECOMMENDER_DISABLED_ERROR)


# ---------------------------------------------------------------------------
# CLI surface — kairix recommend
# ---------------------------------------------------------------------------


def _build_parser() -> Any:
    """Argparse for ``kairix recommend <task...> [--json] [--limit] [--db-path]``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="kairix recommend",
        description=(
            "Recommend which kairix tool or local skill fits a task. Describe "
            "the task; get a ranked list of capabilities, each with why it fits "
            "and a ready-to-call invocation."
        ),
    )
    parser.add_argument("task", nargs="+", help="The task you want to do, in plain words.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the structured JSON envelope instead of the human-readable table.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of recommendations to return (default: 5).",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help=(
            "Override the index database path for this invocation — points the "
            "capability search at a pre-built tmp index. Read-only; this is the "
            "F30 subprocess seam (no KAIRIX_* env vars needed)."
        ),
    )
    return parser


class _NullEmbeddingService:
    """A no-op ``EmbeddingService`` for the BM25-only ``--db-path`` seam.

    The F30 read-only seam runs ``skip_vector=True`` so the vector leg's
    embed service is never invoked at query time — but the factory still
    constructs one. This null service satisfies the
    :class:`kairix.core.protocols.EmbeddingService` shape (``embed`` /
    ``embed_batch``) without resolving a provider, so the read-only path is
    provider-free on any host. It returns empty vectors, never a network
    call.
    """

    def embed(self, _text: str) -> list[float]:
        # _-prefixed: the EmbeddingService Protocol requires the positional
        # ``text``, but the null service ignores it (F19).
        return []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


def read_only_db_search_config() -> Any:
    """Return the BM25-only retrieval config for the ``--db-path`` seam.

    Rerank force-on (``recommender_config``) AND ``skip_vector=True`` — the
    read-only F30 seam must NOT attempt the vector leg (it has no provider).
    Public so the BM25-only contract is unit-pinnable without a provider:
    ``skip_vector`` MUST be True or the seam tries to embed against a
    missing provider. Mirrors :func:`recommender_config`'s testable shape.
    """
    from dataclasses import replace

    from kairix.core.search.config import RetrievalConfig

    return replace(recommender_config(RetrievalConfig.defaults()), skip_vector=True)


def _db_path_search_fn(db_path: str) -> Callable[..., Any]:
    """Build a read-only ``search_fn`` pointed at a pre-built tmp index.

    The F30 subprocess seam: wires :func:`build_search_pipeline` against a
    :class:`kairix.paths.KairixPaths` whose ``db_path`` is the supplied
    path so a subprocess can drive the composed search against a seeded
    ``capabilities`` collection without touching the environment. The
    config is :func:`read_only_db_search_config` (BM25-only, rerank on),
    and the embed service is overridden with
    :class:`_NullEmbeddingService` — the seam is provider-free by
    construction (no ``provider:`` field required), so the read-only path
    runs on any host. The cross-encoder is wired off
    (``RERANK_DISABLED``) for the same reason: the production reranker's
    first call imports torch and downloads its model from the Hugging Face
    hub, so leaving it on made this "any host" seam network-bound (~20-50s
    cold, the #493 flake class). Production callers leave ``deps`` None and
    reach the full hybrid ``_default_search`` (rerank included) instead.
    """
    from pathlib import Path

    def _search(**kwargs: Any) -> Any:
        from kairix.core.factory import RERANK_DISABLED, FactoryDeps, build_search_pipeline
        from kairix.paths import KairixPaths

        cfg = read_only_db_search_config()
        resolved = Path(db_path)
        paths = KairixPaths(
            db_path=resolved,
            document_root=resolved.parent,
            log_dir=resolved.parent,
            workspace_root=resolved.parent,
        )
        deps = FactoryDeps(embed_service_override=_NullEmbeddingService(), reranker_override=RERANK_DISABLED)
        pipeline = build_search_pipeline(config=cfg, paths=paths, deps=deps)
        return _pipeline_search(pipeline, **kwargs)

    return _search


def _deps_from_args(args: Any) -> RecommendDeps | None:
    """Build override deps from the F30 ``--db-path`` subprocess seam, or None."""
    if args.db_path:
        return RecommendDeps(search_fn=_db_path_search_fn(args.db_path))
    return None


def _format_human(out: RecommendOutput) -> str:
    """Human-readable table for the default (non-``--json``) output."""
    lines = [f"Recommendations for: {out.task}"]
    if not out.recommendations:
        lines.append("  (no matching capabilities found)")
        return "\n".join(lines) + "\n"
    for rank, rec in enumerate(out.recommendations, start=1):
        invocation = rec.mcp_tool or rec.cli or rec.name
        lines.append(f"  {rank}. {rec.name} ({rec.kind}) — call: {invocation}")
        if rec.when_to_use:
            lines.append(f"     when: {rec.when_to_use}")
    return "\n".join(lines) + "\n"


def main(
    argv: list[str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    deps: RecommendDeps | None = None,
    flag_reader: Callable[[], bool] = default_recommender_flag_reader,
) -> int:
    """CLI entry point for ``kairix recommend``. Returns 0 on success, 1 on error.

    Flag-gated at THIS adapter level (not inside ``run_recommend``): when
    the ``recommender`` flag is OFF, prints the disabled message to stderr
    and returns 1 WITHOUT calling ``run_recommend``. ``deps`` is the
    in-process test seam; ``--db-path`` is the F30 subprocess seam;
    ``flag_reader`` is the flag DI seam (default reads ``flag("recommender")``).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    out_sink = out if out is not None else sys.stdout
    err_sink = err if err is not None else sys.stderr

    task = " ".join(args.task)

    if not flag_reader():
        result = recommender_disabled_output(task)
    else:
        effective_deps = deps if deps is not None else _deps_from_args(args)
        result = run_recommend(task, limit=args.limit, deps=effective_deps)

    if args.as_json:
        out_sink.write(json.dumps(recommend_output_to_envelope(result), indent=2) + "\n")
    elif not result.error:
        out_sink.write(_format_human(result))

    if result.error:
        err_sink.write(f"kairix recommend: {result.error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
