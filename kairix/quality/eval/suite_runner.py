"""Suite runner for the Plan B-parity ``kairix eval`` operator surface.

Discovers a conversation corpus on disk (``session-NNN.jsonl`` +
``ground-truth-queries.json`` + optional ``ground-truth-facts.json``),
ingests the sessions through the configured ``FactStore`` / ``FactExtractor``,
runs each query through the configured backend, and scores results against
ground truth.

Two metrics emerge from one suite run:

- **query-pass-rate** — per-question pass/score against the ground-truth
  answer, broken down by category (``single-hop`` / ``multi-hop`` /
  ``temporal`` / ``open-domain`` / ``adversarial``).
- **extractor-f1** — if ``ground-truth-facts.json`` is present, score the
  extractor's output via precision / recall / F1 of matched
  (entity, attribute, substring(value)) tuples.

Both metrics are emitted in a single :class:`SuiteResult` dataclass so the
operator surface and the regression-gate CI step read the same artefact.

Design contract:

- **Dependency injection is total.** ``fact_store`` / ``fact_extractor``
  / ``llm`` / ``paths`` are constructor-injected. Tests pass fakes from
  ``tests/fakes.py``; production wires the real implementations at the
  CLI layer. F1: no monkeypatching, no internal-attribute reassignment.
- **F26-clean.** Imports the ``LLMBackend`` Protocol from
  ``kairix.platform.llm.protocol`` rather than reaching into providers.
- **Hermetic ingest in tests.** Sessions are ingested into a
  caller-supplied :class:`KairixPaths` (typically ``tmp_path``-rooted)
  so a unit run never touches the operator's real document store.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kairix.core.facts.consolidation import ConsolidationPass
from kairix.core.protocols import (
    CorpusEmbedder,
    DocumentWriter,
    FactExtractor,
    FactStore,
)
from kairix.core.search.pipeline import SearchPipeline
from kairix.corpus.ingest import IngestRequest, SessionPayload, ingest_corpus
from kairix.paths import KairixPaths, agent_cli_roots, confine_to_roots
from kairix.platform.llm.protocol import LLMBackend
from kairix.quality.scoring.judge_call import call_judge, parse_judge_score
from kairix.quality.scoring.types import (
    JudgeFailedError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SuiteResult",
    "SuiteRunner",
    "SuiteSpec",
]


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


# Recognised ground-truth-queries category vocabulary. Mirrors the LoCoMo
# taxonomy and matches what reference-library/conversations/README.md
# documents. Unknown categories fall back to "uncategorised" so a
# malformed entry is visible but doesn't crash the gate.
_KNOWN_CATEGORIES: tuple[str, ...] = (
    "single-hop",
    "multi-hop",
    "temporal",
    "open-domain",
    "adversarial",
)

# Pass-threshold for query-score against ground truth. A score of >= 0.5
# is considered a pass - the LLM-judge prompt is a 0.0-1.0 graded judgement
# and 0.5 is the documented "partially correct" boundary.
_PASS_THRESHOLD: float = 0.5


@dataclass(frozen=True)
class SuiteSpec:
    """Discovered shape of a conversation corpus on disk.

    Produced by :meth:`SuiteRunner.discover_suite`; consumed by
    :meth:`SuiteRunner.run`. Kept as a frozen dataclass so test
    helpers can build one directly without going through disk.
    """

    name: str
    path: Path
    session_paths: tuple[Path, ...]
    queries: tuple[dict[str, Any], ...]
    ground_truth_facts: tuple[dict[str, Any], ...] | None


@dataclass(frozen=True)
class SuiteResult:
    """Outcome of running one suite — both metrics in one envelope.

    The shape is JSON-serialisable (every value is a primitive, dict, or
    list). The CLI ``--json`` flag round-trips this dataclass via
    :func:`dataclasses.asdict`.

    LLM-judge failures (backend down, empty / non-numeric / non-finite
    reply) are NOT scores: those questions keep a row (``score`` /
    ``pass`` are ``None``, plus ``judge_failure`` / ``judge_error``) but
    are excluded from ``n_questions``, ``n_passed``, ``mean_score`` and
    ``per_category``, and counted in ``judge_failures``. ``n_questions``
    is therefore the number of *scored* questions.
    """

    suite_name: str
    n_questions: int
    n_passed: int
    mean_score: float
    per_category: dict[str, dict[str, float]]
    per_extraction_f1: float | None
    extraction_precision: float | None
    extraction_recall: float | None
    rows: list[dict[str, Any]] = field(default_factory=list)
    judge_failures: int = 0


# ---------------------------------------------------------------------------
# Public runner
# ---------------------------------------------------------------------------


class SuiteRunner:
    """Discover + run + score a conversation eval suite.

    All collaborators are constructor-injected; the runner itself is a
    pure orchestrator. Tests pass ``FakeFactStore`` / ``FakeFactExtractor``
    / ``FakeLLMBackend``; production wires real implementations.
    """

    def __init__(
        self,
        *,
        fact_store: FactStore,
        fact_extractor: FactExtractor,
        llm: LLMBackend,
        paths: KairixPaths,
        search_pipeline: SearchPipeline | None = None,
        document_writer: DocumentWriter | None = None,
        embedder: CorpusEmbedder | None = None,
        consolidation: ConsolidationPass | None = None,
    ) -> None:
        self._fact_store = fact_store
        self._fact_extractor = fact_extractor
        self._llm = llm
        self._paths = paths
        # Plan B-parity D3 remediation — when wired, queries route through
        # the full SearchPipeline (intent classifier + fact federation +
        # fusion + budget). When ``None``, the runner uses the legacy
        # direct ``fact_store.search`` path (regression-debugging escape
        # hatch only — slated for removal in v2026.5.19).
        self._search_pipeline = search_pipeline
        # Spike C1 Phase 3 — corpus-ingest collaborators. When all three
        # are ``None`` (today's default for reference-library invocations),
        # the runner stays on the facts-only ingest path that mirrors
        # legacy ``_ingest_sessions`` behaviour. When any are set, the
        # runner routes through :func:`kairix.corpus.ingest.ingest_corpus`
        # so SuiteRunner shares the Lever A session_metadata wiring,
        # consolidation, and chunk-index update with ``kairix ingest-chat``
        # and the LoCoMo harness.
        self._document_writer = document_writer
        self._embedder = embedder
        self._consolidation = consolidation

    # -----------------------------------------------------------------
    # Discovery
    # -----------------------------------------------------------------

    def discover_suite(self, suite_path: Path) -> SuiteSpec:
        """Locate sessions + ground-truth files under ``suite_path``.

        Required: at least one ``session-*.jsonl`` AND
        ``ground-truth-queries.json``. Optional:
        ``ground-truth-facts.json`` — if absent, the run skips the
        extractor F1 metric (still emits query metrics).

        Raises:
            ValueError: with actionable ``fix:`` / ``next:`` markers
                if a required file is missing.
            PathTraversalError: when ``suite_path`` (the operator-supplied
                ``--suite`` argument) escapes the working-area allow-list.

        S8707: ``suite_path`` is the agent/operator-supplied entry point; every
        ground-truth file read (``_load_json_list``) derives from it. Confine it
        to the working-area allow-list HERE, before any glob/read, so a crafted
        ``--suite ../../etc`` is rejected rather than scanned off-tree.
        """
        suite_path = confine_to_roots(suite_path, agent_cli_roots())
        if not suite_path.exists() or not suite_path.is_dir():
            raise ValueError(
                f"Suite path {suite_path!r} does not exist or is not a directory. "
                f"fix: pass a directory under reference-library/conversations/. "
                f"next: run `ls reference-library/conversations/` to see candidates."
            )

        sessions = tuple(sorted(suite_path.glob("session-*.jsonl")))
        if not sessions:
            raise ValueError(
                f"No session-*.jsonl files found under {suite_path!r}. "
                f"fix: add at least one session-001.jsonl file with conversation turns. "
                f"next: see reference-library/conversations/README.md for the JSONL shape."
            )

        queries_path = suite_path / "ground-truth-queries.json"
        if not queries_path.exists():
            raise ValueError(
                f"Required file {queries_path!r} is missing. "
                f"fix: add a ground-truth-queries.json file with question/answer pairs. "
                f"next: see reference-library/conversations/README.md for the schema."
            )

        queries = _load_json_list(queries_path)

        facts_path = suite_path / "ground-truth-facts.json"
        gt_facts: tuple[dict[str, Any], ...] | None = None
        if facts_path.exists():
            gt_facts = _load_json_list(facts_path)

        return SuiteSpec(
            name=suite_path.name,
            path=suite_path,
            session_paths=sessions,
            queries=queries,
            ground_truth_facts=gt_facts,
        )

    # -----------------------------------------------------------------
    # Orchestration
    # -----------------------------------------------------------------

    def run(self, suite: SuiteSpec) -> SuiteResult:
        """Ingest sessions, score every query, optionally score extractor F1.

        Workflow:

        1. Read every ``session-*.jsonl`` and feed turns through the
           configured ``FactExtractor``; persist returned records via
           ``FactStore.add``.
        2. For each query in ``ground-truth-queries.json``, run a
           recall + LLM-judge pass; record per-question score + pass.
        3. If ``ground-truth-facts.json`` is present, compute F1 of
           the extractor's output against ground truth.
        """
        extracted_facts = self._ingest_sessions(suite)

        rows, per_cat = self._score_queries(suite.queries)
        # Judge failures keep their row but are excluded from every aggregate.
        scored = [r for r in rows if r["score"] is not None]
        n_questions = len(scored)
        n_passed = sum(1 for r in scored if r["pass"])
        mean_score = (sum(r["score"] for r in scored) / n_questions) if n_questions else 0.0

        f1, precision, recall = self._score_extraction(extracted_facts, suite.ground_truth_facts)

        return SuiteResult(
            suite_name=suite.name,
            n_questions=n_questions,
            n_passed=n_passed,
            mean_score=mean_score,
            per_category=per_cat,
            per_extraction_f1=f1,
            extraction_precision=precision,
            extraction_recall=recall,
            rows=rows,
            judge_failures=len(rows) - len(scored),
        )

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _ingest_sessions(self, suite: SuiteSpec) -> list[Any]:
        """Route ingest through :func:`kairix.corpus.ingest.ingest_corpus`.

        Reads every ``session-*.jsonl`` under ``suite.session_paths``,
        loads any sidecar ``.metadata.json`` (Lever A — session
        ``date_time`` etc.), then hands the batch to ``ingest_corpus``
        with the runner's configured collaborators.

        Returns the flat list of extracted facts so the extractor-F1
        scoring path can compare them against ground truth without a
        second round-trip through the store. Facts are captured by
        wrapping ``self._fact_store`` in a per-call recorder — the
        underlying store still receives every ``add`` call, but the
        captured list keeps the scorer independent of the store's
        search-shape (idempotency / namespace filtering).

        Spike C1 Phase 3 — closes the Lever A regression where
        ``self._fact_extractor.extract(turns=turns)`` was being called
        without ``session_metadata``, silently dropping the session
        ``date_time`` anchor on every reference-library conversational
        eval.
        """
        sessions = self._build_session_payloads(suite.session_paths)
        recorder = _CapturingFactStore(self._fact_store)
        request = IngestRequest(sessions=tuple(sessions), corpus_id=suite.name)
        ingest_corpus(
            request,
            paths=self._paths,
            fact_store=recorder,
            fact_extractor=self._fact_extractor,
            document_writer=self._document_writer,
            embedder=self._embedder,
            consolidation=self._consolidation,
        )
        return recorder.captured

    def _build_session_payloads(self, session_paths: Iterable[Path]) -> list[SessionPayload]:
        """Read every session JSONL into a :class:`SessionPayload`.

        Skips empty session files so the orchestrator never sees a
        zero-turn payload (matches the legacy ``if not turns: continue``
        guard).
        """
        payloads: list[SessionPayload] = []
        for sp in session_paths:
            turns = _read_session(sp)
            if not turns:
                continue
            metadata = _load_session_metadata(sp)
            payloads.append(
                SessionPayload(
                    turns=tuple(turns),
                    session_id=sp.stem,
                    metadata=metadata,
                )
            )
        return payloads

    def _score_queries(
        self, queries: Iterable[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
        """Score every query and return (rows, per_category) summary."""
        rows: list[dict[str, Any]] = []
        per_cat_raw: dict[str, list[float]] = {}

        for q in queries:
            question = str(q.get("question", ""))
            answer = str(q.get("answer", ""))
            category = str(q.get("category", "uncategorised"))
            if category not in _KNOWN_CATEGORIES:
                category = "uncategorised"

            context = self._retrieve_context(question)
            try:
                score = self._judge(question=question, expected=answer, context=context)
            except JudgeFailedError as exc:
                # Unscored — never folded into a category as a 0.0 "wrong".
                rows.append(
                    {
                        "question": question,
                        "answer": answer,
                        "category": category,
                        "score": None,
                        "pass": None,
                        "judge_failure": exc.reason,
                        "judge_error": exc.detail,
                    }
                )
                continue
            passed = score >= _PASS_THRESHOLD

            rows.append(
                {
                    "question": question,
                    "answer": answer,
                    "category": category,
                    "score": score,
                    "pass": passed,
                }
            )
            per_cat_raw.setdefault(category, []).append(score)

        per_cat = {
            cat: {
                "n": float(len(scores)),
                "passed": float(sum(1 for s in scores if s >= _PASS_THRESHOLD)),
                "mean": (sum(scores) / len(scores)) if scores else 0.0,
            }
            for cat, scores in per_cat_raw.items()
        }
        return rows, per_cat

    def _retrieve_context(self, question: str) -> str:
        """Route ``question`` to either the SearchPipeline or the legacy fact_store.

        Plan B-parity D3 remediation: when ``self._search_pipeline`` is
        wired, the eval CLI uses the same retrieval surface as ``kairix
        prep`` — intent classifier + fact federation + fusion + budget.
        When ``None``, falls back to today's direct fact_store.search
        path (preserved as ``--legacy-direct`` for regression debugging).
        """
        if self._search_pipeline is not None:
            result = self._search_pipeline.search(question)
            return _search_result_to_context(result)
        hits = self._fact_store.search(question, top_k=5)
        return _hits_to_context(hits)

    def _judge(self, *, question: str, expected: str, context: str) -> float:
        """LLM-judge prompt - returns a graded 0.0-1.0 score.

        The prompt asks the LLM for a single float on its own line; an
        out-of-range value is clamped to [0, 1].

        Raises:
            JudgeFailedError: the backend raised or returned an empty reply
                (``LLMBackend.chat`` returns ``""`` on failure), or the reply
                holds no finite float. A failure is never scored as 0.0.
        """
        prompt = [
            {
                "role": "system",
                "content": (
                    "You score retrieval answers. Respond with a single float "
                    "between 0.0 and 1.0 on its own line. 1.0 = exact match. "
                    "0.5 = partially correct. 0.0 = wrong or missing."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {question}\nExpected answer: {expected}\n"
                    f"Retrieved context:\n{context}\n\nScore (0.0-1.0):"
                ),
            },
        ]
        return parse_judge_score(call_judge(lambda: self._llm.chat(prompt, max_tokens=8)))

    def _score_extraction(
        self,
        extracted: list[Any],
        ground_truth: tuple[dict[str, Any], ...] | None,
    ) -> tuple[float | None, float | None, float | None]:
        """Compute precision/recall/F1 of extracted facts vs ground truth.

        Returns ``(None, None, None)`` when ``ground_truth is None`` —
        the use case tolerates missing ``ground-truth-facts.json`` so
        suites that only score query pass-rate still run.
        """
        if ground_truth is None:
            return None, None, None

        gt_total = len(ground_truth)
        ext_total = len(extracted)
        if gt_total == 0 and ext_total == 0:
            return 1.0, 1.0, 1.0
        if gt_total == 0:
            return 0.0, 0.0, 0.0

        matched = 0
        for gt in ground_truth:
            if _has_matching_extracted(gt, extracted):
                matched += 1

        precision = (matched / ext_total) if ext_total else 0.0
        recall = matched / gt_total
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        return f1, precision, recall


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _load_json_list(path: Path) -> tuple[dict[str, Any], ...]:
    """Read a JSON array of objects from ``path``.

    Raises:
        ValueError: with actionable markers if the file is not JSON or
            does not contain a list of objects.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"File {path!r} is not valid JSON: {exc}. "
            f"fix: validate with `python -m json.tool {path}`. "
            f"next: see reference-library/conversations/README.md for the schema."
        ) from exc
    if not isinstance(raw, list):
        raise ValueError(
            f"File {path!r} must contain a JSON array, got {type(raw).__name__}. "
            f"fix: wrap the entries in [ ... ]. "
            f"next: see reference-library/conversations/README.md."
        )
    return tuple(item for item in raw if isinstance(item, dict))


