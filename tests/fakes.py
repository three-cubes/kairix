"""
Fake implementations of core domain protocols for testing.

Each fake is:
  - Simple (in-memory data structures)
  - Configurable (accepts test data in constructor)
  - Protocol-compliant (implements all methods from kairix.core.protocols)

These fakes are the canonical test doubles for contract and unit tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kairix.core.search.intent import QueryIntent
from kairix.core.search.intent import classify as _real_classify
from kairix.paths import KairixPaths


def FakePaths(  # noqa: N802 — factory function returning KairixPaths; named like a class for call-site clarity
    *,
    document_root: Path | str = "/fake/document_root",
    db_path: Path | str = "/fake/index.sqlite",
    log_dir: Path | str = "/fake/logs",
    workspace_root: Path | str = "/fake/workspaces",
) -> KairixPaths:
    """Construct a real ``KairixPaths`` from explicit arguments — no env-var I/O.

    The canonical replacement for ``monkeypatch.setenv("KAIRIX_*")`` +
    ``_resolve_cached.cache_clear()``. Tests construct a paths object with
    whatever values they need and pass it through the production code's
    ``paths: KairixPaths`` parameter.

    Returns a ``KairixPaths`` instance (not a separate Fake type) so the
    production type surface stays narrow — there is one paths shape, used
    in both production and tests.

    Defaults are sentinel ``/fake/...`` paths that won't accidentally match
    real filesystem locations; tests should pass concrete ``tmp_path``
    values when path semantics matter for the test.

    Example:
        >>> from pathlib import Path
        >>> from tests.fakes import FakePaths
        >>> paths = FakePaths(
        ...     document_root=tmp_path / "vault",
        ...     workspace_root=tmp_path / "workspaces",
        ... )
        >>> result = should_inject(f"{paths.document_root}/01-Projects/x.md", paths=paths)
    """
    return KairixPaths(
        document_root=Path(document_root),
        db_path=Path(db_path),
        log_dir=Path(log_dir),
        workspace_root=Path(workspace_root),
    )


class FakeClassifier:
    """Fake IntentClassifier that returns a fixed intent.

    Pass ``raises=`` to make ``classify()`` raise — covers never-raises
    contracts in callers that wrap the classifier.
    """

    def __init__(
        self,
        intent: QueryIntent = QueryIntent.SEMANTIC,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self.intent = intent
        self._raises = raises

    def classify(self, query: str) -> QueryIntent:
        if self._raises is not None:
            raise self._raises
        return self.intent


class RealClassifierAdapter:
    """Adapter that wires the real ``classify()`` free function as an
    ``IntentClassifier`` protocol implementation.

    This is not a fake — it delegates to production code. It exists so
    integration tests can construct a ``SearchPipeline`` whose intent
    classification is exactly what production runs, without inline adapters
    in the test file.

    Records every classify call so tests can assert the pipeline reached
    the classifier.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def classify(self, query: str) -> QueryIntent:
        self.calls.append(query)
        return _real_classify(query)