def _load_session_metadata(session_path: Path) -> dict[str, Any] | None:
    """Load the sidecar metadata JSON for ``session_path`` if present.

    Resolution order (first hit wins):

    1. ``<session_path>.metadata.json`` — e.g.
       ``session-001.jsonl.metadata.json``. The reference-library
       convention used by Stream A / Spike C1.
    2. ``<session_path.stem>.md.metadata.json`` — e.g.
       ``session-001.md.metadata.json``. The LoCoMo harness convention
       (P6) — accepted here so a corpus that mirrors both shapes
       lights up regardless of which sidecar the operator wrote.

    Returns ``None`` when neither sidecar exists or the file is
    malformed / not a JSON object. Malformed sidecars are logged at
    warning level and the session continues with no metadata — same
    fail-soft posture as :func:`_read_session`.
    """
    candidates = (
        session_path.with_suffix(session_path.suffix + ".metadata.json"),
        session_path.with_suffix(".md.metadata.json"),
    )
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning(
                "eval-suite: skipping malformed sidecar metadata at %s: %s",
                candidate,
                exc,
            )
            continue
        if isinstance(raw, dict):
            return raw
        logger.warning(
            "eval-suite: sidecar metadata at %s is not a JSON object (got %s); ignoring.",
            candidate,
            type(raw).__name__,
        )
    return None


class _CapturingFactStore:
    """Per-call FactStore wrapper that records every ``add`` call.

    Forwards every Protocol method to the wrapped store unchanged
    (``add`` / ``search`` / ``find_conflicts`` / ``supersede``) so
    consolidation passes and downstream lookups see the real backing
    store. The wrapper exists purely so the SuiteRunner can recover
    the list of facts that one ``ingest_corpus`` call emitted —
    independent of the store's namespace / superseded-filter behaviour.

    F1 note: this is NOT monkeypatching the production fact store.
    It's a composition seam — the runner constructs a recorder per
    call, hands the recorder (not the underlying store) to
    ``ingest_corpus``, and the underlying store stays unmodified.
    """

    def __init__(self, inner: FactStore) -> None:
        self._inner = inner
        self.captured: list[Any] = []

    def add(self, fact: Any) -> None:
        """Forward ``fact`` to the wrapped store and capture it locally."""
        self._inner.add(fact)
        self.captured.append(fact)

    def search(self, query: str, *, top_k: int = 10, namespace: str | None = None) -> list[Any]:
        """Forward search to the inner store unchanged."""
        return self._inner.search(query, top_k=top_k, namespace=namespace)

    def find_conflicts(self, *, entity: str, attribute: str, namespace: str | None = None) -> list[Any]:
        """Forward find_conflicts to the inner store unchanged."""
        return self._inner.find_conflicts(entity=entity, attribute=attribute, namespace=namespace)

    def supersede(self, *, old_id: str, new_id: str) -> None:
        """Forward supersede to the inner store unchanged."""
        self._inner.supersede(old_id=old_id, new_id=new_id)