class FakeDocumentRepository:
    """In-memory document store keyed by path.

    Three construction modes (mutually exclusive):

      Default (``documents=...``)
        ``search_fts`` does a substring match of query against title+content
        and returns the stored doc dicts.

      Scripted bm25-shaped (``bm25_rows=...``)
        ``search_fts`` returns the supplied list verbatim, truncated to
        ``limit``, with optional ``collections`` filter honoured. Used by
        integration / BDD tests that want exact control over BM25Result-
        shaped rows fed into the fusion stage.

      Scripted exact (``force_rows=...``)
        ``search_fts`` returns the supplied list verbatim — used when a
        contract test needs exact row shapes (e.g. missing keys) without
        any filtering.

    Pass ``raises=Exception(...)`` to make every call raise (covers
    ``never-raises`` contracts in callers).
    Captures every ``search_fts`` call arg in ``calls`` for assertion.
    """

    def __init__(
        self,
        documents: list[dict[str, Any]] | None = None,
        *,
        raises: BaseException | None = None,
        force_rows: list[dict[str, Any]] | None = None,
        bm25_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._docs: dict[str, dict[str, Any]] = {}
        for doc in documents or []:
            path = doc.get("path", "")
            self._docs[path] = doc
        self._raises = raises
        self._force_rows = force_rows
        self._bm25_rows: list[dict[str, Any]] | None = list(bm25_rows) if bm25_rows is not None else None
        self.calls: list[tuple[str, list[str] | None, int]] = []

    def search_fts(
        self,
        query: str,
        collections: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.calls.append((query, collections, limit))
        if self._raises is not None:
            raise self._raises
        if self._force_rows is not None:
            return list(self._force_rows[:limit])
        if self._bm25_rows is not None:
            # Scripted mode: return the configured rows verbatim, truncated
            # to ``limit``. Optional ``collections`` filter is honoured if
            # the row carries a ``collection`` field.
            rows = self._bm25_rows
            if collections:
                rows = [r for r in rows if r.get("collection") in collections]
            return list(rows[:limit])
        results = []
        query_lower = query.lower()
        for doc in self._docs.values():
            if collections and doc.get("collection") not in collections:
                continue
            content = doc.get("content", "") + " " + doc.get("title", "")
            if query_lower in content.lower():
                # Match BM25Result TypedDict shape — production bm25_search
                # emits ``file``, not ``path``. Without this normalisation
                # downstream RRF swallows KeyError into [] and integration
                # tests "pass" against no-op fusion (#162).
                row = dict(doc)
                if "file" not in row:
                    row["file"] = row.get("path", "")
                if "score" not in row:
                    row["score"] = 1.0
                if "snippet" not in row:
                    row["snippet"] = row.get("content", "")[:300]
                results.append(row)
            if len(results) >= limit:
                break
        return results

    def get_by_path(self, path: str) -> dict[str, Any] | None:
        return self._docs.get(path)

    def get_chunk_dates(self, paths: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in paths:
            doc = self._docs.get(path)
            if doc and "chunk_date" in doc:
                result[path] = doc["chunk_date"]
        return result

    def insert_or_update(
        self,
        path: str,
        collection: str,
        title: str,
        content: str,
        content_hash: str,
    ) -> None:
        self._docs[path] = {
            "path": path,
            "collection": collection,
            "title": title,
            "content": content,
            "content_hash": content_hash,
        }


class FakeGraphRepository:
    """In-memory entity graph keyed by name.

    Configure with ``entities=`` (each must have a ``name`` for indexing).
    Pass ``available=False`` to simulate Neo4j-not-wired.
    Pass ``raises=Exception(...)`` to make ``cypher()`` raise (covers
    the never-raises contract in entity-boost callers).
    Pass ``cypher_rows=`` to supply explicit Neo4j-shaped rows for ``cypher()``
    (the ``entity_boost_neo4j`` helper expects ``{vault_path, name, labels,
    in_degree}`` which the entity-keyed ``_entities`` dict does not carry).
    Tracks ``available_checks``, ``find_entity_calls``, ``cypher_calls``,
    ``entity_in_degrees_calls`` so integration tests can assert which code
    paths reached the graph backend.
    """

    def __init__(
        self,
        entities: list[dict[str, Any]] | None = None,
        available: bool = True,
        *,
        raises: BaseException | None = None,
        cypher_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._available = available
        self._raises = raises
        self._entities: dict[str, dict[str, Any]] = {}
        self._all_entities: list[dict[str, Any]] = list(entities or [])
        for entity in entities or []:
            name = entity.get("name", entity.get("id", ""))
            self._entities[name.lower()] = entity
        self._cypher_rows: list[dict[str, Any]] = list(cypher_rows or [])
        # Call-tracking — tests inspect these to verify routing.
        self.available_checks: int = 0
        self.find_entity_calls: list[str] = []
        self.cypher_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.entity_in_degrees_calls: int = 0

    @property
    def available(self) -> bool:
        self.available_checks += 1
        return self._available

    def find_entity(self, name: str) -> dict[str, Any] | None:
        self.find_entity_calls.append(name)
        return self._entities.get(name.lower())

    def entity_in_degrees(self) -> list[dict[str, Any]]:
        self.entity_in_degrees_calls += 1
        return list(self._all_entities)

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.cypher_calls.append((query, params))
        if self._raises is not None:
            raise self._raises
        if self._cypher_rows:
            return list(self._cypher_rows)
        return list(self._all_entities)


class FakePlannerGraphClient:
    """Fake Neo4j-style client for the QueryPlanner ``neo4j_graph_context`` flow.

    Implements ``available``, ``find_by_name(name) -> list[dict]`` and
    ``related_entities(entity_id, max_hops) -> list[dict]`` — the surface the
    planner uses through duck-typing. Constructor-driven test data; never
    raises unless explicitly configured to.
    """

    def __init__(
        self,
        entities_by_word: dict[str, list[dict[str, Any]]] | None = None,
        related_by_id: dict[str, list[dict[str, Any]]] | None = None,
        available: bool = True,
        find_raises: BaseException | None = None,
        related_raises: BaseException | None = None,
    ) -> None:
        self._entities_by_word: dict[str, list[dict[str, Any]]] = {
            k.lower(): list(v) for k, v in (entities_by_word or {}).items()
        }
        self._related_by_id: dict[str, list[dict[str, Any]]] = dict(related_by_id or {})
        self._available = available
        self._find_raises = find_raises
        self._related_raises = related_raises
        self.find_calls: list[str] = []
        self.related_calls: list[str] = []

    @property
    def available(self) -> bool:
        return self._available

    def find_by_name(self, name: str) -> list[dict[str, Any]]:
        self.find_calls.append(name)
        if self._find_raises is not None:
            raise self._find_raises
        return list(self._entities_by_word.get(name.lower(), []))

    def related_entities(self, entity_id: str, max_hops: int = 1) -> list[dict[str, Any]]:
        self.related_calls.append(entity_id)
        if self._related_raises is not None:
            raise self._related_raises
        return list(self._related_by_id.get(entity_id, []))


class FakeVectorRepository:
    """In-memory vector store that returns configured results.

    Pass ``raises=`` to make ``search()`` raise — covers never-raises
    contracts in vector-backend callers.
    """

    def __init__(
        self,
        results: list[dict[str, Any]] | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self._results: list[dict[str, Any]] = results or []
        self._vectors: list[tuple[str, list[float]]] = []
        self._raises = raises

    def search(
        self,
        query_vec: list[float],
        k: int,
        collections: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if self._raises is not None:
            raise self._raises
        if collections:
            filtered = [r for r in self._results if r.get("collection") in collections]
            return filtered[:k]
        return self._results[:k]

    def add_vectors(self, items: list[tuple[str, list[float]]]) -> int:
        self._vectors.extend(items)
        return len(items)

    def count(self) -> int:
        return len(self._vectors) + len(self._results)


class FakeEmbeddingService:
    """Deterministic embedding service that returns a fixed vector.

    When constructed with ``vector=[]`` (or any empty iterable), every call to
    ``embed`` returns ``[]`` — useful for exercising backend short-circuit
    paths that treat empty embeddings as a soft failure.
    """

    def __init__(self, vector: list[float] | None = None, dim: int = 1536) -> None:
        # Treat an explicitly-passed empty list as "embed always returns []".
        # Default (None) -> a normal fixed dim-vector.
        if vector is None:
            self._vector: list[float] = [0.01] * dim
        else:
            self._vector = list(vector)

    def embed(self, text: str) -> list[float]:
        return list(self._vector)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vector) for _ in texts]


class FakeEmbedProvider:
    """Deterministic EmbedProvider — captures call args for assertion.

    Implements ``kairix.platform.llm.embed_provider.EmbedProvider``:
    ``embed_batch(texts, *, model, dims) -> list[list[float]]``.
    """

    def __init__(self, vector: list[float] | None = None, dim: int = 3) -> None:
        self._vector = vector or [0.0, 0.6, 0.8]
        self._dim = dim
        self.calls: list[dict[str, Any]] = []

    def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
        self.calls.append({"texts": list(texts), "model": model, "dims": dims})
        return [list(self._vector) for _ in texts]


class FakeSummaryLoader:
    """Deterministic ``SummaryLoader`` for the budget enforcer.

    Implements ``kairix.core.search.budget.SummaryLoader``:
    ``get_l0(path)`` and ``get_l1(path)``.

    Configure with ``l0_by_path`` / ``l1_by_path`` dicts. Unset paths return
    ``None``. Pass ``raises=Exception(...)`` to make every call raise.
    """

    def __init__(
        self,
        *,
        l0_by_path: dict[str, str] | None = None,
        l1_by_path: dict[str, str] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._l0 = dict(l0_by_path or {})
        self._l1 = dict(l1_by_path or {})
        self._raises = raises
        self.l0_calls: list[str] = []
        self.l1_calls: list[str] = []

    def get_l0(self, path: str) -> str | None:
        self.l0_calls.append(path)
        if self._raises is not None:
            raise self._raises
        return self._l0.get(path)

    def get_l1(self, path: str) -> str | None:
        self.l1_calls.append(path)
        if self._raises is not None:
            raise self._raises
        return self._l1.get(path)


class FakeLLMBackend:
    """Deterministic ``LLMBackend`` for tests.

    Implements ``kairix.platform.llm.protocol.LLMBackend``: ``chat(messages, max_tokens)``
    returns a configured response (or successive responses), and ``embed(text)`` returns
    a configured vector. Captures call args.
    """

    def __init__(
        self,
        *,
        chat_responses: list[str] | None = None,
        chat_response: str | None = None,
        embed_vector: list[float] | None = None,
        chat_raises: BaseException | None = None,
    ) -> None:
        # Single-response shortcut: chat_response="..." reuses the value for every call.
        if chat_response is not None:
            chat_responses = [chat_response]
        self._chat_responses = list(chat_responses or [])
        self._chat_call_idx = 0
        self._embed_vector = list(embed_vector or [0.0, 0.6, 0.8])
        self._chat_raises = chat_raises
        self.chat_calls: list[dict[str, Any]] = []
        self.embed_calls: list[str] = []

    def chat(self, messages: list[dict[str, Any]], max_tokens: int = 800) -> str:
        self.chat_calls.append({"messages": list(messages), "max_tokens": max_tokens})
        if self._chat_raises is not None:
            raise self._chat_raises
        if not self._chat_responses:
            return ""
        # If we have multiple responses, advance through them; if only one, reuse it.
        if len(self._chat_responses) == 1:
            return self._chat_responses[0]
        idx = min(self._chat_call_idx, len(self._chat_responses) - 1)
        self._chat_call_idx += 1
        return self._chat_responses[idx]

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return list(self._embed_vector)


class FakeContentClassifier:
    """Two-step ``ContentClassifier`` for the benchmark runner.

    Implements ``kairix.quality.benchmark.runner.ContentClassifier``:
    ``classify_rules(query, agent)`` and ``classify_with_llm(query, agent)``.

    Configure via ``rules_type`` (returned for every rules call) and
    ``llm_type`` (returned for every LLM-fallback call). Captures call args.
    """

    def __init__(
        self,
        *,
        rules_type: str = "unknown",
        llm_type: str = "unknown",
        rules_raises: BaseException | None = None,
    ) -> None:
        self._rules_type = rules_type
        self._llm_type = llm_type
        self._rules_raises = rules_raises
        self.rules_calls: list[dict[str, str]] = []
        self.llm_calls: list[dict[str, str]] = []

    def classify_rules(self, query: str, agent: str) -> Any:
        self.rules_calls.append({"query": query, "agent": agent})
        if self._rules_raises is not None:
            raise self._rules_raises
        from types import SimpleNamespace

        return SimpleNamespace(type=self._rules_type)

    def classify_with_llm(self, query: str, agent: str) -> Any:
        self.llm_calls.append({"query": query, "agent": agent})
        from types import SimpleNamespace

        return SimpleNamespace(type=self._llm_type)


class FakeVectorSearcher:
    """Deterministic VectorSearcher for ``RecallChecker``.

    Implements ``kairix.core.embed.recall_check.VectorSearcher``:
    ``search_vectors(vector, *, limit) -> list[str]``.

    Returns the configured paths for any input vector. Captures the
    ``(vector, limit)`` of every call so tests can assert what the recall
    gate fed into the index — including the actual numpy vector, so
    tests can verify normalisation via ``np.linalg.norm(call["vector"])``.
    """

    def __init__(self, paths: list[str] | None = None) -> None:
        self._paths = list(paths or [])
        self.calls: list[dict[str, Any]] = []

    def search_vectors(self, vector: Any, *, limit: int) -> list[str]:
        self.calls.append({"vector": vector, "limit": limit})
        return list(self._paths[:limit])


class FakeFusion:
    """Pass-through fusion: concatenates BM25 and vector results.

    Pass ``raises=`` to make ``fuse()`` raise — covers never-raises
    contracts in pipeline callers.
    """

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self._raises = raises

    def fuse(self, bm25: list[Any], vec: list[Any]) -> list[Any]:
        if self._raises is not None:
            raise self._raises
        return bm25 + vec


class FakeBoost:
    """No-op boost: returns results unmodified.

    Pass ``raises=`` to make ``boost()`` raise — covers never-raises
    contracts in pipeline callers.
    """

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self._raises = raises

    def boost(self, results: list[Any], query: str, context: dict[str, Any]) -> list[Any]:
        if self._raises is not None:
            raise self._raises
        return results


class CapturingBoost:
    """BoostStrategy that records the (query, intent) of every boost call.

    Useful for verifying that ``SearchPipeline`` propagates the classifier's
    intent into the boost context. Always returns ``results`` unchanged so it
    composes cleanly with other boosts in a chain.
    """

    def __init__(self) -> None:
        self.captured: list[tuple[str, QueryIntent | None]] = []

    def boost(self, results: list[Any], query: str, context: dict[str, Any]) -> list[Any]:
        self.captured.append((query, context.get("intent")))
        return results


class IntentGatedBoost:
    """BoostStrategy wrapper that delegates only when ``context['intent']``
    matches the configured intent.

    This is the canonical adapter used by intent-routing integration tests:
    wrap any production boost (e.g. ``TemporalDateBoost``, ``ProceduralBoost``,
    ``EntityBoost``) so it fires only for its target intent. Production wires
    boosts ungated and relies on internal heuristics; this wrapper makes the
    intent dispatch explicit so tests can assert routing.

    Tracks ``invocations`` and ``skipped`` counts so tests can verify whether
    the inner boost actually ran.
    """

    def __init__(self, inner: Any, intent: QueryIntent) -> None:
        self._inner = inner
        self._intent = intent
        self.invocations: int = 0
        self.skipped: int = 0

    def boost(self, results: list[Any], query: str, context: dict[str, Any]) -> list[Any]:
        if context.get("intent") == self._intent:
            self.invocations += 1
            inner_result: list[Any] = self._inner.boost(results, query, context)
            return inner_result
        self.skipped += 1
        return results


class FakeScorer:
    """Fixed-score scorer for testing."""

    def __init__(self, score: float = 1.0) -> None:
        self._score = score

    def score(self, retrieved: list[str], gold: list[dict[str, Any]]) -> float:
        return self._score


class FakeSearchLogger:
    """In-memory search logger that captures events.

    Pass ``raises=`` to make every log call raise — covers never-raises
    contracts in pipeline callers that wrap the logger.
    """

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self._raises = raises

    def log_search(self, event: dict[str, Any]) -> None:
        if self._raises is not None:
            raise self._raises
        self.events.append(event)

    def log_query(self, event: dict[str, Any]) -> None:
        if self._raises is not None:
            raise self._raises
        self.events.append(event)


class FakeCollectionResolver:
    """In-memory CollectionResolver that returns configured lists per (agent, scope) key.

    Constructed with a mapping from (agent_or_None, scope_value) tuples to
    collection lists. Anything not in the map returns None.
    """

    def __init__(self, by_key: dict[tuple[str | None, str], list[str] | None] | None = None) -> None:
        self._by_key: dict[tuple[str | None, str], list[str] | None] = dict(by_key or {})

    def resolve(self, agent: str | None, scope: Any) -> list[str] | None:
        scope_value = scope.value if hasattr(scope, "value") else str(scope)
        return self._by_key.get((agent, scope_value))


class FakeAgentRegistry:
    """In-memory AgentRegistry constructed from a list of agent dicts.

    Each entry is a dict with at least ``name`` and ``collection``; optional
    ``write_path`` and ``read_only`` mirror AgentDef in the production
    Adapter. Tests use this rather than ConfigDrivenAgentRegistry so they
    don't have to construct the full YAML pipeline.
    """

    def __init__(self, agents: list[dict[str, Any]] | None = None) -> None:
        self._agents = list(agents or [])

    def list_agents(self) -> list[Any]:
        # Returns dict-like entries; resolver only needs .collection attribute,
        # so wrap each in a minimal namespace-style object.
        class _Agent:
            def __init__(self, d: dict[str, Any]) -> None:
                self.name = d["name"]
                self.collection = d.get("collection", f"{d['name']}-memory")
                self.write_path = d.get("write_path", "")
                self.read_only = d.get("read_only", False)

        return [_Agent(a) for a in self._agents]

    def collection_for(self, name: str) -> str:
        for a in self._agents:
            if a["name"] == name:
                return str(a.get("collection", f"{name}-memory"))
        raise KeyError(f"unknown agent {name!r}")

    def validate_write(self, agent_name: str, path: str) -> bool:
        for a in self._agents:
            if a["name"] == agent_name and not a.get("read_only", False):
                wp = a.get("write_path", "")
                if not wp:
                    return False
                return path == wp or path.startswith(wp.rstrip("/") + "/")
        return False


# ---------------------------------------------------------------------------
# Eval-module fakes (#143 Phase 1)
#
# Paired with the eval protocols in kairix/core/protocols.py — together they
# replace the *_fn=None test-substitution kwargs scattered through the eval
# module. Tests inject these via the constructor of the LLMJudge / GoldBuilder /
# QueryGenerator / SuiteGenerator classes that Phase 2a/2b add.
# ---------------------------------------------------------------------------


class FakeChatBackend:
    """Configurable ChatBackend that returns canned responses or raises a configured error.

    Usage:
        backend = FakeChatBackend(responses=['{"A": 2, "B": 1}'])
        ...
        backend = FakeChatBackend(raise_on_call=ValueError("No API credentials"))

    `responses` is consumed in order; once exhausted, subsequent calls raise
    `IndexError` (a deliberate explicit failure rather than silently looping
    or returning empty — silent fallback is the smell this protocol replaces).
    """

    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        raise_on_call: Exception | None = None,
    ) -> None:
        self._responses: list[str] = list(responses or [])
        self._raise_on_call = raise_on_call
        self.calls: list[dict[str, Any]] = []  # for test inspection

    def complete(
        self,
        prompt: str,
        *,
        api_key: str,
        endpoint: str,
        deployment: str,
        system: str | None = None,
        temperature: float = 0.0,
        timeout_s: float = 30.0,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "api_key": api_key,
                "endpoint": endpoint,
                "deployment": deployment,
                "system": system,
                "temperature": temperature,
                "timeout_s": timeout_s,
            }
        )
        if self._raise_on_call is not None:
            raise self._raise_on_call
        if not self._responses:
            raise IndexError(
                f"FakeChatBackend: ran out of canned responses on call {len(self.calls)} (prompt[:60]={prompt[:60]!r})"
            )
        return self._responses.pop(0)


class FakeLLMJudge:
    """Configurable LLMJudge returning fixed grades per query.

    Usage:
        judge = FakeLLMJudge(
            grades_by_query={"deploy docker": {"docker-guide": 2, "ci-cd": 1}},
            calibration_passed=True,
        )

    `grade()` returns a JudgeResult-shaped object using the configured grades
    for the given query, defaulting to all-zero for unknown queries. The fake
    returns a `_StubJudgeResult` (a small namespace) rather than importing
    the real `JudgeResult` class to keep the fake import-free of judge.py
    internals — judge.py's tests can construct real JudgeResults explicitly.
    """

    def __init__(
        self,
        *,
        grades_by_query: dict[str, dict[str, int]] | None = None,
        calibration_passed: bool = True,
    ) -> None:
        self._grades_by_query = dict(grades_by_query or {})
        self._calibration_passed = calibration_passed
        self.grade_calls: list[tuple[str, list[tuple[str, str]]]] = []
        self.calibrate_calls: int = 0

    def grade(
        self,
        query: str,
        candidates: list[tuple[str, str]],
        *,
        runs: int = 1,
    ) -> Any:
        self.grade_calls.append((query, candidates))
        configured = self._grades_by_query.get(query, {})
        # Build a minimal namespace mimicking JudgeResult — judge_model / shuffle_order
        # default to deterministic test values.
        from types import SimpleNamespace

        return SimpleNamespace(
            query=query,
            grades={stem: configured.get(stem, 0) for stem, _ in candidates},
            shuffle_order=tuple(stem for stem, _ in candidates),
            judge_model="fake-llm",
            calibration_passed=self._calibration_passed,
        )

    def calibrate(self) -> bool:
        self.calibrate_calls += 1
        return self._calibration_passed


class FakeQueryGenerator:
    """Configurable QueryGenerator returning fixed queries per (title, body) call.

    Usage:
        gen = FakeQueryGenerator(
            queries_by_title={"deploy.md": [GeneratedQuery(...)]},
        )
    """

    def __init__(self, *, queries_by_title: dict[str, list[Any]] | None = None) -> None:
        self._queries_by_title = dict(queries_by_title or {})
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        title: str,
        body: str,
        *,
        n: int,
        categories: list[str],
    ) -> list[Any]:
        self.calls.append({"title": title, "body": body[:50], "n": n, "categories": list(categories)})
        return list(self._queries_by_title.get(title, []))[:n]


class FakeRetriever:
    """Configurable Retriever returning fixed results per query.

    Usage:
        retriever = FakeRetriever(
            results_by_query={"deploy docker": _build_retrieval_result([...])},
        )

    Default empty result is a SimpleNamespace with `results=[]` and
    `vec_failed=False` — callers that need richer surface should construct
    a typed RetrievalResult and pass it in via `results_by_query`.
    """

    def __init__(self, *, results_by_query: dict[str, Any] | None = None) -> None:
        self._results_by_query = dict(results_by_query or {})
        self.calls: list[dict[str, Any]] = []

    def retrieve(
        self,
        query: str,
        *,
        collections: list[str] | None = None,
        cfg: Any = None,
    ) -> Any:
        self.calls.append({"query": query, "collections": collections, "cfg": cfg})
        if query in self._results_by_query:
            return self._results_by_query[query]
        from types import SimpleNamespace

        return SimpleNamespace(results=[], vec_failed=False)


# ---------------------------------------------------------------------------
# Summaries DB builder (for kairix.core.search.budget integration tests)
# ---------------------------------------------------------------------------
#
# The budget step (kairix.core.search.budget) loads L0/L1 summaries via
# kairix.knowledge.summaries.loader.{get_l0,get_l1}, both of which read from a
# real sqlite3.Connection containing a ``summaries(path TEXT, l0 TEXT, l1
# TEXT)`` table. There is no SummaryLoader Protocol — the contract surface is
# "a connection to a sqlite database with this schema". The helper below
# builds that real DB from a Python mapping so tests don't have to
# monkeypatch loader functions.
#
# The returned path can be plugged into the production code path by setting
# the KAIRIX_SUMMARIES_DB environment variable (see kairix.paths).


def build_summaries_db(
    target: Any,
    rows: dict[str, dict[str, str | None]] | None = None,
) -> Any:
    """Create a real sqlite summaries DB at ``target`` and populate it.

    Args:
        target: A ``pathlib.Path`` (or anything ``str()``-able to a filesystem
            path) where the DB file should be written.
        rows: Mapping from document path to a dict with optional ``l0``/``l1``
            keys. Missing/None values are stored as NULL.

    Returns:
        The same ``target`` object, after the DB has been created and
        populated. The caller is responsible for cleanup (use ``tmp_path``).
    """
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(target))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS summaries (path TEXT PRIMARY KEY, l0 TEXT, l1 TEXT)")
        for path, payload in (rows or {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO summaries(path, l0, l1) VALUES (?, ?, ?)",
                (path, payload.get("l0"), payload.get("l1")),
            )
        conn.commit()
    finally:
        conn.close()
    return target