def _read_session(path: Path) -> list[dict[str, Any]]:
    """Parse a ``session-*.jsonl`` file into a list of turn dicts.

    Malformed lines are logged + skipped, mirroring the
    ``ingest_chat`` use case's tolerant parser.
    """
    turns: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("eval-suite: skipping malformed line in %s: %s", path, exc)
                continue
            if isinstance(obj, dict):
                turns.append(obj)
    return turns


def _hits_to_context(hits: list[Any]) -> str:
    """Stringify FactHit objects into a context block for the LLM judge."""
    lines: list[str] = []
    for hit in hits:
        try:
            rec = hit.record
            lines.append(f"- {rec.entity} {rec.attribute} = {rec.value}")
        except AttributeError:
            # Some backends return raw memory shapes; fall back to content.
            content = getattr(hit, "content", None)
            if content:
                lines.append(f"- {content}")
    return "\n".join(lines) if lines else "(no relevant facts retrieved)"


# Maximum chunk-snippet length passed into the LLM-judge context. Fact
# rows are short triplets and arrive whole; chunk rows can be 1-2 kB
# each, which blows the judge prompt past its 8-token answer budget.
_CHUNK_SNIPPET_CHARS: int = 300


def _search_result_to_context(result: Any) -> str:
    """Adapt a SearchPipeline result to the LLM-judge context format.

    Mirrors what :func:`_hits_to_context` produces for FactHits, but
    handles the BudgetedResult wrapper from SearchPipeline (which carries
    both chunk and fact rows after fusion). Fact rows arrive with
    triplet-formatted snippets already (via ``_fused_from_fact_hit``);
    chunk rows arrive with full chunk text — truncate to keep the judge
    context tractable.

    Plan B-parity D3 remediation. Empty results map to the same
    ``(no relevant facts retrieved)`` sentinel that :func:`_hits_to_context`
    emits, so the judge prompt shape is invariant across both retrieval
    paths and the score is comparable.
    """
    lines: list[str] = []
    for budgeted in getattr(result, "results", [])[:5]:
        inner = getattr(budgeted, "result", None)
        if inner is None:
            continue
        snippet = (getattr(budgeted, "content", "") or "").strip()
        if not snippet:
            continue
        is_fact = str(getattr(inner, "path", "")).startswith("facts://")
        if is_fact:
            lines.append(f"- {snippet}")
        else:
            title = getattr(inner, "title", "") or getattr(inner, "path", "")
            lines.append(f"- [{title}] {snippet[:_CHUNK_SNIPPET_CHARS]}")
    return "\n".join(lines) if lines else "(no relevant facts retrieved)"


def _has_matching_extracted(gt_fact: dict[str, Any], extracted: list[Any]) -> bool:
    """Return True iff one of ``extracted`` matches ``gt_fact``.

    Match definition (per the brief): same ``entity`` + same
    ``attribute`` + ground-truth ``value`` is at least a substring of
    the extracted record's value (case-insensitive). The substring
    direction is "extracted value contains GT value" — an extractor
    that emits a longer-than-needed answer still counts as having
    found the ground-truth fact.
    """
    gt_entity = str(gt_fact.get("entity", "")).strip().lower()
    gt_attribute = str(gt_fact.get("attribute", "")).strip().lower()
    gt_value = str(gt_fact.get("value", "")).strip().lower()
    for ext in extracted:
        try:
            if (
                str(ext.entity).strip().lower() == gt_entity
                and str(ext.attribute).strip().lower() == gt_attribute
                and gt_value in str(ext.value).strip().lower()
            ):
                return True
        except AttributeError:
            continue
    return False
