"""
Fake implementations of core domain protocols for testing.

Each fake is:
  - Simple (in-memory data structures)
  - Configurable (accepts test data in constructor)
  - Protocol-compliant (implements all methods from kairix.core.protocols)

These fakes are the canonical test doubles for contract and unit tests.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairix.core.search.intent import QueryIntent
from kairix.core.search.intent import classify as _real_classify
from kairix.paths import KairixPaths

if TYPE_CHECKING:
    from kairix.quality.benchmark.corpus import FetchedCorpus


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

    Issue #456 — pass ``confidence=`` to drive the confidence value that
    ``classify_with_confidence()`` returns. Default 1.0 preserves the
    pre-#456 contract where ambient confidence wasn't surfaced.
    """

    def __init__(
        self,
        intent: QueryIntent = QueryIntent.SEMANTIC,
        *,
        raises: BaseException | None = None,
        confidence: float = 1.0,
    ) -> None:
        self.intent = intent
        self._raises = raises
        self._confidence = confidence

    def classify(self, query: str) -> QueryIntent:
        if self._raises is not None:
            raise self._raises
        return self.intent

    def classify_with_confidence(self, query: str):
        """Return an IntentDecision matching this fake's configured (intent,
        confidence). Used by SearchPipeline._classify_with_confidence when
        the classifier supports the post-#456 surface."""
        from kairix.core.search.intent import IntentDecision

        if self._raises is not None:
            raise self._raises
        return IntentDecision(primary=self.intent, confidence=self._confidence, alternatives=())


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

    Pass ``backend_unavailable=True`` to model the production
    :class:`~kairix.core.db.repository.SQLiteDocumentRepository` when its
    SQLite backend cannot be opened / queried: like production it never
    raises — ``search_fts`` / ``list_chunk_seqs`` return ``[]``,
    ``get_by_path`` returns ``None``, ``get_chunk_dates`` returns ``{}``
    and ``insert_or_update`` drops the write (production logs a WARNING).
    Captures every ``search_fts`` call arg in ``calls`` for assertion.
    """

    def __init__(
        self,
        documents: list[dict[str, Any]] | None = None,
        *,
        raises: BaseException | None = None,
        force_rows: list[dict[str, Any]] | None = None,
        bm25_rows: list[dict[str, Any]] | None = None,
        backend_unavailable: bool = False,
    ) -> None:
        self._docs: dict[str, dict[str, Any]] = {}
        self._backend_unavailable = backend_unavailable
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
        if self._backend_unavailable:
            return []
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
                # MM-3 — per-page citation. Default to ``None`` so the
                # fake satisfies the BM25Result TypedDict shape (paged
                # documents are a connector-framework concern; the
                # in-memory doc-repo fake is content-keyed).
                if "source_page" not in row:
                    row["source_page"] = None
                # PLA-274 — canonical breadcrumb. Default to "" (SourceRef.of
                # falls that back to the path downstream) so the fake row
                # carries every BM25Result key the contract requires.
                if "source_uri" not in row:
                    row["source_uri"] = ""
                results.append(row)
            if len(results) >= limit:
                break
        return results

    def get_by_path(self, path: str) -> dict[str, Any] | None:
        if self._backend_unavailable:
            return None
        return self._docs.get(path)

    def list_chunk_seqs(self, source_uri: str) -> list[int]:
        """Sorted 0-based seqs of the ``<source_uri>#<seq>`` chunk rows held.

        Mirrors the real repo's by-prefix lookup (PLA-297) over the in-memory
        docs: only paths whose trailing ``#<tail>`` is all-digits count, so
        heading-anchor fragments are ignored. ``[]`` when the source_uri has
        no finer chunk rows (the doc-level-only class).
        """
        if self._backend_unavailable:
            return []
        prefix = f"{source_uri}#"
        seqs: list[int] = []
        for path in self._docs:
            if not path.startswith(prefix):
                continue
            tail = path[len(prefix) :]
            if tail.isdigit():
                seqs.append(int(tail))
        return sorted(seqs)

    def get_chunk_dates(self, paths: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        if self._backend_unavailable:
            return result
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
        if self._backend_unavailable:
            return
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
    Pass ``swallow_errors=True`` alongside ``raises=`` to mirror the REAL
    :class:`kairix.knowledge.graph.repository.Neo4jGraphRepository` contract:
    ``Neo4jClient.cypher`` logs a backend error and returns ``[]`` rather than
    raising (the default ``raises=`` shape is a caller-robustness injection
    with no production analogue).
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
        swallow_errors: bool = False,
    ) -> None:
        self._available = available
        self._raises = raises
        self._swallow_errors = swallow_errors
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
            if self._swallow_errors:
                return []
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
        # max_hops is part of the protocol surface but the fake returns its
        # configured related-entities list verbatim; tests that need hop-aware
        # behaviour configure the fake's data accordingly.
        del max_hops
        self.related_calls.append(entity_id)
        if self._related_raises is not None:
            raise self._related_raises
        return list(self._related_by_id.get(entity_id, []))


class FakeVectorRepository:
    """In-memory vector store that returns configured results.

    Pass ``raises=`` to make ``search()`` raise — covers never-raises
    contracts in vector-backend callers. ``add_raises=`` / ``count_raises=``
    make ``add_vectors()`` / ``count()`` raise (a rejected batch / an
    unreachable index handle).
    """

    def __init__(
        self,
        results: list[dict[str, Any]] | None = None,
        *,
        raises: BaseException | None = None,
        add_raises: BaseException | None = None,
        count_raises: BaseException | None = None,
    ) -> None:
        self._results: list[dict[str, Any]] = results or []
        self._vectors: list[tuple[str, list[float]]] = []
        self._raises = raises
        self._add_raises = add_raises
        self._count_raises = count_raises

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
        if self._add_raises is not None:
            raise self._add_raises
        self._vectors.extend(items)
        return len(items)

    def count(self) -> int:
        if self._count_raises is not None:
            raise self._count_raises
        return len(self._vectors) + len(self._results)


class FakeEmbeddingService:
    """Deterministic embedding service that returns a fixed vector.

    When constructed with ``vector=[]`` (or any empty iterable), every call to
    ``embed`` returns ``[]`` — useful for exercising backend short-circuit
    paths that treat empty embeddings as a soft failure.

    Pass ``raises=`` to make ``embed`` / ``embed_batch`` raise — covers
    never-raises contracts in callers that wrap the embedder (e.g. the
    fact-store fused recall path degrading to BM25-only).
    """

    def __init__(
        self,
        vector: list[float] | None = None,
        dim: int = 1536,
        *,
        raises: BaseException | None = None,
    ) -> None:
        # Treat an explicitly-passed empty list as "embed always returns []".
        # Default (None) -> a normal fixed dim-vector.
        if vector is None:
            self._vector: list[float] = [0.01] * dim
        else:
            self._vector = list(vector)
        self._raises = raises

    def embed(self, text: str) -> list[float]:
        if self._raises is not None:
            raise self._raises
        return list(self._vector)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self._raises is not None:
            raise self._raises
        return [list(self._vector) for _ in texts]


class FakeEmbedProvider:
    """Deterministic EmbedProvider — captures call args for assertion.

    Implements ``kairix.platform.llm.embed_provider.EmbedProvider``:
    ``embed_batch(texts, *, model, dims) -> list[list[float]]``.

    Pass ``empty=True`` to return an empty result for every call — exercises
    the 'no vectors → skip' branch in callers (e.g. RecallChecker).
    Pass ``raises=Exception(...)`` to make every call raise — mirrors the
    openai SDK surfacing a connection / auth failure from the real providers.
    """

    def __init__(
        self,
        vector: list[float] | None = None,
        dim: int = 3,
        *,
        empty: bool = False,
        raises: BaseException | None = None,
    ) -> None:
        self._vector = vector or [0.0, 0.6, 0.8]
        self._dim = dim
        self._empty = empty
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
        self.calls.append({"texts": list(texts), "model": model, "dims": dims})
        if self._raises is not None:
            raise self._raises
        if self._empty:
            return []
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
    a configured vector. Captures call args. Pass ``embed_empty=True`` to make
    ``embed`` return ``[]`` — the Protocol's documented failure shape.
    """

    def __init__(
        self,
        *,
        chat_responses: list[str] | None = None,
        chat_response: str | None = None,
        embed_vector: list[float] | None = None,
        chat_raises: BaseException | None = None,
        embed_empty: bool = False,
    ) -> None:
        # Single-response shortcut: chat_response="..." reuses the value for every call.
        if chat_response is not None:
            chat_responses = [chat_response]
        self._chat_responses = list(chat_responses or [])
        self._chat_call_idx = 0
        self._embed_vector = list(embed_vector or [0.0, 0.6, 0.8])
        self._chat_raises = chat_raises
        self._embed_empty = embed_empty
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
        if self._embed_empty:
            return []
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


def FakeCredentials(  # noqa: N802 — factory function returning real Credentials; named like a class for call-site clarity
    *,
    api_key: str = "fake-api-key",
    endpoint: str = "https://fake.openai.azure.com",
    model: str = "text-embedding-3-large",
    dims: int = 1536,
) -> Any:
    """Construct a real ``kairix.credentials.Credentials`` from explicit args.

    The canonical replacement for "monkey-patch ``get_credentials``" in tests.
    Tests inject ``creds_resolver=lambda: FakeCredentials(...)`` (or
    ``lambda: None``, or ``lambda: FakeCredentials(api_key="")`` to drive
    the missing-credentials skip path) into ``RecallChecker`` rather than
    mutating module-level state.

    Returns a real ``Credentials`` instance — the production code's
    ``isinstance(creds, Credentials)`` check passes, so tests exercise the
    same control flow production runs.
    """
    from kairix.credentials import Credentials

    return Credentials(api_key=api_key, endpoint=endpoint, model=model, dims=dims)


class FakeFusion:
    """Pass-through fusion: concatenates BM25 and vector results.

    Pass ``raises=`` to make ``fuse()`` raise — covers never-raises
    contracts in pipeline callers.
    """

    def __init__(self, *, raises: BaseException | None = None, swallow_errors: bool = False) -> None:
        self._raises = raises
        # ``swallow_errors=True`` mirrors the shipped RRFFusion /
        # BM25PrimaryFusion, which log an internal fusion error and return
        # ``[]`` (never raise).
        self._swallow_errors = swallow_errors

    def fuse(self, bm25: list[Any], vec: list[Any]) -> list[Any]:
        if self._raises is not None:
            if self._swallow_errors:
                return []
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
    contracts in pipeline callers that wrap the logger. Add
    ``contain_errors=True`` to model the shipped
    :class:`kairix.core.search.logger.JsonlSearchLogger` instead: the sink
    failure is swallowed and nothing is recorded.
    """

    def __init__(self, *, raises: BaseException | None = None, contain_errors: bool = False) -> None:
        self.events: list[dict[str, Any]] = []
        self._raises = raises
        self._contain_errors = contain_errors

    def _record(self, event: dict[str, Any]) -> None:
        if self._raises is not None:
            if self._contain_errors:
                return
            raise self._raises
        self.events.append(event)

    def log_search(self, event: dict[str, Any]) -> None:
        self._record(event)

    def log_query(self, event: dict[str, Any]) -> None:
        self._record(event)


class FakeCollectionResolver:
    """In-memory CollectionResolver that returns configured lists per (agent, scope) key.

    Constructed with a mapping from (agent_or_None, scope_value) tuples to
    collection lists. Anything not in the map returns None.
    """

    def __init__(
        self,
        by_key: dict[tuple[str | None, str], list[str] | None] | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self._by_key: dict[tuple[str | None, str], list[str] | None] = dict(by_key or {})
        # F68 knob — mirrors the production TopologyCollectionResolver,
        # which propagates a failing scope-profile lookup rather than
        # silently widening scope to "no filter".
        self._raises = raises

    def resolve(self, agent: str | None, scope: Any) -> list[str] | None:
        if self._raises is not None:
            raise self._raises
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


class FakeClaimExtractor:
    """Sentence-splitting :class:`kairix.knowledge.contradict.protocols.ClaimExtractor`.

    Shares the shipped ``EntityDensityClaimExtractor``'s observable
    contract without its ranking heuristic: blank content → ``[]``; any
    other text → at most ``top_n`` sentence strings (in input order);
    input that is not decoded text (e.g. raw ``bytes``) raises
    ``TypeError`` from the ``str`` sentence-split pattern rather than
    being masked as "no claims".
    """

    _SENTENCE_SPLIT = r"(?<=[.!?])\s+(?=[A-Z])"

    def extract(self, content: str, *, top_n: int = 3) -> list[str]:
        import re

        if not content or not content.strip():
            return []
        sentences = [s.strip() for s in re.split(self._SENTENCE_SPLIT, content.strip()) if s.strip()]
        return sentences[:top_n]


class FakeContradictionScorer:
    """Scripted :class:`kairix.knowledge.contradict.protocols.ContradictionScorer`.

    Returns the configured ``(score, reason)`` for every pair — default
    ``(0.0, "")``, the Protocol's "no contradiction / unparseable" signal.
    ``raises=`` models a programming bug inside a scorer; like the
    shipped scorers (whose no-raise contract covers LLM parse failure
    only), the fake lets it propagate.
    """

    def __init__(
        self,
        *,
        category: str = "direct",
        score: float = 0.0,
        reason: str = "",
        raises: BaseException | None = None,
    ) -> None:
        self.category = category
        self._score = score
        self._reason = reason
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    def score(self, claim: str, candidate: str) -> tuple[float, str]:
        self.calls.append((claim, candidate))
        if self._raises is not None:
            raise self._raises
        return self._score, self._reason


class FakeConfidenceParser:
    """Scripted :class:`kairix.agents.research.protocols.ConfidenceParser`.

    Returns the scripted confidence (clamped to ``[0.0, 1.0]``, like the
    shipped parsers) for a known response; any other response raises
    :class:`~kairix.agents.research.protocols.ConfidenceParseError` —
    never a silent ``0.0`` (the bug the Protocol exists to prevent).
    """

    def __init__(self, scripted: dict[str, float] | None = None) -> None:
        self._scripted: dict[str, float] = dict(scripted or {})
        self.calls: list[str] = []

    def parse(self, response: str) -> float:
        from kairix.agents.research.protocols import ConfidenceParseError

        self.calls.append(response)
        if response not in self._scripted:
            raise ConfidenceParseError(f"fake: no scripted confidence for response[:60]={response[:60]!r}")
        return max(0.0, min(1.0, float(self._scripted[response])))


class FakeDoctor:
    """In-memory stand-in for the agent doctor surface
    (:func:`kairix.agents.onboarding.doctor.doctor_check_all` /
    :func:`~kairix.agents.onboarding.doctor.doctor_check_agent`).

    Reads the same ``agents:`` config block as production but never
    touches the filesystem: every configured surface reports
    ``exists=True`` with ``file_count`` files (constructor knob). Returns
    the production frozen dataclasses (:class:`SurfaceHealth`,
    :class:`AgentHealth`, :class:`DoctorReport`) so callers see the same
    shapes. Like production it never raises: an unknown agent yields an
    ``overall="error"`` :class:`AgentHealth` carrying an actionable issue.
    """

    def __init__(self, *, file_count: int = 1) -> None:
        self._file_count = file_count

    def check_all(self, *, config: dict[str, object] | None = None) -> Any:
        from kairix.agents.onboarding.doctor import DoctorReport

        agents_raw = (config or {}).get("agents") or {}
        names = sorted(agents_raw) if isinstance(agents_raw, dict) else []
        healths = tuple(self.check_agent(name, config=config) for name in names)
        overall = "error" if any(h.overall == "error" for h in healths) else "ok"
        summary = f"{len(healths)} agent(s) checked — overall {overall}" if healths else "no agents configured"
        return DoctorReport(agents=healths, overall=overall, summary_text=summary)

    def check_agent(self, agent_name: str, *, config: dict[str, object] | None = None) -> Any:
        from pathlib import Path as _Path

        from kairix.agents.onboarding.doctor import AgentHealth, SurfaceHealth

        agents_raw = (config or {}).get("agents") or {}
        entry = agents_raw.get(agent_name) if isinstance(agents_raw, dict) else None
        if not isinstance(entry, dict):
            return AgentHealth(
                name=agent_name,
                harness="",
                surfaces=(),
                overall="error",
                issues=(f"no config for {agent_name} — run: kairix onboard agent {agent_name}",),
            )
        surfaces = tuple(
            SurfaceHealth(
                path=_Path(str(raw["path"])),
                label=str(raw.get("label", "")),
                exists=True,
                file_count=self._file_count,
                most_recent_mtime=None,
                issues=(),
            )
            for raw in entry.get("surfaces", [])
        )
        return AgentHealth(
            name=agent_name,
            harness=str(entry.get("harness", "")),
            surfaces=surfaces,
            overall="ok" if surfaces else "error",
            issues=(),
        )


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
        swallow_errors: bool = False,
    ) -> None:
        self._responses: list[str] = list(responses or [])
        self._raise_on_call = raise_on_call
        # ``swallow_errors=True`` mirrors the production eval adapter
        # (:class:`kairix.quality.eval.chat_backend.ProviderEvalChatBackend`),
        # which logs a provider failure and returns ``""`` rather than
        # raising. Default ``False`` keeps the raising shape (the
        # unconfigured-provider ``ValueError`` path) for callers' tests.
        self._swallow_errors = swallow_errors
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
            if self._swallow_errors:
                return ""
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
        raise_on_failed_calibration: bool = False,
    ) -> None:
        self._grades_by_query = dict(grades_by_query or {})
        self._calibration_passed = calibration_passed
        # The REAL ``kairix.quality.eval.judge.LLMJudge.calibrate`` never
        # returns False — a failed calibration raises JudgeCalibrationError.
        # Opt in to that faithful shape; the default keeps the legacy
        # bool-return used by existing callers.
        self._raise_on_failed_calibration = raise_on_failed_calibration
        self.grade_calls: list[tuple[str, list[tuple[str, str]]]] = []
        self.calibrate_calls: int = 0

    def grade(
        self,
        query: str,
        candidates: list[tuple[str, str]],
        *,
        runs: int = 1,
    ) -> Any:
        # ``runs`` is part of the LLMJudge protocol; the fake returns the
        # configured grades regardless of run count (deterministic by design).
        del runs
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
        if not self._calibration_passed and self._raise_on_failed_calibration:
            from kairix.quality.eval.judge import JudgeCalibrationError

            raise JudgeCalibrationError("FakeLLMJudge: calibration failed (configured)")
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

    Default empty result is the production shape — a
    ``RetrievalResult(paths=[], vec_failed=False)`` — so it feeds every
    Retriever consumer (``evaluate_single_config`` reads ``.paths`` /
    ``.vec_failed``). Callers that need richer surface construct a typed
    RetrievalResult and pass it in via `results_by_query`.
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
        from kairix.quality.eval.retrieval import RetrievalResult

        return RetrievalResult(paths=[], vec_failed=False)


class FakeProvider:
    """Configurable ``Provider`` for transport / provider-registry tests.

    Implements ``kairix.providers.Provider``: ``embed_batch``, ``chat``,
    ``dimension``, ``healthcheck``, plus the ``name`` attribute. Counts
    every call so transport tests can assert pool / coalescer / cache
    semantics (e.g. "embed_batch called once for N coalesced texts").

    Configuration:
      ``name`` — provider name attribute (default ``"fake"``).
      ``vector`` — fixed embedding vector returned per text
        (default ``[0.0] * dim``).
      ``dim`` — embedding dimension reported by ``dimension()``
        (default ``3``).
      ``chat_reply`` — fixed string returned from ``chat`` (default ``""``).
      ``health`` — ``ProviderHealth`` returned by ``healthcheck``
        (default: ok=True, endpoint=``"fake://provider"``).
      ``embed_empty`` — when True, ``embed_batch`` returns ``[]`` per text
        so callers can exercise the soft-failure branch.
      ``embed_delay_s`` — per-call delay (seconds) applied inside
        ``embed_batch`` via the injected ``sleep`` callable, used by
        the ``transport_timeout`` BDD scenarios with :class:`FakeClock`.
      ``sleep`` — callable taking ``seconds: float``; defaults to
        ``time.sleep``. Tests using :class:`FakeClock` pass
        ``clock.sleep`` so delay assertions cost zero wall-clock.
      ``embed_latency_s`` — wall time the fake sleeps via real
        ``time.sleep`` inside ``embed_batch`` (default ``0.0``). Used
        by ``kairix probe-config`` tests where small real-time delays
        feed warm/p95 timing assertions.
      ``embed_raises`` — when not ``None``, every ``embed_batch`` call
        raises this exception instead of returning. Used to drive the
        ``unreachable`` status branch of ``probe-config``.
      ``chat_raises`` — when not ``None``, every ``chat`` call raises
        this exception (mirrors a plugin surfacing ``ProviderUnreachable``
        from its transport).

    Socket counters — extension for ``transport_timeout.feature``.
    ``opened`` / ``closed`` / ``peak_open`` track FD usage; the
    :class:`kairix.transport.timeout.SocketCounter` Protocol uses
    these directly. Existing callers don't need to use the counters —
    the additions are backwards-compatible.
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        vector: list[float] | None = None,
        dim: int = 3,
        chat_reply: str = "",
        health: Any = None,
        embed_empty: bool = False,
        embed_delay_s: float = 0.0,
        sleep: Any = None,
        embed_latency_s: float = 0.0,
        embed_raises: BaseException | None = None,
        chat_raises: BaseException | None = None,
    ) -> None:
        import time as _time

        from kairix.providers import ProviderHealth

        self.name = name
        self._vector = list(vector) if vector is not None else [0.0] * dim
        self._dim = dim
        self._chat_reply = chat_reply
        self._health = (
            health
            if health is not None
            else ProviderHealth(
                ok=True,
                endpoint="fake://provider",
                cold_ms=0.0,
                warm_ms=0.0,
                error=None,
            )
        )
        self._embed_empty = embed_empty
        self._embed_delay_s = embed_delay_s
        self._sleep = sleep if sleep is not None else _time.sleep
        self._embed_latency_s = float(embed_latency_s)
        self._embed_raises = embed_raises
        # ``chat_raises`` — when not ``None``, every ``chat`` call raises
        # this (credential / transport failure inside the plugin).
        self._chat_raises = chat_raises
        self.embed_calls: list[list[str]] = []
        self.chat_calls: list[dict[str, Any]] = []
        self.dimension_calls: int = 0
        self.healthcheck_calls: int = 0
        # Socket counters — see SocketCounter Protocol in
        # kairix.transport.timeout. Bumped via open()/close() either by
        # the FakeProvider itself or by a TimeoutBudget that wires the
        # provider as its counter.
        self.opened: int = 0
        self.closed: int = 0
        self.peak_open: int = 0

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import time as _time

        self.embed_calls.append(list(texts))
        if self._embed_raises is not None:
            raise self._embed_raises
        if self._embed_delay_s > 0:
            self._sleep(self._embed_delay_s)
        if self._embed_latency_s > 0:
            _time.sleep(self._embed_latency_s)
        if self._embed_empty:
            return [[] for _ in texts]
        return [list(self._vector) for _ in texts]

    def chat(self, messages: list[dict[str, Any]], *, max_tokens: int = 800) -> str:
        self.chat_calls.append({"messages": list(messages), "max_tokens": max_tokens})
        if self._chat_raises is not None:
            raise self._chat_raises
        return self._chat_reply

    def dimension(self) -> int:
        self.dimension_calls += 1
        return self._dim

    def healthcheck(self) -> Any:
        self.healthcheck_calls += 1
        return self._health

    # SocketCounter Protocol — used by kairix.transport.timeout when the
    # provider doubles as the FD-accounting source. Thread-safe under the
    # lock so concurrent dispatchers can't race the peak_open update.
    def open(self) -> None:
        """Record a socket open; bump ``peak_open`` if the running balance grew."""
        # Lazy lock — only paid by tests that exercise the counter path.
        import threading

        lock = getattr(self, "_counter_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._counter_lock = lock
        with lock:
            self.opened += 1
            running = self.opened - self.closed
            if running > self.peak_open:
                self.peak_open = running

    def close(self) -> None:
        """Record a socket close — must be called for every open()."""
        import threading

        lock = getattr(self, "_counter_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._counter_lock = lock
        with lock:
            self.closed += 1


class FakeSocketCounter:
    """Standalone :class:`kairix.transport.timeout.SocketCounter`.

    The minimal FD-accounting counter — the same ``opened`` / ``closed``
    / ``peak_open`` ledger :class:`FakeProvider` carries, without the
    provider surface. Lets transport-timeout tests prove the
    ``TimeoutBudget`` ledger contract over more than one counter
    implementation (F43 parity).
    """

    def __init__(self) -> None:
        import threading

        self.opened: int = 0
        self.closed: int = 0
        self.peak_open: int = 0
        self._lock = threading.Lock()

    def open(self) -> None:
        """Record a socket open; bump ``peak_open`` if the running balance grew."""
        with self._lock:
            self.opened += 1
            self.peak_open = max(self.peak_open, self.opened - self.closed)

    def close(self) -> None:
        """Record a socket close — must be called for every open()."""
        with self._lock:
            self.closed += 1


class FakeTransportSnapshotter:
    """In-memory :class:`kairix.quality.probe.config_runner.TransportSnapshotter`.

    Returns the configured :class:`TransportSnapshot` (an empty one by
    default — the "transport layer never exercised" shape) and counts
    calls in ``snapshot_calls``.
    """

    def __init__(self, snapshot: Any = None) -> None:
        self._snapshot = snapshot
        self.snapshot_calls: int = 0

    def snapshot(self) -> Any:
        from kairix.quality.probe.config_runner import TransportSnapshot

        self.snapshot_calls += 1
        return self._snapshot if self._snapshot is not None else TransportSnapshot()


class FakeClock:
    """Deterministic clock for retry / timeout BDD scenarios.

    Replaces ``time.monotonic`` + ``time.sleep`` in tests that care
    about backoff timing or timeout enforcement without paying real
    wall-clock cost. Construct, pass ``clock.now`` and ``clock.sleep``
    into the policy under test, then call :meth:`advance` to fast-forward.

    Behaviour:

    * ``now()`` — returns the current virtual time (seconds, float).
    * ``advance(seconds)`` — advances virtual time by the supplied
      delta. Equivalent to ``sleep`` for assertion purposes but
      named to make the test intent explicit ("we are jumping the
      clock") versus ``sleep`` ("the code under test asked us to wait").
    * ``sleep(seconds)`` — records the wait elapsed (so tests can
      assert "the policy did sleep N seconds") and advances the
      virtual clock by the same amount. Does NOT block real time.
    * ``waits`` — list of every recorded sleep duration in order.

    Usage:
        clock = FakeClock()
        policy = RetryPolicy(max_attempts=3, backoff_factor=0.1,
                             sleep=clock.sleep, clock=clock.now)
        ... policy.with_retry(fn) ...
        assert clock.waits == [0.1, 0.1]  # two retries waited 100ms each

    The clock is NOT thread-safe in the strict sense, but each test
    drives a single coroutine through the policy so there's no
    contention to manage; concurrent producers should construct
    their own clock per thread.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self.waits: list[float] = []

    def now(self) -> float:
        """Read the virtual clock (seconds)."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Fast-forward virtual time by ``seconds`` without recording a wait.

        Use this when the test itself is manipulating the timeline
        (e.g. "before the lonely-request flush fires we manually
        advance past the coalesce window"). Use :meth:`sleep` instead
        when the code under test is the one asking to wait.
        """
        if seconds < 0:
            raise ValueError(f"cannot advance time by negative {seconds}")
        self._now += float(seconds)

    def sleep(self, seconds: float) -> None:
        """Record a wait + advance the clock; does not block real time."""
        if seconds < 0:
            raise ValueError(f"cannot sleep for negative {seconds}")
        self.waits.append(float(seconds))
        self._now += float(seconds)


class FakeProviderRegistry:
    """In-memory ``ProviderRegistry`` for tests.

    Implements ``kairix.providers.ProviderRegistry``: ``resolve(name)``
    and ``available()``. Takes a name→Provider mapping at construction;
    unknown names raise ``ProviderNotRegistered`` with the populated
    ``available`` list.

    Example:
        registry = FakeProviderRegistry({"openai": FakeProvider(name="openai")})
        provider = get_provider("openai", registry=registry)
    """

    def __init__(self, providers: dict[str, Any] | None = None) -> None:
        self._providers = dict(providers or {})
        self.resolve_calls: list[str] = []

    def resolve(self, name: str) -> Any:
        self.resolve_calls.append(name)
        if name not in self._providers:
            from kairix.providers import ProviderNotRegistered

            raise ProviderNotRegistered(name=name, available=self.available())
        return self._providers[name]

    def available(self) -> list[str]:
        return sorted(self._providers)


class FakeOllamaTransport:
    """Recording ``OllamaTransport`` for the Ollama provider plugin.

    Implements ``kairix.providers.ollama.OllamaTransport``:
    ``post(path, json) -> dict``. Every call is appended to
    ``recorded_requests``. ``responses`` maps a request path
    (``/api/embeddings`` / ``/api/chat``) to the decoded JSON body to
    return; unmapped paths return ``{}``. Pass ``raises=`` to make every
    ``post`` raise — e.g. ``ConnectionRefusedError`` for a stopped sidecar,
    which the plugin maps to ``ProviderUnreachable``.
    """

    def __init__(
        self,
        *,
        responses: dict[str, dict[str, Any]] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._responses = dict(responses or {})
        self._raises = raises
        self.recorded_requests: list[dict[str, Any]] = []

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        self.recorded_requests.append({"path": path, "json": dict(json)})
        if self._raises is not None:
            raise self._raises
        return dict(self._responses.get(path, {}))


# ---------------------------------------------------------------------------
# Phase 0 — memory-backend Protocol fakes.
#
# FakeMemory satisfies the Memory Protocol (id/content/score/metadata).
# FakeMemoryStore implements the MemoryStore surface as a dict-backed
# in-memory store with naive substring scoring — enough to exercise
# Protocol conformance and round-trip semantics without dragging in a
# real backend. FakeConversationStore extends with add_turn for the
# chat-paradigm protocol probe.
# ---------------------------------------------------------------------------


class FakeMemory:
    """Minimal Memory satisfying the runtime-checkable Memory Protocol."""

    def __init__(
        self,
        *,
        id: str,
        content: str,
        score: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._id = id
        self._content = content
        self._score = score
        self._metadata = dict(metadata or {})

    @property
    def id(self) -> str:
        return self._id

    @property
    def content(self) -> str:
        return self._content

    @property
    def score(self) -> float:
        return self._score

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)


class FakeMemoryStore:
    """In-memory MemoryStore — dict-backed, naive substring scoring.

    Satisfies the runtime-checkable ``MemoryStore`` Protocol. Used by
    contract tests to pin the add/search/update/delete round-trip
    semantics without standing up a real backend.

    Search scoring: returns memories whose content shares any word
    with the query, sorted by overlap ratio. Crude on purpose — the
    point of the fake is Protocol conformance, not retrieval quality.

    ``add_raises`` is the failure-injection knob: when set, ``add``
    raises it instead of persisting — mirrors a real backend whose
    write fails (``KairixNativeMemoryStore`` raises ``OSError`` when
    the memories directory can't be written).
    """

    def __init__(self, *, add_raises: BaseException | None = None) -> None:
        self._memories: dict[str, FakeMemory] = {}
        self._next_id = 0
        self._add_raises = add_raises

    def _mint_id(self) -> str:
        self._next_id += 1
        return f"fake-mem-{self._next_id:04d}"

    def add(self, content: str, *, metadata: dict[str, Any] | None = None) -> str:
        if self._add_raises is not None:
            raise self._add_raises
        mem_id = self._mint_id()
        self._memories[mem_id] = FakeMemory(id=mem_id, content=content, score=1.0, metadata=metadata)
        return mem_id

    def search(self, query: str, *, top_k: int = 10) -> list[FakeMemory]:
        q_words = set(query.lower().split())
        scored: list[tuple[float, FakeMemory]] = []
        for mem in self._memories.values():
            c_words = set(mem.content.lower().split())
            overlap = len(q_words & c_words)
            if overlap == 0:
                continue
            score = overlap / max(len(q_words), 1)
            scored.append((score, FakeMemory(id=mem.id, content=mem.content, score=score, metadata=mem.metadata)))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [m for _, m in scored[:top_k]]

    def update(self, memory_id: str, content: str) -> None:
        if memory_id not in self._memories:
            raise KeyError(f"FakeMemoryStore: no memory with id {memory_id!r}")
        existing = self._memories[memory_id]
        self._memories[memory_id] = FakeMemory(
            id=existing.id,
            content=content,
            score=existing.score,
            metadata=existing.metadata,
        )

    def delete(self, memory_id: str) -> None:
        self._memories.pop(memory_id, None)


class FakeConversationStore(FakeMemoryStore):
    """FakeMemoryStore + turn ingestion for the ConversationStore Protocol probe.

    ``add_turn`` records the turn metadata alongside the content so
    benchmark tests can verify that timestamp/role/conversation_id
    round-trip through the search surface.
    """

    def add_turn(
        self,
        *,
        message: str,
        role: str,
        conversation_id: str,
        timestamp: str | None = None,
    ) -> str:
        metadata = {
            "role": role,
            "conversation_id": conversation_id,
            "timestamp": timestamp or "1970-01-01T00:00:00Z",
        }
        return self.add(message, metadata=metadata)


# ---------------------------------------------------------------------------
# Plan B-parity — fact-extraction Protocol fakes.
#
# FakeFactRecord satisfies the FactRecord Protocol (read-only view of
# the canonical entity-attribute-value shape). FakeFactExtractor returns
# scripted records so contract tests can pin consumer behaviour without
# an LLM call. FakeFactStore is a dict-backed in-memory implementation
# of the FactStore Protocol — exercises add/search/find_conflicts/
# supersede round-trip semantics without standing up SQLite.
# ---------------------------------------------------------------------------


class FakeFactRecord:
    """Minimal FactRecord satisfying the runtime-checkable Protocol.

    Properties mirror the Protocol's read surface; backing store is
    plain instance state so test code can construct records inline.
    """

    def __init__(
        self,
        *,
        id: str,
        entity: str,
        attribute: str,
        value: str,
        confidence: float = 0.9,
        source_turn_ids: tuple[str, ...] = (),
        extracted_at: str = "1970-01-01T00:00:00Z",
        superseded_by: str | None = None,
        namespace: str = "shared",
        evidence_at: str | None = None,
        conversation_id: str | None = None,
        source_uri: str | None = None,
    ) -> None:
        self._id = id
        self._entity = entity
        self._attribute = attribute
        self._value = value
        self._confidence = confidence
        self._source_turn_ids = source_turn_ids
        self._extracted_at = extracted_at
        self._superseded_by = superseded_by
        self._namespace = namespace
        self._evidence_at = evidence_at
        self._conversation_id = conversation_id
        self._source_uri = source_uri

    @property
    def id(self) -> str:
        return self._id

    @property
    def entity(self) -> str:
        return self._entity

    @property
    def attribute(self) -> str:
        return self._attribute

    @property
    def value(self) -> str:
        return self._value

    @property
    def confidence(self) -> float:
        return self._confidence

    @property
    def source_turn_ids(self) -> tuple[str, ...]:
        return self._source_turn_ids

    @property
    def extracted_at(self) -> str:
        return self._extracted_at

    @property
    def superseded_by(self) -> str | None:
        return self._superseded_by

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def evidence_at(self) -> str | None:
        return self._evidence_at

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    @property
    def source_uri(self) -> str | None:
        return self._source_uri


class FakeFactHit:
    """Minimal FactHit Protocol satisfier — ``record`` + ``score`` properties."""

    def __init__(self, *, record: Any, score: float) -> None:
        self._record = record
        self._score = score

    @property
    def record(self) -> Any:
        return self._record

    @property
    def score(self) -> float:
        return self._score


class FakeFactExtractor:
    """Scripted FactExtractor — returns a preconfigured list of facts.

    Production-shape: an LLM-driven extractor. The fake skips the LLM
    call entirely so contract tests run sub-millisecond. Pass
    ``scripted_facts=[FakeFactRecord(...)]`` to configure what
    ``extract`` returns regardless of ``turns``.

    Records every ``extract`` invocation in ``calls`` for assertion.
    """

    def __init__(self, scripted_facts: list[Any] | None = None, *, raises: BaseException | None = None) -> None:
        self._scripted_facts = list(scripted_facts or [])
        # F68 knob — the extraction backend (LLM) fails; like the shipped
        # LLMFactExtractor, the error propagates (never a silent ``[]``).
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def extract(
        self,
        *,
        turns: list[dict[str, Any]],
        window_hint: dict[str, Any] | None = None,
        session_metadata: dict[str, Any] | None = None,
    ) -> list[Any]:
        self.calls.append(
            {
                "turns": list(turns),
                "window_hint": window_hint,
                "session_metadata": session_metadata,
            }
        )
        # Faithful to LLMFactExtractor: an empty window short-circuits to
        # ``[]`` before any backend call (no facts can be grounded).
        if not turns:
            return []
        if self._raises is not None:
            raise self._raises
        return list(self._scripted_facts)


class FakeFactStore:
    """Dict-backed in-memory FactStore — pins add/search/find_conflicts/supersede.

    Search scoring is naive substring overlap on ``value`` — enough
    to exercise the Protocol round-trip without a real BM25/vector
    backend. ``namespace`` filtering is honoured because the
    SearchPipeline federation uses it for engagement-scoped recall.
    """

    def __init__(self) -> None:
        self._facts: dict[str, Any] = {}

    def add(self, fact: Any) -> None:
        # Idempotent on the fact's deterministic id (Protocol contract).
        if fact.id not in self._facts:
            self._facts[fact.id] = fact

    def search(self, query: str, *, top_k: int = 10, namespace: str | None = None) -> list[Any]:
        q_words = set(query.lower().split())
        scored: list[tuple[float, FakeFactHit]] = []
        for fact in self._facts.values():
            if fact.superseded_by is not None:
                continue
            if namespace is not None and fact.namespace != namespace:
                continue
            haystack_words = set((fact.entity + " " + fact.attribute + " " + fact.value).lower().split())
            overlap = len(q_words & haystack_words)
            if overlap == 0:
                continue
            score = overlap / max(len(q_words), 1)
            scored.append((score, FakeFactHit(record=fact, score=score)))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [hit for _, hit in scored[:top_k]]

    def find_conflicts(self, *, entity: str, attribute: str, namespace: str | None = None) -> list[Any]:
        return [
            fact
            for fact in self._facts.values()
            if fact.superseded_by is None
            and fact.entity == entity
            and fact.attribute == attribute
            and (namespace is None or fact.namespace == namespace)
        ]

    def supersede(self, *, old_id: str, new_id: str) -> None:
        if old_id not in self._facts:
            raise KeyError(f"FakeFactStore: no fact with id {old_id!r}")
        if new_id not in self._facts:
            raise KeyError(f"FakeFactStore: no fact with id {new_id!r}")
        old = self._facts[old_id]
        # Re-mint a record carrying the superseded_by link. Preserve
        # the temporal anchor (``evidence_at``) and the provenance
        # breadcrumb (``conversation_id`` / ``source_uri``) too —
        # supersession marks a *newer* fact about the same
        # (entity, attribute); the old fact's event-time anchor and
        # source pointer stay valid for audit.
        self._facts[old_id] = FakeFactRecord(
            id=old.id,
            entity=old.entity,
            attribute=old.attribute,
            value=old.value,
            confidence=old.confidence,
            source_turn_ids=old.source_turn_ids,
            extracted_at=old.extracted_at,
            superseded_by=new_id,
            namespace=old.namespace,
            evidence_at=getattr(old, "evidence_at", None),
            conversation_id=getattr(old, "conversation_id", None),
            source_uri=getattr(old, "source_uri", None),
        )


class _FakeBudgetedResult:
    """Minimal stand-in for :class:`kairix.core.search.budget.BudgetedResult`.

    Exposes the ``.result`` (inner FusedResult-ish object), ``.content``
    (the text that ``_search_result_to_context`` consumes), ``.tier``,
    and ``.token_estimate`` fields the adapter reads. Built from
    ``FakeSearchPipeline`` so eval tests can pin the SearchPipeline-mode
    branch without spinning up a full pipeline.
    """

    def __init__(self, *, result: Any, content: str, tier: str = "L2", token_estimate: int = 0) -> None:
        self.result = result
        self.content = content
        self.tier = tier
        self.token_estimate = token_estimate


class _FakeFusedRow:
    """Minimal FusedResult-shaped row carrying ``path`` + ``title``.

    Used by FakeSearchPipeline to compose synthetic SearchResult.results
    lists. Adapter distinguishes fact rows from chunk rows by the
    ``facts://`` path prefix, so the two flavours just differ on
    ``path``.

    ``source_uri`` / ``collection`` / ``source_page`` mirror the real
    ``FusedResult`` breadcrumb fields (PLA-274 / PLA-266) so surfaces that
    project a ``SourceRef`` from the fused row — e.g. the brief's structured
    ``## Sources`` citations — see the same shape they would in production.
    They default to the FusedResult zero-values (``""`` / ``""`` / ``None``)
    so existing callers that pass only ``path`` + ``title`` are unchanged.
    """

    def __init__(
        self,
        *,
        path: str,
        title: str = "",
        source_uri: str = "",
        collection: str = "",
        source_page: int | None = None,
    ) -> None:
        self.path = path
        self.title = title
        self.source_uri = source_uri
        self.collection = collection
        self.source_page = source_page


class FakeSearchPipeline:
    """Protocol-compliant stand-in for :class:`SearchPipeline`.

    Records every ``search(...)`` call in ``calls`` for assertion. The
    response is scripted: pass ``scripted_results`` to control the
    returned ``SearchResult.results`` list. Each entry is a
    ``_FakeBudgetedResult`` so the adapter under test sees the same
    shape it would in production.

    The fake exposes the minimum surface eval needs:
      * ``search(query, ...)`` → object with ``.results`` attribute.
    No real intent classification, fusion, or budget — the unit tests
    in ``tests/quality/eval/test_suite_runner_pipeline_path.py`` pin
    the runner's *use* of the pipeline, not the pipeline itself.
    """

    def __init__(self, scripted_results: list[Any] | None = None, *, config: Any = None) -> None:
        self._scripted_results = list(scripted_results or [])
        self.calls: list[dict[str, Any]] = []
        # Mirrors the production SearchPipeline's ``.config`` (a
        # RetrievalConfig). The warm runner's cross-encoder warm step reads
        # ``pipeline.config.rerank`` + ``.rerank_intents`` to decide whether
        # rerank is wired; tests pass a real RetrievalConfig to drive it.
        self.config = config

    def search(self, query: str, **kwargs: Any) -> Any:
        self.calls.append({"query": query, "kwargs": kwargs})
        return _FakeSearchResult(results=list(self._scripted_results))

    @staticmethod
    def make_fact_row(*, fact_id: str, entity: str, attribute: str, value: str) -> _FakeBudgetedResult:
        """Build a fact-row BudgetedResult matching production's _fused_from_fact_hit shape."""
        snippet = f"{entity} {attribute}: {value}"
        return _FakeBudgetedResult(
            result=_FakeFusedRow(path=f"facts://{fact_id}", title=f"{entity} — {attribute}"),
            content=snippet,
            tier="L2",
            token_estimate=len(snippet) // 4,
        )

    @staticmethod
    def make_chunk_row(
        *,
        path: str,
        title: str,
        content: str,
        source_uri: str = "",
        collection: str = "",
        source_page: int | None = None,
    ) -> _FakeBudgetedResult:
        """Build a chunk-row BudgetedResult — non-facts path, full content text.

        ``source_uri`` / ``collection`` / ``source_page`` populate the
        FusedResult-shaped breadcrumb fields so a surface that projects a
        ``SourceRef`` from the row (PLA-266 brief citations) sees the
        canonical pointer; they default to the FusedResult zero-values.
        """
        return _FakeBudgetedResult(
            result=_FakeFusedRow(
                path=path,
                title=title,
                source_uri=source_uri,
                collection=collection,
                source_page=source_page,
            ),
            content=content,
            tier="L2",
            token_estimate=len(content) // 4,
        )


class _FakeSearchResult:
    """Minimal SearchResult shape — just the ``.results`` field the adapter reads."""

    def __init__(self, *, results: list[Any]) -> None:
        self.results = results


class FakeSearchClient:
    """Probe ``SearchClient`` for ``run_probe_search``.

    Implements ``kairix.quality.probe.clients.SearchClient``:
    ``search(query) -> Any``. Records every sampled query in ``queries``.
    Returns a ``_FakeSearchResult`` carrying ``results``; pass ``raises=``
    to make every call raise (the probe executor counts it as an error).
    """

    def __init__(self, *, results: list[Any] | None = None, raises: BaseException | None = None) -> None:
        self._results = list(results or [])
        self._raises = raises
        self.queries: list[Any] = []

    def search(self, query: Any) -> Any:
        self.queries.append(query)
        if self._raises is not None:
            raise self._raises
        return _FakeSearchResult(results=list(self._results))


class FakeBriefingSource:
    """In-memory ``BriefingSourceProtocol`` (``kairix.quality.contracts.briefing``).

    ``fetch(agent, limit)`` returns up to ``limit`` of the configured
    ``items``; pass ``raises=`` to make every call raise. Records every
    ``(agent, limit)`` in ``calls``.
    """

    def __init__(self, *, items: list[dict[str, Any]] | None = None, raises: BaseException | None = None) -> None:
        self._items = [dict(item) for item in (items or [])]
        self._raises = raises
        self.calls: list[tuple[str, int]] = []

    def fetch(self, agent: str, limit: int = 10) -> list[dict[str, Any]]:
        self.calls.append((agent, limit))
        if self._raises is not None:
            raise self._raises
        return [dict(item) for item in self._items[:limit]]


class FakeCrossEncoderLoader:
    """Recording stand-in for ``kairix.core.search.rerank.get_cross_encoder``.

    Injected into :func:`kairix.platform.warm.run_warm` through the
    ``cross_encoder_loader`` seam so a test can prove the cross-encoder model
    load is requested *exactly* when rerank is wired — without importing
    torch / sentence-transformers or loading a real model. Every requested
    model name lands in ``models``; ``calls`` is the request count.
    """

    def __init__(self, encoder: Any = None) -> None:
        self._encoder = encoder
        self.models: list[str] = []

    @property
    def calls(self) -> int:
        """Number of times the loader was invoked."""
        return len(self.models)

    def __call__(self, model: str) -> Any:
        self.models.append(model)
        return self._encoder


# ---------------------------------------------------------------------------
# Spike C1 unified corpus-ingest Protocol fakes.
#
# FakeDocumentWriter satisfies the DocumentWriter Protocol — captures
# every write call in ``writes`` and returns a synthetic Path so the
# ingest_corpus result can populate ``document_paths`` without touching
# the filesystem. FakeCorpusEmbedder satisfies the CorpusEmbedder
# Protocol — captures every embed call in ``calls`` and returns a
# scripted chunk count (or default 0).
# ---------------------------------------------------------------------------


class FakeDocumentWriter:
    """Capture-only DocumentWriter — records writes, no filesystem I/O.

    Pass ``base_path=Path(...)`` to control the parent directory the
    fake's returned Paths sit under. Defaults to ``/fake/documents``
    so the sentinel surfaces if tests accidentally try to read what
    they wrote.

    Every ``write(...)`` call lands in ``writes`` as a dict carrying
    the four kwargs; the returned Path is
    ``<base_path> / <corpus_id> / <session_id>.md``.
    """

    def __init__(self, base_path: Path | str = "/fake/documents", *, raises: BaseException | None = None) -> None:
        self._base_path = Path(base_path)
        self.writes: list[dict[str, Any]] = []
        # F68 knob — the backing store rejects the write (disk full, FTS5
        # rebuild error, permission denied); propagates, never a sentinel path.
        self._raises = raises

    def write(
        self,
        *,
        corpus_id: str,
        session_id: str,
        rendered_body: str,
        frontmatter: dict[str, Any],
    ) -> Path:
        if self._raises is not None:
            raise self._raises
        self.writes.append(
            {
                "corpus_id": corpus_id,
                "session_id": session_id,
                "rendered_body": rendered_body,
                "frontmatter": dict(frontmatter),
            }
        )
        return self._base_path / corpus_id / f"{session_id}.md"


class FakePassthroughExtractor:
    """Canonical fake for the passthrough extractor plugin (F43 contract layer).

    Implements the :class:`kairix.extractors.Extractor` Protocol with
    the minimum behaviour the spec promises: claim ``text/*`` mime
    types, decode UTF-8 bytes into ``ExtractedDocument.markdown``,
    and report ``quality_ok`` based on whether the markdown has any
    non-whitespace content.

    Used by ``tests/contracts/test_passthrough_protocol.py`` to prove
    that the real :class:`PassthroughExtractor` satisfies the same
    Protocol surface a downstream consumer would expect.
    """

    def __init__(self, *, version: str = "1.0.0") -> None:
        from kairix.extractors import (
            DocMetadata,
            ExtractedDocument,
            MimeType,
        )

        self.name = "passthrough"
        self.version = version
        self._DocMetadata = DocMetadata
        self._ExtractedDocument = ExtractedDocument
        self._MimeType = MimeType

    def can_extract(self, mime: str, magic_bytes: bytes) -> bool:
        del magic_bytes
        return isinstance(mime, str) and mime.startswith("text/")

    def extract(self, raw: bytes, mime: str) -> Any:
        del mime
        text = raw.decode("utf-8", errors="replace")
        return self._ExtractedDocument(
            markdown=text,
            pages=(),
            images=(),
            metadata=self._DocMetadata(
                title=None,
                author=None,
                created_date=None,
                language=None,
                page_count=None,
            ),
            confidence=1.0,
        )

    def quality_ok(self, doc: Any) -> bool:
        return bool(doc.markdown.strip())

    def metadata_for(self, raw: bytes, mime: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only)."""
        del raw, mime
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakePdfFallbackExtractor:
    """Canonical fake for the pdf_fallback extractor plugin (F43 contract layer).

    Implements the :class:`kairix.extractors.Extractor` Protocol without
    invoking the real :mod:`pdfplumber` library. Claims
    ``application/pdf`` (and any bytes starting with ``%PDF``), returns
    a scripted markdown string with at least one page carrying non-
    empty text — sized to clear the production ``>=100 char`` floor.

    Used by ``tests/contracts/test_pdf_fallback_protocol.py`` to prove
    that the real :class:`PdfFallbackExtractor` satisfies the same
    Protocol surface a downstream consumer would expect.
    """

    def __init__(
        self,
        *,
        version: str = "0.11.9",
        scripted_markdown: str | None = None,
        scripted_page_text: str | None = None,
    ) -> None:
        from kairix.extractors import (
            DocMetadata,
            ExtractedDocument,
            MimeType,
            Page,
        )

        self.name = "pdf_fallback"
        self.version = version
        # ``is not None`` (not ``or``) so an explicit ``""`` models the
        # real extractor's image-only / scanned-PDF output (empty text layer).
        self.scripted_markdown = (
            scripted_markdown
            if scripted_markdown is not None
            else ("Recovered PDF content from the fallback extractor.\n" + ("Line of body text from page one.\n" * 6))
        )
        self.scripted_page_text = (
            scripted_page_text
            if scripted_page_text is not None
            else "Recovered PDF page text from the fallback extractor."
        )
        self._DocMetadata = DocMetadata
        self._ExtractedDocument = ExtractedDocument
        self._MimeType = MimeType
        self._Page = Page

    def can_extract(self, mime: str, magic_bytes: bytes) -> bool:
        if isinstance(mime, str) and mime == "application/pdf":
            return True
        return magic_bytes[:4] == b"%PDF"

    def extract(self, raw: bytes, mime: str) -> Any:
        del mime
        markdown = self.scripted_markdown
        page = self._Page(page_number=1, text=self.scripted_page_text, has_images=False)
        confidence = min(len(markdown) / max(len(raw), 1), 1.0) if raw else 0.0
        return self._ExtractedDocument(
            markdown=markdown,
            pages=(page,),
            images=(),
            metadata=self._DocMetadata(
                title="fixture",
                author=None,
                created_date=None,
                language=None,
                page_count=1,
            ),
            confidence=confidence,
        )

    def quality_ok(self, doc: Any) -> bool:
        if len(doc.markdown) < 100:
            return False
        return any(page.text.strip() for page in doc.pages)

    def metadata_for(self, raw: bytes, mime: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only)."""
        del raw, mime
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeMarkitdownExtractor:
    """Canonical fake for the markitdown extractor plugin (F43 contract layer).

    Implements the :class:`kairix.extractors.Extractor` Protocol
    without invoking the real :mod:`markitdown` library. Returns a
    scripted markdown string (default: a non-empty paragraph) so the
    quality-gate assertions pass; sized to clear the production
    ``>=50 char`` floor.

    Used by ``tests/contracts/test_markitdown_protocol.py`` to prove
    that the real :class:`MarkitdownExtractor` satisfies the same
    Protocol surface a downstream consumer would expect.
    """

    def __init__(
        self,
        *,
        version: str = "0.1.5",
        scripted_markdown: str | None = None,
    ) -> None:
        from kairix.extractors import (
            DocMetadata,
            ExtractedDocument,
            MimeType,
        )

        self.name = "markitdown"
        self.version = version
        self.scripted_markdown = scripted_markdown or ("# Recovered document\n\n" + ("scripted markdown line\n" * 8))
        self._DocMetadata = DocMetadata
        self._ExtractedDocument = ExtractedDocument
        self._MimeType = MimeType
        self._supported_mimes = frozenset(
            {
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/html",
                "application/xhtml+xml",
            }
        )

    def can_extract(self, mime: str, magic_bytes: bytes) -> bool:
        if isinstance(mime, str) and mime in self._supported_mimes:
            return True
        return magic_bytes.startswith(b"%PDF")

    def extract(self, raw: bytes, mime: str) -> Any:
        del mime
        markdown = self.scripted_markdown
        confidence = min(len(markdown) / max(len(raw), 1), 1.0) if raw else 0.0
        return self._ExtractedDocument(
            markdown=markdown,
            pages=(),
            images=(),
            metadata=self._DocMetadata(
                title="fixture",
                author=None,
                created_date=None,
                language=None,
                page_count=None,
            ),
            confidence=confidence,
        )

    def quality_ok(self, doc: Any) -> bool:
        text = doc.markdown.strip()
        if len(text) < 50:
            return False
        return bool(doc.confidence >= 0.10)

    def metadata_for(self, raw: bytes, mime: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only)."""
        del raw, mime
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeOcrExtractor:
    """Canonical fake for the OCR extractor plugin (F43 contract layer).

    Implements the :class:`kairix.extractors.Extractor` Protocol
    without invoking :mod:`pytesseract`, :mod:`pdfplumber`, or any of
    the OCR extra's other libraries. Returns a scripted markdown
    string with a scripted confidence so quality-gate assertions
    can be parameterised across the fake AND the real impl.

    The fake claims ``application/pdf`` and the common image mimes
    (matching the production plugin's surface) but refuses
    ``text/*`` (that's passthrough's job).

    Used by ``tests/contracts/test_ocr_protocol.py`` to prove that
    the real :class:`OcrExtractor` satisfies the same Protocol
    surface a downstream consumer would expect.
    """

    def __init__(
        self,
        *,
        version: str = "1.0.0",
        scripted_markdown: str | None = None,
        scripted_confidence: float = 0.85,
    ) -> None:
        from kairix.extractors import (
            DocMetadata,
            ExtractedDocument,
        )

        self.name = "ocr"
        self.version = version
        self.scripted_markdown = scripted_markdown or ("## Page 1\n\n" + ("recognised line of text\n" * 8))
        self.scripted_confidence = scripted_confidence
        self._DocMetadata = DocMetadata
        self._ExtractedDocument = ExtractedDocument
        self._supported_mimes = frozenset(
            {
                "application/pdf",
                "image/png",
                "image/jpeg",
                "image/jpg",
                "image/tiff",
                "image/bmp",
            }
        )

    def can_extract(self, mime: str, magic_bytes: bytes) -> bool:
        if isinstance(mime, str) and mime in self._supported_mimes:
            return True
        return magic_bytes.startswith(b"%PDF")

    def extract(self, raw: bytes, mime: str) -> Any:
        del raw, mime
        return self._ExtractedDocument(
            markdown=self.scripted_markdown,
            pages=(),
            images=(),
            metadata=self._DocMetadata(
                title=None,
                author=None,
                created_date=None,
                language=None,
                page_count=1,
            ),
            confidence=self.scripted_confidence,
        )

    def quality_ok(self, doc: Any) -> bool:
        text = doc.markdown.strip()
        if len(text) < 50:
            return False
        return bool(doc.confidence >= 0.6)

    def metadata_for(self, raw: bytes, mime: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only)."""
        del raw, mime
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakePptxExtractor:
    """Canonical fake for the pptx extractor plugin (F43 contract layer).

    Implements the :class:`kairix.extractors.Extractor` Protocol without
    invoking the real :mod:`pptx` library. Returns a scripted three-slide
    deck so the per-slide Page assertions and the speaker-notes
    quality-gate assertions can be parameterised across the fake AND the
    real impl.

    Used by ``tests/contracts/test_pptx_protocol.py`` to prove that the
    real :class:`PptxExtractor` satisfies the same Protocol surface a
    downstream consumer would expect.
    """

    def __init__(
        self,
        *,
        version: str = "1.0.2",
        scripted_slide_count: int = 3,
        include_notes: bool = True,
    ) -> None:
        from kairix.extractors import (
            DocMetadata,
            ExtractedDocument,
            Page,
        )

        self.name = "pptx"
        self.version = version
        self.scripted_slide_count = scripted_slide_count
        self.include_notes = include_notes
        self._DocMetadata = DocMetadata
        self._ExtractedDocument = ExtractedDocument
        self._Page = Page
        self._pptx_mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

    def can_extract(self, mime: str, magic_bytes: bytes) -> bool:
        if isinstance(mime, str) and mime == self._pptx_mime:
            return True
        return bool(magic_bytes.startswith(b"PK\x03\x04") and isinstance(mime, str) and mime.endswith("presentation"))

    def extract(self, raw: bytes, mime: str) -> Any:
        del mime
        slides_md: list[str] = []
        pages: list[Any] = []
        for i in range(self.scripted_slide_count):
            n = i + 1
            slide_md = f"## Slide {n}: Scripted Slide {n}\n\nBody text for slide {n}."
            if self.include_notes:
                slide_md += f"\n\n> **Speaker notes**: Notes for slide {n}."
            slides_md.append(slide_md)
            pages.append(self._Page(page_number=n, text=slide_md, has_images=False))
        markdown = "\n\n".join(slides_md)
        confidence = min(len(markdown) / max(len(raw), 1), 1.0) if raw else 0.0
        return self._ExtractedDocument(
            markdown=markdown,
            pages=tuple(pages),
            images=(),
            metadata=self._DocMetadata(
                title="Scripted Deck",
                author="agent-alpha",
                created_date=None,
                language=None,
                page_count=self.scripted_slide_count,
            ),
            confidence=confidence,
        )

    def quality_ok(self, doc: Any) -> bool:
        if len(doc.pages) == 0:
            return False
        return len(doc.markdown.strip()) >= 100

    def metadata_for(self, raw: bytes, mime: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only)."""
        del raw, mime
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeDocxExtractor:
    """Canonical fake for the docx extractor plugin (F43 contract layer).

    Implements the :class:`kairix.extractors.Extractor` Protocol
    without invoking the real :mod:`docx` (python-docx) library.
    Returns a scripted markdown string carrying at least one
    ``#``-prefixed heading line so the heading-aware quality gate
    passes.
    """

    def __init__(
        self,
        *,
        version: str = "1.2.0",
        scripted_markdown: str | None = None,
        scripted_has_tracked_changes: bool = False,
    ) -> None:
        from kairix.extractors import (
            DocMetadata,
            ExtractedDocument,
        )

        self.name = "docx"
        self.version = version
        self.scripted_markdown = scripted_markdown or (
            "# Section One\n\n"
            + ("Body paragraph from the docx fixture.\n" * 6)
            + "\n## Subsection\n\n"
            + "More body text for coverage.\n"
        )
        self.last_extract_had_tracked_changes = scripted_has_tracked_changes
        self._scripted_has_tracked_changes = scripted_has_tracked_changes
        self._DocMetadata = DocMetadata
        self._ExtractedDocument = ExtractedDocument
        self._docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def can_extract(self, mime: str, magic_bytes: bytes) -> bool:
        if isinstance(mime, str) and mime == self._docx_mime:
            return True
        if magic_bytes.startswith(b"PK\x03\x04") and isinstance(mime, str) and mime.endswith("document"):
            return True
        return False

    def extract(self, raw: bytes, mime: str) -> Any:
        del mime
        markdown = self.scripted_markdown
        confidence = min(len(markdown) / max(len(raw), 1), 1.0) if raw else 0.0
        self.last_extract_had_tracked_changes = self._scripted_has_tracked_changes
        return self._ExtractedDocument(
            markdown=markdown,
            pages=(),
            images=(),
            metadata=self._DocMetadata(
                title="fixture",
                author=None,
                created_date=None,
                language=None,
                page_count=None,
            ),
            confidence=confidence,
        )

    def quality_ok(self, doc: Any) -> bool:
        if len(doc.markdown) < 100:
            return False
        for line in doc.markdown.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#") and " " in stripped:
                return True
        return False

    def metadata_for(self, raw: bytes, mime: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only)."""
        del raw, mime
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


# ---------------------------------------------------------------------------
# Upstream-library adapter fakes for the extractor plugins' wire-shape
# Protocols (``_PdfPage`` / ``_PdfDocument`` / ``_DocxDocument`` /
# ``_PptxPresentation`` / ``_MarkitdownConverter`` / ``OcrRunner`` /
# ``PageRenderer``). Each is injected through the real extractor's opener /
# loader / factory seam so failure-mode contracts drive the production
# extractor code against a failing upstream document object.
# ---------------------------------------------------------------------------


class FakePdfPage:
    """``pdfplumber.Page``-shaped fake (``_PdfPage`` Protocol).

    ``text=None`` models an image-only (scanned) page whose text layer is
    empty — what pdfplumber returns for a page with no extractable text.
    """

    def __init__(self, *, text: str | None = None, tables: list[list[list[str | None]]] | None = None) -> None:
        self._text = text
        self._tables = [list(t) for t in (tables or [])]

    def extract_text(self) -> str | None:
        return self._text

    def extract_tables(self) -> list[list[list[str | None]]]:
        return [list(t) for t in self._tables]


class FakePdfDocument:
    """Context-managed ``pdfplumber.PDF``-shaped fake (``_PdfDocument`` Protocol)."""

    def __init__(self, *, pages: list[Any] | None = None, metadata: dict[str, Any] | None = None) -> None:
        self._pages = list(pages or [])
        self._metadata = dict(metadata or {})

    @property
    def pages(self) -> list[Any]:
        return list(self._pages)

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    def __enter__(self) -> FakePdfDocument:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class FakeDocxDocument:
    """``docx.Document``-shaped fake (``_DocxDocument`` Protocol).

    Paragraph / table entries are any objects exposing python-docx's
    ``.text`` / ``.style.name`` / ``.rows[].cells[].text`` attributes.
    ``element`` defaults to a body-less document element (no
    ``<w:body>``), and ``core_properties`` to an all-empty property set.
    """

    def __init__(
        self,
        *,
        paragraphs: list[Any] | None = None,
        tables: list[Any] | None = None,
        element: Any = None,
        core_properties: Any = None,
    ) -> None:
        from types import SimpleNamespace

        self._paragraphs = list(paragraphs or [])
        self._tables = list(tables or [])
        self._element = element if element is not None else SimpleNamespace(body=None)
        self._core_properties = (
            core_properties if core_properties is not None else SimpleNamespace(title="", author="", created=None)
        )

    @property
    def paragraphs(self) -> list[Any]:
        return list(self._paragraphs)

    @property
    def tables(self) -> list[Any]:
        return list(self._tables)

    @property
    def element(self) -> Any:
        return self._element

    @property
    def core_properties(self) -> Any:
        return self._core_properties


class FakePptxPresentation:
    """``pptx.Presentation``-shaped fake (``_PptxPresentation`` Protocol).

    ``slides`` is any sized iterable of slide-shaped objects;
    ``core_properties`` defaults to an all-empty property set.
    """

    def __init__(self, *, slides: list[Any] | None = None, core_properties: Any = None) -> None:
        from types import SimpleNamespace

        self._slides = list(slides or [])
        self._core_properties = (
            core_properties if core_properties is not None else SimpleNamespace(title="", author="", created=None)
        )

    @property
    def slides(self) -> list[Any]:
        return list(self._slides)

    @property
    def core_properties(self) -> Any:
        return self._core_properties


class FakeMarkitdownConverter:
    """``markitdown.MarkItDown``-shaped fake (``_MarkitdownConverter`` Protocol).

    ``convert(source)`` returns an object with ``.markdown`` /
    ``.text_content`` / ``.title``; pass ``raises=`` to make every
    conversion raise. Records every converted source in ``sources``.
    """

    def __init__(self, *, markdown: str = "", title: str | None = None, raises: BaseException | None = None) -> None:
        self._markdown = markdown
        self._title = title
        self._raises = raises
        self.sources: list[Any] = []

    def convert(self, source: Any, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        del kwargs
        self.sources.append(source)
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(markdown=self._markdown, text_content=self._markdown, title=self._title)


class FakePageRenderer:
    """OCR ``PageRenderer`` fake — returns pre-built page images.

    ``pages`` defaults to one flat 32x32 white uint8 page. Pass
    ``raises=`` to make ``render`` raise (e.g. Pillow's
    ``UnidentifiedImageError`` for undecodable bytes).
    """

    def __init__(self, *, pages: tuple[Any, ...] | None = None, raises: BaseException | None = None) -> None:
        self._pages = pages
        self._raises = raises
        self.calls: list[str] = []

    def render(self, raw: bytes, mime: str) -> tuple[Any, ...]:
        import numpy as np

        del raw
        self.calls.append(mime)
        if self._raises is not None:
            raise self._raises
        if self._pages is not None:
            return tuple(self._pages)
        return (np.full((32, 32), 255, dtype=np.uint8),)


class FakeOcrRunner:
    """OCR ``OcrRunner`` fake — scripted orientation + recognition.

    Defaults to an upright page recognised as ``text`` at 90% mean
    confidence. ``detect_raises`` / ``recognise_raises`` make the
    respective call raise.
    """

    def __init__(
        self,
        *,
        text: str = "Recognised OCR text.",
        mean_confidence: float = 90.0,
        word_count: int = 3,
        rotation_degrees: int = 0,
        detect_raises: BaseException | None = None,
        recognise_raises: BaseException | None = None,
    ) -> None:
        self._text = text
        self._mean_confidence = mean_confidence
        self._word_count = word_count
        self._rotation_degrees = rotation_degrees
        self._detect_raises = detect_raises
        self._recognise_raises = recognise_raises

    def detect_orientation(self, img: Any) -> Any:
        from kairix.extractors.ocr.tesseract_runner import OrientationResult

        del img
        if self._detect_raises is not None:
            raise self._detect_raises
        return OrientationResult(rotation_degrees=self._rotation_degrees, orientation_confidence=10.0)

    def recognise_text(self, img: Any) -> Any:
        from kairix.extractors.ocr.tesseract_runner import RecognitionResult

        del img
        if self._recognise_raises is not None:
            raise self._recognise_raises
        return RecognitionResult(
            text=self._text,
            mean_confidence=self._mean_confidence,
            word_count=self._word_count,
        )


class FakePytesseractModule:
    """Module-shaped stand-in for :mod:`pytesseract` (external SDK boundary).

    Injected through ``TesseractRunner(pytesseract_module=...)`` so the real
    runner's OSD / ``image_to_data`` handling runs without the Tesseract
    binary. ``osd`` / ``data`` are the dicts returned by ``image_to_osd`` /
    ``image_to_data``; ``osd_raises`` / ``data_raises`` make them raise.
    Exposes ``Output.DICT`` and a ``TesseractError`` class like the real
    module.
    """

    class TesseractError(RuntimeError):
        """Mirror of ``pytesseract.TesseractError``."""

    class Output:
        """Mirror of ``pytesseract.Output``."""

        DICT = "dict"

    def __init__(
        self,
        *,
        osd: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        osd_raises: BaseException | None = None,
        data_raises: BaseException | None = None,
    ) -> None:
        self._osd = dict(osd or {"rotate": 0, "orientation_conf": 10.0})
        self._data = dict(data or {"text": ["Recognised", "OCR", "text."], "conf": [90, 90, 90]})
        self._osd_raises = osd_raises
        self._data_raises = data_raises

    def image_to_osd(self, img: Any, output_type: Any = None) -> dict[str, Any]:
        del img, output_type
        if self._osd_raises is not None:
            raise self._osd_raises
        return dict(self._osd)

    def image_to_data(self, img: Any, output_type: Any = None) -> dict[str, Any]:
        del img, output_type
        if self._data_raises is not None:
            raise self._data_raises
        return {key: list(value) for key, value in self._data.items()}


class FakeXlsxExtractor:
    """Canonical fake for the xlsx extractor plugin (F43 contract layer).

    Implements the :class:`kairix.extractors.Extractor` Protocol without
    invoking the real :mod:`openpyxl` library. Returns a scripted set of
    ``Page`` objects (one per "sheet").

    ``scripted_sheets=`` models a concrete workbook as ``(title, rows)``
    pairs and renders it the way the real extractor does: sheets with no
    non-blank cell are skipped, each surviving sheet becomes one ``Page``
    (numbered by its 1-based sheet index) headed ``## Sheet: <title>``
    with a pipe-syntax table (first row = header, ``---`` separator).
    """

    def __init__(
        self,
        *,
        version: str = "3.1.5",
        scripted_sheet_count: int = 2,
        scripted_sheet_markdown: str | None = None,
        scripted_sheets: tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...] | None = None,
    ) -> None:
        from kairix.extractors import (
            DocMetadata,
            ExtractedDocument,
            MimeType,
            Page,
        )

        self.name = "xlsx"
        self.version = version
        self.scripted_sheet_count = scripted_sheet_count
        self.scripted_sheet_markdown = scripted_sheet_markdown or (
            "## Sheet: Fixture\n\n| col1 | col2 |\n| --- | --- |\n| a | b |\n| c | d |\n"
        )
        self._DocMetadata = DocMetadata
        self._ExtractedDocument = ExtractedDocument
        self._MimeType = MimeType
        self._Page = Page
        self._supported_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        self.scripted_sheets = scripted_sheets

    def can_extract(self, mime: str, magic_bytes: bytes) -> bool:
        if isinstance(mime, str) and mime == self._supported_mime:
            return True
        if not magic_bytes.startswith(b"PK\x03\x04"):
            return False
        return bool(isinstance(mime, str) and mime.endswith("sheet"))

    @staticmethod
    def _render_scripted_sheet(title: str, rows: tuple[tuple[Any, ...], ...]) -> str:
        cells = [["" if cell is None else str(cell).strip() for cell in row] for row in rows]
        width = max(len(row) for row in cells)
        padded = [row + [""] * (width - len(row)) for row in cells]
        lines = ["| " + " | ".join(padded[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
        return f"## Sheet: {title}\n\n" + "\n".join(lines)

    def _scripted_numbered_sections(self) -> list[tuple[int, str]]:
        if self.scripted_sheets is None:
            return [(index, self.scripted_sheet_markdown) for index in range(1, self.scripted_sheet_count + 1)]
        out: list[tuple[int, str]] = []
        for index, (title, rows) in enumerate(self.scripted_sheets, start=1):
            if not any(cell is not None and str(cell).strip() for row in rows for cell in row):
                continue  # mirrors the real extractor's empty-sheet skip
            out.append((index, self._render_scripted_sheet(title, rows)))
        return out

    def extract(self, raw: bytes, mime: str) -> Any:
        del mime
        numbered = self._scripted_numbered_sections()
        sections = [section for _, section in numbered]
        markdown = "\n\n".join(sections)
        pages = tuple(self._Page(page_number=index, text=section, has_images=False) for index, section in numbered)
        confidence = min(len(markdown) / max(len(raw), 1), 1.0) if raw else 0.0
        return self._ExtractedDocument(
            markdown=markdown,
            pages=pages,
            images=(),
            metadata=self._DocMetadata(
                title="fixture",
                author=None,
                created_date=None,
                language=None,
                page_count=self.scripted_sheet_count,
            ),
            confidence=confidence,
        )

    def quality_ok(self, doc: Any) -> bool:
        if len(doc.pages) < 1:
            return False
        return len(doc.markdown) >= 100

    def metadata_for(self, raw: bytes, mime: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only)."""
        del raw, mime
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeGotenbergExtractor:
    """Canonical fake for the gotenberg conversion-tier extractor (F43 contract layer).

    Implements the :class:`kairix.extractors.Extractor` Protocol without
    reaching the real gotenberg HTTP service. Claims the
    legacy-Office/ODF/Visio/Publisher/RTF mimes the real tier converts,
    and — crucially, mirroring the real ``can_extract`` — REFUSES
    ``application/pdf`` / ``text/*`` / ``application/octet-stream`` AND the
    modern OOXML mimes (.docx / .pptx / .xlsx, owned by the in-process
    markitdown / pptx / docx / xlsx tiers) so the tier never shadows
    ``pdf_fallback`` / ``passthrough`` or an in-process extractor.
    ``extract`` returns a scripted converted-then-extracted document (one
    text-bearing page) sized to clear the production ``>=100 char`` floor.

    Used by ``tests/contracts/test_gotenberg_protocol.py`` to prove the
    real :class:`GotenbergExtractor` satisfies the same Protocol surface a
    downstream consumer would expect.
    """

    def __init__(
        self,
        *,
        version: str = "1.0.0",
        scripted_markdown: str | None = None,
        scripted_page_text: str | None = None,
    ) -> None:
        from kairix.extractors import (
            DocMetadata,
            ExtractedDocument,
            MimeType,
            Page,
        )

        self.name = "gotenberg"
        self.version = version
        self.scripted_markdown = scripted_markdown or (
            "Recovered Office content via the gotenberg conversion tier.\n"
            + ("Line of body text from the converted PDF page one.\n" * 6)
        )
        self.scripted_page_text = scripted_page_text or "Recovered converted-PDF page text from the gotenberg tier."
        self._DocMetadata = DocMetadata
        self._ExtractedDocument = ExtractedDocument
        self._MimeType = MimeType
        self._Page = Page
        # Mirror the real tier's mime allow-list (a representative subset
        # — the contract test exercises a legacy-Office mime as the
        # canonical case; the full set is unit-tested in test_gotenberg.py).
        # Modern OOXML (.docx / .pptx / .xlsx) is deliberately ABSENT — the
        # real tier refuses it (the in-process markitdown / pptx / docx /
        # xlsx extractors own those), so the fake must too.
        self._claimed_mimes = frozenset(
            {
                "application/msword",
                "application/vnd.ms-excel",
                "application/vnd.ms-powerpoint",
                "application/vnd.oasis.opendocument.text",
                "application/vnd.oasis.opendocument.spreadsheet",
                "application/vnd.oasis.opendocument.presentation",
                "application/vnd.oasis.opendocument.graphics",
                "application/vnd.ms-visio.drawing",
                "application/vnd.ms-visio.drawing.macroenabled.12",
                "application/vnd.visio",
                "application/x-mspublisher",
                "application/rtf",
                "text/rtf",
            }
        )

    def can_extract(self, mime: str, magic_bytes: bytes) -> bool:
        del magic_bytes
        return isinstance(mime, str) and mime in self._claimed_mimes

    def extract(self, raw: bytes, mime: str) -> Any:
        del mime
        markdown = self.scripted_markdown
        page = self._Page(page_number=1, text=self.scripted_page_text, has_images=False)
        confidence = min(len(markdown) / max(len(raw), 1), 1.0) if raw else 0.0
        return self._ExtractedDocument(
            markdown=markdown,
            pages=(page,),
            images=(),
            metadata=self._DocMetadata(
                title="fixture",
                author=None,
                created_date=None,
                language=None,
                page_count=1,
            ),
            confidence=confidence,
        )

    def quality_ok(self, doc: Any) -> bool:
        if len(doc.markdown) < 100:
            return False
        return any(page.text.strip() for page in doc.pages)

    def metadata_for(self, raw: bytes, mime: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only)."""
        del raw, mime
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeEmbeddingCache:
    """In-memory ``EmbeddingCache``-compatible fake for unit tests.

    Mirrors :class:`kairix.core.embed.embedding_cache.EmbeddingCache`'s
    shape — ``get_many`` / ``put_many`` / ``count`` / ``clear`` /
    ``close`` — but stores rows in a plain dict instead of SQLite. Use
    when a test wants to assert "the cache was consulted with these
    hashes" or "the cache contains exactly N vectors" without paying
    the SQLite open cost.

    Integration tests that want production fidelity should instead
    construct a real :class:`EmbeddingCache` against ``tmp_path``.

    Records every ``get_many`` / ``put_many`` call on ``get_calls`` /
    ``put_calls`` so tests can sabotage-prove the integration boundary.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, int, str], list[float]] = {}
        self.get_calls: list[tuple[str, int, list[str]]] = []
        self.put_calls: list[tuple[str, int, list[str]]] = []
        self.closed: bool = False

    def get_many(
        self,
        model: str,
        dimension: int,
        chunk_hashes: Any,
    ) -> dict[str, Any]:
        import numpy as np

        hashes = list(chunk_hashes)
        self.get_calls.append((model, dimension, hashes))
        out: dict[str, Any] = {}
        for h in hashes:
            stored = self._store.get((model, dimension, h))
            if stored is not None:
                out[h] = np.asarray(stored, dtype="float32")
        return out

    def put_many(
        self,
        model: str,
        dimension: int,
        pairs: Any,
    ) -> int:
        written = 0
        recorded_hashes: list[str] = []
        for chunk_hash, vector in pairs:
            self._store[(model, dimension, chunk_hash)] = list(vector)
            recorded_hashes.append(chunk_hash)
            written += 1
        self.put_calls.append((model, dimension, recorded_hashes))
        return written

    def count(self, model: str | None = None, dimension: int | None = None) -> int:
        if model is None and dimension is None:
            return len(self._store)
        return sum(1 for (m, d, _h) in self._store if m == model and d == dimension)

    def clear(self) -> None:
        self._store.clear()

    def close(self) -> None:
        self.closed = True


class FakeCorpusEmbedder:
    """Capture-only CorpusEmbedder — records embed calls, returns scripted counts.

    Pass ``scripted_chunks_per_call=[3, 1, 0]`` to make consecutive
    ``embed(...)`` calls return 3, 1, 0 chunks. When the script runs
    out, the fake returns the final value (default 0). Every call's
    paths argument lands in ``calls`` so tests can verify the
    embedder saw the documents the writer just produced.
    """

    def __init__(
        self,
        scripted_chunks_per_call: list[int] | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self._scripted = list(scripted_chunks_per_call or [])
        self.calls: list[tuple[Path, ...]] = []
        # F68 knob — the underlying embed pipeline crashes (CUDA OOM, vector
        # index write error); propagates, never a silent ``0``.
        self._raises = raises

    def embed(self, paths_to_embed: tuple[Path, ...]) -> int:
        self.calls.append(tuple(paths_to_embed))
        if self._raises is not None:
            raise self._raises
        if not self._scripted:
            return 0
        if len(self.calls) <= len(self._scripted):
            return self._scripted[len(self.calls) - 1]
        return self._scripted[-1]


# ---------------------------------------------------------------------------
# Connector fakes (Wave 2 — IM-5)
# ---------------------------------------------------------------------------


class FakeObsidian:
    """Scripted :class:`kairix.core.protocols.SourceConnector` for the Obsidian
    plugin's contract test.

    Constructor takes the events to emit and a content fixture; the fake
    satisfies the full Protocol surface without touching the filesystem,
    watchdog, or any real thread. This is the canonical fake F43 pairs
    with the real :class:`kairix.connectors.obsidian.ObsidianConnector`
    inside ``tests/contracts/test_obsidian_protocol.py``.
    """

    name: str = "obsidian"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = None

    def __init__(
        self,
        *,
        vault_root: Path | str = "/fake/vault",
        events: list[Any] | None = None,
        content: dict[str, bytes] | None = None,
        sensitivity: str = "internal",
    ) -> None:
        from kairix.core.protocols import ChangeEvent  # local import — avoids reordering top-of-file

        self.vault_root = Path(vault_root)
        self._events: list[ChangeEvent] = list(events) if events is not None else []
        self._content: dict[str, bytes] = dict(content) if content is not None else {}
        self._sensitivity = sensitivity

    def list_changes(self, cursor: Any | None) -> Any:
        return iter(self._events)

    def fetch(self, item_id: str) -> Any:
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        raw = self._content.get(item_id, b"")
        mime = "text/markdown" if item_id.endswith(".md") else "application/octet-stream"
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=raw, mime=mime, fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        return f"obsidian://open?vault={self.vault_root.name}&file={item_id}"

    def sensitivity_for(self, _item_id: str) -> Any:
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Return the max ``modified_at`` across seeded events (ISO timestamp cursor)."""
        if not self._events:
            return None
        return max(ev.modified_at for ev in self._events)

    def metadata_for(self, item_id: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only).

        ADR-021 (Wave E.5): real envelope extraction lives on the
        shipped :class:`ObsidianConnector`.
        """
        del item_id
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeDexCrmConnector:
    """Scripted :class:`kairix.core.protocols.SourceConnector` for the
    Dex CRM connector plugin's contract test.

    Constructor takes the events to emit and a content map keyed by
    item_id. The fake satisfies the full Protocol surface without any
    HTTP, secret resolution, or threading. This is the canonical fake
    F43 pairs with the real
    :class:`kairix.connectors.dex_crm.DexCrmConnector` inside
    ``tests/contracts/test_dex_crm_protocol.py``.
    """

    name: str = "dex_crm"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = None

    def __init__(
        self,
        *,
        events: list[Any] | None = None,
        content: dict[str, Any] | None = None,
        sensitivity: str = "internal",
    ) -> None:
        from kairix.core.protocols import ChangeEvent  # local import — avoids reordering top-of-file

        self._events: list[ChangeEvent] = list(events) if events is not None else []
        self._content: dict[str, Any] = dict(content) if content is not None else {}
        self._sensitivity = sensitivity

    def list_changes(self, cursor: Any | None) -> Any:
        del cursor
        return iter(self._events)

    def fetch(self, item_id: str) -> Any:
        import json
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        payload = self._content.get(item_id, {"id": item_id})
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=raw, mime="application/json", fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        from urllib.parse import quote

        if ":" not in item_id:
            return f"https://app.getdex.com/contacts/{quote(item_id, safe='')}"
        kind, raw_id = item_id.split(":", 1)
        path = {
            "contact": "contacts",
            "organisation": "organisations",
            "relationship": "relationships",
        }.get(kind, "contacts")
        return f"https://app.getdex.com/{path}/{quote(raw_id, safe='')}"

    def sensitivity_for(self, item_id: str) -> Any:
        del item_id
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Return the max ``modified_at`` across seeded events (ISO timestamp cursor)."""
        if not self._events:
            return None
        return max(ev.modified_at for ev in self._events)

    def metadata_for(self, item_id: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only).

        ADR-021 (Wave E.5): the contract-test fake exists to prove
        Protocol-shape compliance, not behaviour. Real Dex envelope
        metadata extraction lives on
        :meth:`kairix.connectors.dex_crm.DexCrmConnector.metadata_for`.
        """
        del item_id
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


# ---------------------------------------------------------------------------
# Connector-pipeline orchestration fakes (Wave 2 — IM-2)
#
# These satisfy the boundary Protocols the ConnectorPipeline composes around
# real Bronze / Silver / Cursor / DeadLetter stores. The Source and Extractor
# fakes script behaviour (events + per-item failure injection) so integration
# tests can prove the per-batch transaction + dead-letter contract.
# ---------------------------------------------------------------------------


class FakeSourceConnector:
    """Scripted :class:`kairix.core.protocols.SourceConnector`.

    Constructor takes events to emit and an optional ``fail_on_fetch``
    set — item_ids in that set raise ``RuntimeError`` from ``fetch``.
    ``content`` maps item_id to the raw bytes ``fetch`` returns; absent
    entries return empty bytes. ``mime_overrides`` maps item_id to the
    MIME ``fetch`` reports; absent entries fall back to the legacy
    ``.md → text/markdown, else text/plain`` rule.

    Used by ``tests/integration/test_connector_pipeline.py`` to drive
    the per-batch orchestration through the real Bronze + Silver + Cursor
    + DeadLetter surfaces.

    The fake's :meth:`next_cursor` returns the configurable
    ``cursor_token`` so integration tests can assert the orchestrator
    persisted the connector-supplied token (not the per-item
    ``modified_at``). Pass ``track_modified_at=True`` to simulate the
    Obsidian/Dex-style "max modified_at observed in last drain"
    behaviour; pass ``cursor_token=...`` for the SharePoint/Graph-style
    "opaque token unrelated to modified_at" shape.
    """

    def __init__(
        self,
        *,
        name: str = "fake-source",
        events: list[Any] | None = None,
        content: dict[str, bytes] | None = None,
        fail_on_fetch: set[str] | None = None,
        timeout_on_fetch: set[str] | None = None,
        raise_on_list_changes: Exception | None = None,
        raise_on_source_link: set[str] | None = None,
        raise_on_sensitivity_for: set[str] | None = None,
        raise_on_next_cursor: Exception | None = None,
        raise_on_metadata_for: set[str] | None = None,
        sensitivity: str = "internal",
        cursor_token: str | None = None,
        track_modified_at: bool = False,
        per_tick_max_items: int = 500,
        disk_watermark_min_free_bytes: int | None = None,
        metadata: dict[str, Any] | None = None,
        mime_overrides: dict[str, str] | None = None,
    ) -> None:
        from kairix.core.protocols import ChangeEvent  # local import — avoids reordering top-of-file

        self.name = name
        # F66 ceilings — declared on the instance so the SourceConnector
        # Protocol attribute lookup works at the call site in
        # ``ConnectorPipeline.run_batch``. Defaults match the Protocol
        # defaults (500 items, no watermark) so existing tests are
        # unaffected; tests exercising budget / watermark gating set
        # these explicitly.
        self.per_tick_max_items = per_tick_max_items
        self.disk_watermark_min_free_bytes = disk_watermark_min_free_bytes
        self._events: list[ChangeEvent] = list(events) if events is not None else []
        self._content: dict[str, bytes] = dict(content) if content is not None else {}
        self._fail_on_fetch: set[str] = set(fail_on_fetch) if fail_on_fetch is not None else set()
        # F68 (ADR-024 Bundle A) — per-method failure-injection knobs.
        # Each knob targets one Protocol method's failure surface so
        # contract tests can drive the failure-mode behaviour
        # (raises / times_out / unauthorized / unavailable) explicitly
        # without monkeypatching kairix internals.
        self._timeout_on_fetch: set[str] = set(timeout_on_fetch) if timeout_on_fetch is not None else set()
        self._raise_on_list_changes: Exception | None = raise_on_list_changes
        self._raise_on_source_link: set[str] = set(raise_on_source_link) if raise_on_source_link is not None else set()
        self._raise_on_sensitivity_for: set[str] = (
            set(raise_on_sensitivity_for) if raise_on_sensitivity_for is not None else set()
        )
        self._raise_on_next_cursor: Exception | None = raise_on_next_cursor
        self._raise_on_metadata_for: set[str] = (
            set(raise_on_metadata_for) if raise_on_metadata_for is not None else set()
        )
        self._sensitivity = sensitivity
        self.fetch_calls: list[str] = []
        # ADR-021 (Wave E.5): ``metadata`` maps item_id -> SourceMetadata.
        # Missing entries return an empty SourceMetadata so tests that
        # don't care about envelope metadata stay terse.
        self._metadata: dict[str, Any] = dict(metadata) if metadata is not None else {}
        # PR-2 (compat gate): per-item MIME override keyed by item_id.
        # Absent entries fall back to the legacy ``.md → text/markdown,
        # else text/plain`` rule, so existing tests are unaffected. Set
        # an override to drive the MIME-driven skip path (e.g. an
        # ``application/msword`` item with no recognizable magic bytes).
        self._mime_overrides: dict[str, str] = dict(mime_overrides) if mime_overrides is not None else {}
        # next_cursor() shapes:
        #   - cursor_token=<str>: returned verbatim (opaque-token shape;
        #     mirrors SharePoint/Graph/Slack deltaLink behaviour).
        #   - track_modified_at=True: returns max ``modified_at`` seen on
        #     the last list_changes drain (Obsidian/Dex shape).
        #   - both None: returns None (simulates "no cursor advance").
        self._cursor_token = cursor_token
        self._track_modified_at = track_modified_at
        self._last_max_modified_at: str | None = None
        # list_changes_calls captures the cursor argument each call
        # received so tests can assert the orchestrator passed the
        # stored cursor (not None) on the second tick.
        self.list_changes_calls: list[Any] = []

    def list_changes(self, cursor: Any | None = None) -> Any:
        self.list_changes_calls.append(cursor)
        if self._raise_on_list_changes is not None:
            raise self._raise_on_list_changes
        if self._track_modified_at and self._events:
            self._last_max_modified_at = max(ev.modified_at for ev in self._events)
        return iter(self._events)

    def fetch(self, item_id: str) -> Any:
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        self.fetch_calls.append(item_id)
        if item_id in self._timeout_on_fetch:
            # F68 ``times_out`` failure class — TimeoutError mirrors the
            # asyncio / socket timeout shape the real HTTP-bound
            # connectors raise.
            raise TimeoutError(f"fake-source: simulated fetch timeout for {item_id!r}")
        if item_id in self._fail_on_fetch:
            raise RuntimeError(f"fake-source: simulated fetch failure for {item_id!r}")
        raw = self._content.get(item_id, b"")
        mime = self._mime_overrides.get(item_id)
        if mime is None:
            mime = "text/markdown" if item_id.endswith(".md") else "text/plain"
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=raw, mime=mime, fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        if item_id in self._raise_on_source_link:
            raise RuntimeError(f"fake-source: simulated source_link failure for {item_id!r}")
        return f"{self.name}://item/{item_id}"

    def sensitivity_for(self, item_id: str) -> Any:
        if item_id in self._raise_on_sensitivity_for:
            raise RuntimeError(f"fake-source: simulated sensitivity_for failure for {item_id!r}")
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Return the cursor token the orchestrator should persist.

        Three shapes (mirrors the real-connector taxonomy):

          * If ``cursor_token`` was supplied at construction, return
            it verbatim — simulates SharePoint deltaLink / Graph
            ``@odata.deltaLink`` opaque-token cursors.
          * If ``track_modified_at=True``, return the max
            ``modified_at`` observed in the last :meth:`list_changes`
            drain — simulates Obsidian / Dex CRM ISO timestamp cursors.
          * Otherwise return ``None`` — simulates "no cursor advance
            this tick" so tests can assert the orchestrator does NOT
            clobber a prior cursor with None.
        """
        if self._raise_on_next_cursor is not None:
            raise self._raise_on_next_cursor
        if self._cursor_token is not None:
            return self._cursor_token
        if self._track_modified_at:
            return self._last_max_modified_at
        return None

    def metadata_for(self, item_id: str) -> Any:
        """Return the scripted :class:`SourceMetadata` for ``item_id``.

        ADR-021 (Wave E.5): test fixtures pass a ``metadata`` mapping
        keyed by ``item_id`` at construction time; absent keys collapse
        to an empty :class:`SourceMetadata` so tests that don't care
        about envelope metadata stay terse.
        """
        from kairix.core.protocols import SourceMetadata

        if item_id in self._raise_on_metadata_for:
            raise RuntimeError(f"fake-source: simulated metadata_for failure for {item_id!r}")
        value = self._metadata.get(item_id)
        if isinstance(value, SourceMetadata):
            return value
        return SourceMetadata()


class FakeM365EmailHeadersConnector:
    """Scripted :class:`kairix.core.protocols.SourceConnector` for the
    M365 email-headers plugin's contract test.

    Constructor takes the header envelopes to emit; the fake satisfies
    the full Protocol surface without touching the network, OAuth2
    helper, or any real Graph endpoint. This is the canonical fake F43
    pairs with the real
    :class:`kairix.connectors.m365_email_headers.M365EmailHeadersConnector`
    inside ``tests/contracts/test_m365_email_headers_protocol.py``.

    Locked sensitivity tier per ADR-004 + ADR-005: every event /
    fetch reports the ``personal`` tier; the constructor does NOT
    accept a sensitivity override so the fake structurally mirrors
    the real-connector's locked-tier behaviour.
    """

    name: str = "m365_email_headers"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = None

    def __init__(
        self,
        *,
        user_principal_name: str = "agent-alpha@example.com",
        envelopes: list[dict[str, Any]] | None = None,
    ) -> None:
        self._upn = user_principal_name
        self._envelopes: list[dict[str, Any]] = list(envelopes) if envelopes is not None else []

    def list_changes(self, cursor: Any | None = None) -> Any:
        """Yield one ``created`` ChangeEvent per seeded envelope.

        Matches the real connector's positional+keyword acceptance for
        the ``cursor`` parameter — the SourceConnector Protocol passes
        it positionally; contract tests pass it as a keyword.
        """
        _ = cursor  # cursor is ignored by the fake; events are scripted
        from kairix.core.protocols import ChangeEvent

        events: list[ChangeEvent] = []
        for env in self._envelopes:
            events.append(
                ChangeEvent(
                    op="created",
                    item_id=str(env.get("id", "")),
                    modified_at=str(env.get("receivedDateTime", "1970-01-01T00:00:00Z")),
                    metadata={"sensitivity": "personal"},
                )
            )
        return iter(events)

    def fetch(self, item_id: str) -> Any:
        import json as _json
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        match = next((env for env in self._envelopes if env.get("id") == item_id), None)
        payload = _json.dumps(match if match is not None else {"id": item_id}, sort_keys=True).encode("utf-8")
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=payload, mime="application/json", fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        from urllib.parse import quote

        return f"https://outlook.office.com/mail/inbox/id/{quote(item_id, safe='')}"

    def sensitivity_for(self, _item_id: str) -> Any:
        return "personal"

    def next_cursor(self) -> str | None:
        """Return a synthetic Graph-style deltaLink for the most recent drain."""
        if not self._envelopes:
            return None
        return f"https://graph.microsoft.com/v1.0/users/{self._upn}/messages/delta?token=fake-token"

    def metadata_for(self, item_id: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only).

        ADR-021 (Wave E.5): real envelope extraction lives on the
        shipped :class:`M365EmailHeadersConnector`.
        """
        del item_id
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeM365CalendarConnector:
    """Scripted :class:`kairix.core.protocols.SourceConnector` for the
    M365 calendar plugin (Wave 5 KP-3).
    """

    name: str = "m365_calendar"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = 5 * 1024**3

    def __init__(
        self,
        *,
        events: list[Any] | None = None,
        content: dict[str, bytes] | None = None,
        sensitivity: str = "internal",
        delta_link: str | None = None,
    ) -> None:
        from kairix.core.protocols import ChangeEvent

        self._events: list[ChangeEvent] = list(events) if events is not None else []
        self._content: dict[str, bytes] = dict(content) if content is not None else {}
        self._sensitivity = sensitivity
        self.last_delta_link = delta_link

    def list_changes(self, cursor: Any | None) -> Any:
        del cursor
        return iter(self._events)

    def fetch(self, item_id: str) -> Any:
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        raw = self._content.get(item_id, b"{}")
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=raw, mime="application/json", fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        return f"https://outlook.office.com/calendar/item/{item_id}"

    def sensitivity_for(self, _item_id: str) -> Any:
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Return the seeded deltaLink (Graph opaque-token cursor shape)."""
        return self.last_delta_link

    def metadata_for(self, item_id: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only).

        ADR-021 (Wave E.5): real Graph event envelope extraction
        lives on the shipped :class:`M365CalendarConnector`.
        """
        del item_id
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeAppleCalDavConnector:
    """Scripted :class:`kairix.core.protocols.SourceConnector` for the
    Apple iCloud CalDAV plugin's contract test.

    Constructor takes the calendar event payloads to emit; the fake
    satisfies the full Protocol surface without touching iCloud, the
    :mod:`caldav` library, or any network. This is the canonical fake
    F43 pairs with the real
    :class:`kairix.connectors.apple_caldav.AppleCalDavConnector`
    inside ``tests/contracts/test_apple_caldav_protocol.py``.

    Default sensitivity tier is ``personal`` per the dispatch brief —
    iCloud calendars are operator-personal data. The constructor
    accepts a ``sensitivity`` override so contract assertions can pin
    both the default and an alternative tier.
    """

    name: str = "apple_caldav"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = None

    def __init__(
        self,
        *,
        events: list[Any] | None = None,
        content: dict[str, bytes] | None = None,
        sensitivity: str = "personal",
        sync_token: str | None = None,
    ) -> None:
        from kairix.core.protocols import ChangeEvent

        self._events: list[ChangeEvent] = list(events) if events is not None else []
        self._content: dict[str, bytes] = dict(content) if content is not None else {}
        self._sensitivity = sensitivity
        self._sync_token = sync_token

    def list_changes(self, cursor: Any | None) -> Any:
        del cursor
        return iter(self._events)

    def fetch(self, item_id: str) -> Any:
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        raw = self._content.get(item_id, b"BEGIN:VCALENDAR\nEND:VCALENDAR\n")
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=raw, mime="text/calendar", fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        return f"caldav://{item_id}"

    def sensitivity_for(self, _item_id: str) -> Any:
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Return the seeded sync token (CalDAV opaque-token cursor shape)."""
        return self._sync_token

    def metadata_for(self, item_id: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only).

        Real CalDAV event envelope extraction (ORGANIZER / DTSTART /
        RRULE / ATTENDEE / LOCATION) lives on the shipped
        :class:`AppleCalDavConnector`.
        """
        del item_id
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeGoogleCalendarConnector:
    """Scripted :class:`kairix.core.protocols.SourceConnector` for the
    Google Calendar plugin's contract test (F43).

    Mirrors :class:`FakeM365CalendarConnector` shape — pinning the
    same Protocol surface keeps the Wave-E connector fleet's contract
    coverage uniform. The real plugin
    (:class:`kairix.connectors.google_calendar.GoogleCalendarConnector`)
    persists Google's ``nextSyncToken`` as the cursor; the fake
    accepts a ``sync_token`` constructor argument the test can pin and
    assert against.
    """

    name: str = "google_calendar"
    per_tick_max_items: int = 500
    # Calendar events are small structured envelopes — no large disk
    # writes, so the watermark is unset (F66-watermark-exempt mirror).
    disk_watermark_min_free_bytes: int | None = None

    def __init__(
        self,
        *,
        events: list[Any] | None = None,
        content: dict[str, bytes] | None = None,
        sensitivity: str = "internal",
        sync_token: str | None = None,
    ) -> None:
        from kairix.core.protocols import ChangeEvent

        self._events: list[ChangeEvent] = list(events) if events is not None else []
        self._content: dict[str, bytes] = dict(content) if content is not None else {}
        self._sensitivity = sensitivity
        self._sync_token = sync_token

    def list_changes(self, cursor: Any | None) -> Any:
        del cursor
        return iter(self._events)

    def fetch(self, item_id: str) -> Any:
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        raw = self._content.get(item_id, b"")
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=raw, mime="text/calendar", fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        return f"https://calendar.google.com/calendar/u/0/r/eventedit/{item_id}"

    def sensitivity_for(self, _item_id: str) -> Any:
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Return the seeded ``nextSyncToken`` (Google opaque-cursor shape)."""
        return self._sync_token

    def metadata_for(self, item_id: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only).

        ADR-021 (Wave E.5): real Google event envelope extraction lives
        on the shipped :class:`GoogleCalendarConnector`.
        """
        del item_id
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeSharePointConnector:
    """Scripted :class:`kairix.core.protocols.SourceConnector` for the
    SharePoint plugin's contract test.

    Constructor takes the drive-item envelopes to emit; the fake
    satisfies the full Protocol surface without touching the Microsoft
    Graph network. This is the canonical fake F43 pairs with the real
    :class:`kairix.connectors.sharepoint.SharePointConnector` inside
    ``tests/contracts/test_sharepoint_protocol.py``.

    Default sensitivity tier is ``internal`` per the connector's
    documented default — the constructor accepts a ``sensitivity``
    override so contract assertions can pin both the default and a
    confidential-tier configuration.
    """

    name: str = "sharepoint"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = 5 * 1024**3

    def __init__(
        self,
        *,
        items: list[dict[str, Any]] | None = None,
        sensitivity: str = "internal",
        delta_link: str | None = None,
    ) -> None:
        self._items: list[dict[str, Any]] = list(items) if items is not None else []
        self._sensitivity = sensitivity
        self._delta_link = delta_link
        self._by_id: dict[str, dict[str, Any]] = {str(item.get("id")): item for item in self._items}

    def list_changes(self, cursor: Any | None = None) -> Any:
        """Yield one ``created`` ChangeEvent per seeded item."""
        _ = cursor
        from kairix.core.protocols import ChangeEvent

        events: list[ChangeEvent] = []
        for item in self._items:
            events.append(
                ChangeEvent(
                    op="created",
                    item_id=str(item.get("id", "")),
                    modified_at=str(item.get("lastModifiedDateTime", "1970-01-01T00:00:00Z")),
                    metadata={
                        "sensitivity": self._sensitivity,
                        "drive_id": str(item.get("driveId", "fake-drive")),
                        "name": str(item.get("name", "")),
                        "mime": str(item.get("mimeType", "")),
                    },
                )
            )
        return iter(events)

    def fetch(self, item_id: str) -> Any:
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        item = self._by_id.get(item_id, {})
        raw = item.get("_content", b"")
        if not isinstance(raw, bytes):
            raw = bytes(raw)
        mime_raw = item.get("mimeType", "application/octet-stream")
        mime = mime_raw if isinstance(mime_raw, str) else "application/octet-stream"
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=raw, mime=mime, fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        item = self._by_id.get(item_id, {})
        url = item.get("webUrl")
        if isinstance(url, str) and url:
            return url
        return f"sharepoint://items/{item_id}"

    def sensitivity_for(self, _item_id: str) -> Any:
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Round-trip the seeded delta link as a JSON cursor map."""
        if self._delta_link is None:
            return None
        import json as _json

        return _json.dumps({"fake-drive": self._delta_link}, sort_keys=True)

    def metadata_for(self, item_id: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only).

        ADR-021 (Wave E.5): real envelope extraction lives on the
        shipped :class:`SharePointConnector`.
        """
        del item_id
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeGoogleDriveConnector:
    """Scripted :class:`kairix.core.protocols.SourceConnector` for the
    Google Drive plugin's contract test.

    Constructor takes the file envelopes to emit; the fake satisfies
    the full Protocol surface without touching the Google Drive REST
    API. This is the canonical fake F43 pairs with the real
    :class:`kairix.connectors.google_drive.GoogleDriveConnector`
    inside ``tests/contracts/test_google_drive_protocol.py``.

    Default sensitivity tier is ``internal`` per the connector's
    documented default — the constructor accepts a ``sensitivity``
    override so contract assertions can pin both the default and a
    confidential-tier configuration.
    """

    name: str = "google_drive"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = 5_000_000_000

    def __init__(
        self,
        *,
        files: list[dict[str, Any]] | None = None,
        sensitivity: str = "internal",
        new_start_page_token: str | None = None,
    ) -> None:
        self._files: list[dict[str, Any]] = list(files) if files is not None else []
        self._sensitivity = sensitivity
        self._new_start_page_token = new_start_page_token
        self._by_id: dict[str, dict[str, Any]] = {str(item.get("id")): item for item in self._files}

    def list_changes(self, cursor: Any | None = None) -> Any:
        """Yield one ``created`` ChangeEvent per seeded file."""
        _ = cursor
        from kairix.core.protocols import ChangeEvent

        events: list[ChangeEvent] = []
        for entry in self._files:
            events.append(
                ChangeEvent(
                    op="created",
                    item_id=str(entry.get("id", "")),
                    modified_at=str(entry.get("modifiedTime", "1970-01-01T00:00:00Z")),
                    metadata={
                        "sensitivity": self._sensitivity,
                        "corpus_id": str(entry.get("corpus_id", "fake-corpus")),
                        "name": str(entry.get("name", "")),
                        "mime": str(entry.get("mimeType", "")),
                    },
                )
            )
        return iter(events)

    def fetch(self, item_id: str) -> Any:
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        entry = self._by_id.get(item_id, {})
        raw = entry.get("_content", b"")
        if not isinstance(raw, bytes):
            raw = bytes(raw)
        mime_raw = entry.get("mimeType", "application/octet-stream")
        mime = mime_raw if isinstance(mime_raw, str) else "application/octet-stream"
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=raw, mime=mime, fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        entry = self._by_id.get(item_id, {})
        url = entry.get("webViewLink")
        if isinstance(url, str) and url:
            return url
        return f"gdrive://files/{item_id}"

    def sensitivity_for(self, _item_id: str) -> Any:
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Round-trip the seeded newStartPageToken."""
        return self._new_start_page_token

    def metadata_for(self, item_id: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only).

        ADR-021 (Wave E.5): real envelope extraction lives on the
        shipped :class:`GoogleDriveConnector`.
        """
        del item_id
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeNotionConnector:
    """Scripted :class:`kairix.core.protocols.SourceConnector` for the
    Notion plugin's contract test.

    Constructor takes the Notion page envelopes to emit; the fake
    satisfies the full Protocol surface without touching the Notion
    REST API. This is the canonical fake F43 pairs with the real
    :class:`kairix.connectors.notion.NotionConnector` inside
    ``tests/contracts/test_notion_protocol.py``.

    Default sensitivity tier is ``internal`` per the connector's
    documented default — the constructor accepts a ``sensitivity``
    override so contract assertions can pin both the default and a
    confidential-tier configuration.
    """

    name: str = "notion"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = None

    def __init__(
        self,
        *,
        pages: list[dict[str, Any]] | None = None,
        sensitivity: str = "internal",
    ) -> None:
        self._pages: list[dict[str, Any]] = list(pages) if pages is not None else []
        self._sensitivity = sensitivity
        self._by_id: dict[str, dict[str, Any]] = {str(page.get("id")): page for page in self._pages}

    def list_changes(self, cursor: Any | None = None) -> Any:
        """Yield one ``modified`` ChangeEvent per seeded page."""
        _ = cursor
        from kairix.core.protocols import ChangeEvent

        events: list[ChangeEvent] = []
        for page in self._pages:
            archived = bool(page.get("archived", False))
            op = "archived" if archived else "modified"
            events.append(
                ChangeEvent(
                    op=op,  # type: ignore[arg-type]  # F3-rationale: op is one of "archived"/"modified" from the fixed string set above; mypy doesn't narrow.
                    item_id=str(page.get("id", "")),
                    modified_at=str(page.get("last_edited_time", "1970-01-01T00:00:00Z")),
                    metadata={
                        "sensitivity": self._sensitivity,
                        "parent_type": str(page.get("parent_type", "workspace")),
                        "name": str(page.get("title", "")),
                        "mime": "text/markdown",
                    },
                )
            )
        return iter(events)

    def fetch(self, item_id: str) -> Any:
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        page = self._by_id.get(item_id, {})
        raw_body = page.get("body_markdown", "")
        if isinstance(raw_body, str):
            raw_bytes = raw_body.encode("utf-8")
        elif isinstance(raw_body, bytes):
            raw_bytes = raw_body
        else:
            raw_bytes = b""
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=raw_bytes, mime="text/markdown", fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        page = self._by_id.get(item_id, {})
        url = page.get("url")
        if isinstance(url, str) and url:
            return url
        return f"notion://pages/{item_id}"

    def sensitivity_for(self, _item_id: str) -> Any:
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Return the max ``last_edited_time`` across seeded pages (Notion cursor shape)."""
        if not self._pages:
            return None
        return max(str(p.get("last_edited_time", "")) for p in self._pages)

    def metadata_for(self, item_id: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only).

        ADR-021 (Wave E.5): real envelope extraction lives on the
        shipped :class:`NotionConnector`.
        """
        del item_id
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeLinearApiClient:
    """Scripted ``LinearApiClient`` stand-in for tests — no network.

    Implements the same ``query`` / ``paginate`` surface as
    :class:`kairix.connectors.linear.api_client.LinearApiClient` using
    in-memory scripted responses. Records every call so tests can assert
    what the connector sent.

    Args:
        pages: Mapping from connection name (``issues`` / ``projects`` /
            ``documents`` / ``initiatives`` / ``projectUpdates``) to a
            list of pages, where each page is a list of node dicts.
            ``paginate()`` serves pages in order and stops at the last
            page. Example::

                FakeLinearApiClient(
                    pages={
                        "issues": [
                            [{"id": "i-1"}, {"id": "i-2"}],   # page 1
                            [{"id": "i-3"}],                   # page 2 (last)
                        ]
                    }
                )

        raise_429_times: Make ``query()`` raise
            :class:`httpx.HTTPStatusError` (429) this many times before
            succeeding. Use to exercise retry logic in callers without
            a real HTTP client.
    """

    def __init__(
        self,
        *,
        pages: Mapping[str, list[list[dict[str, Any]]]] | None = None,
        raise_429_times: int = 0,
        paginate_raises: BaseException | None = None,
    ) -> None:
        import copy

        self._pages: dict[str, list[list[dict[str, Any]]]] = {k: copy.deepcopy(v) for k, v in (pages or {}).items()}
        self._remaining_429s = raise_429_times
        # Failure-injection knob: ``paginate`` raises this (a GraphQL /
        # transport failure mid-poll) instead of yielding nodes.
        self._paginate_raises = paginate_raises
        self.query_calls: list[tuple[str, dict[str, Any]]] = []
        self.paginate_calls: list[tuple[str, dict[str, Any], str]] = []

    def query(self, document: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        """Return an empty data dict after consuming any scripted 429s."""
        self.query_calls.append((document, dict(variables)))
        if self._remaining_429s > 0:
            self._remaining_429s -= 1
            import httpx

            raise httpx.HTTPStatusError(
                "429 Too Many Requests",
                request=httpx.Request("POST", "https://api.linear.app/graphql"),
                response=httpx.Response(429),
            )
        return {}

    def paginate(
        self,
        document: str,
        variables: Mapping[str, Any],
        *,
        connection: str,
    ) -> Iterator[dict[str, Any]]:
        """Yield scripted nodes for ``connection`` filtered by ``updatedAt > since``.

        Mirrors the real Linear API contract (``filter: { updatedAt: { gt:
        $since } }``): nodes whose ``updatedAt`` is at or before the
        ``since`` variable are not returned, so the connector's per-entity
        watermark cursor is exercised faithfully across ticks. A node
        missing ``updatedAt`` is always yielded (the connector handles it).
        """
        self.paginate_calls.append((document, dict(variables), connection))
        if self._paginate_raises is not None:
            raise self._paginate_raises
        since = variables.get("since")
        for page in self._pages.get(connection, []):
            for node in page:
                updated_at = node.get("updatedAt")
                if isinstance(since, str) and isinstance(updated_at, str) and updated_at <= since:
                    continue
                yield node


class FakeLinearConnector:
    """Scripted Linear :class:`kairix.core.protocols.SourceConnector`.

    Constructor takes the per-kind node set the fake should emit; the
    fake satisfies the full MVP capability surface (base SourceConnector
    + PollConnector + CredentialsConnector + SlimConnector) without
    touching the Linear GraphQL network. Canonical fake F43 pairs with
    the real :class:`kairix.connectors.linear.LinearConnector` inside
    ``tests/contracts/test_linear_connector_contract.py``.

    Args:
        nodes: Mapping from entity kind (``issue`` / ``project`` /
            ``document`` / ``initiative`` / ``projectUpdate``) to a list
            of GraphQL-shaped node dicts. Issues key their id on
            ``identifier``; every other kind keys on ``id``.
        sensitivity: the F39 tier ``sensitivity_for`` returns
            (default ``internal`` to mirror the shipped connector).
    """

    name: str = "linear"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = None

    def __init__(
        self,
        *,
        nodes: Mapping[str, list[dict[str, Any]]] | None = None,
        sensitivity: str = "internal",
    ) -> None:
        self._sensitivity = sensitivity
        self._by_id: dict[str, tuple[str, dict[str, Any]]] = {}
        self._order: list[str] = []
        self._next_cursor: str | None = None
        for kind, kind_nodes in (nodes or {}).items():
            for node in kind_nodes:
                key = node.get("identifier") if kind == "issue" else node.get("id")
                if not key:
                    continue
                item_id = f"{kind}:{key}"
                self._by_id[item_id] = (kind, dict(node))
                self._order.append(item_id)

    def list_changes(self, cursor: Any | None = None) -> Any:
        """Yield one ``modified`` ChangeEvent per seeded node.

        Mirrors the real connector's per-entity-type watermark cursor: each
        kind advances its OWN ``updatedAt`` watermark, JSON-encoded into the
        opaque token by :meth:`next_cursor` (F43 behavioural parity with
        :class:`kairix.connectors.linear.LinearConnector`).
        """
        import json

        del cursor
        from kairix.core.protocols import ChangeEvent

        events: list[ChangeEvent] = []
        watermarks: dict[str, str] = {}
        for item_id in self._order:
            kind, node = self._by_id[item_id]
            modified_at = str(node.get("updatedAt", "1970-01-01T00:00:00.000Z"))
            events.append(
                ChangeEvent(
                    op="modified",
                    item_id=item_id,
                    modified_at=modified_at,
                    metadata={
                        "sensitivity": self._sensitivity,
                        "kind": kind,
                        "mime": "text/markdown",
                    },
                )
            )
            prior = watermarks.get(kind)
            if prior is None or modified_at > prior:
                watermarks[kind] = modified_at
        self._next_cursor = json.dumps(watermarks, sort_keys=True) if watermarks else None
        return iter(events)

    def fetch(self, item_id: str) -> Any:
        from datetime import datetime, timezone

        from kairix.connectors.linear.render import render
        from kairix.core.protocols import RawArtefact

        kind, node = self._by_id.get(item_id, ("issue", {}))
        markdown = render(kind, node)
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(
            raw=markdown.encode("utf-8"),
            mime="text/markdown",
            fetched_at=fetched_at,
            sensitivity_hint=self._sensitivity,  # type: ignore[arg-type]  # F3-rationale: fixture passes a valid Sensitivity literal.
        )

    def source_link(self, item_id: str) -> str:
        entry = self._by_id.get(item_id)
        if entry is not None:
            url = entry[1].get("url")
            if isinstance(url, str) and url:
                return url
        return f"linear://{item_id}"

    def sensitivity_for(self, _item_id: str) -> Any:
        return self._sensitivity

    def next_cursor(self) -> str | None:
        return self._next_cursor

    def metadata_for(self, item_id: str) -> Any:
        """Surface author + dates + labels from the seeded node (F65)."""
        from kairix.core.protocols import SourceMetadata

        entry = self._by_id.get(item_id)
        if entry is None:
            return SourceMetadata()
        node = entry[1]
        author = None
        author_email = None
        for key in ("creator", "lead", "user", "assignee"):
            person = node.get(key)
            if isinstance(person, dict):
                author = person.get("displayName")
                author_email = person.get("email")
                break
        labels_block = node.get("labels")
        tags: tuple[str, ...] = ()
        if isinstance(labels_block, dict) and isinstance(labels_block.get("nodes"), list):
            tags = tuple(n["name"] for n in labels_block["nodes"] if isinstance(n, dict) and n.get("name"))
        return SourceMetadata(
            modified_at=node.get("updatedAt"),
            created_at=node.get("createdAt"),
            author=author,
            author_email=author_email,
            tags=tags,
            properties={"kind": entry[0]},
        )

    def list_changes_for_container(self, container: Any) -> Any:
        """PollConnector — delegate to the single-cursor list_changes."""
        return self.list_changes(getattr(container, "cursor_token", None))

    def retrieve_all_slim_docs(self, _container: Any) -> Any:
        """SlimConnector — yield every seeded item_id for the prune cycle."""
        yield from list(self._order)

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """CredentialsConnector — validate + normalise the raw credential."""
        raw = credentials.get("api_key") or credentials.get("token")
        if not isinstance(raw, str) or not raw.strip():
            return None
        return {"api_key": raw.strip()}


class FakeGitHubConnector:
    """Scripted GitHub :class:`kairix.core.protocols.SourceConnector`.

    Constructor takes the per-repo envelope set the fake should emit;
    the fake satisfies the full Wave-E capability surface (base +
    PollConnector + CheckpointedConnector + EventConnector +
    SlimConnector + SlimConnectorWithPermSync + Resolver +
    HierarchyConnector + OAuthConnector + CredentialsConnector)
    without touching the GitHub REST/GraphQL networks. Canonical fake
    F43 pairs with the real
    :class:`kairix.connectors.github.GitHubConnector` inside
    ``tests/contracts/test_github_protocol.py``.

    Default sensitivity tier is ``client-confidential`` per spec §1
    (private repos are the GitHub default); the constructor accepts a
    ``sensitivity`` override so contract assertions can pin both the
    default and a public-tier configuration.
    """

    name: str = "github"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = 5 * 1024**3

    def __init__(
        self,
        *,
        repos: list[dict[str, Any]] | None = None,
        sensitivity: str = "client-confidential",
        webhook_secret: str | None = "fake-secret",  # pragma: allowlist secret
    ) -> None:
        self._repos: list[dict[str, Any]] = list(repos) if repos is not None else []
        self._sensitivity = sensitivity
        self._webhook_secret = webhook_secret
        self._by_id: dict[str, dict[str, Any]] = {
            f"github://{r.get('full_name')}/commit/{r.get('sha', 'fake-sha')}": r for r in self._repos
        }
        self._seen_deliveries: set[str] = set()

    def list_changes(self, cursor: Any | None = None) -> Any:
        from kairix.core.protocols import ChangeEvent

        _ = cursor
        events: list[ChangeEvent] = []
        for repo in self._repos:
            full_name = str(repo.get("full_name", "fake/repo"))
            sha = str(repo.get("sha", "fake-sha"))
            events.append(
                ChangeEvent(
                    op="modified",
                    item_id=f"github://{full_name}/commit/{sha}",
                    modified_at=str(repo.get("committed_at", "2026-05-23T00:00:00Z")),
                    metadata={
                        "sensitivity": self._sensitivity,
                        "repo": full_name,
                        "kind": "commit",
                    },
                )
            )
        return iter(events)

    def fetch(self, item_id: str) -> Any:
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        item = self._by_id.get(item_id, {})
        raw = item.get("_content", b"")
        if not isinstance(raw, bytes):
            raw = bytes(str(raw).encode("utf-8"))
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(
            raw=raw,
            mime="application/json",
            fetched_at=fetched_at,
            sensitivity_hint=self._sensitivity,  # type: ignore[arg-type]  # F3 rationale: contract test pins Literal-compatible value
        )

    def source_link(self, item_id: str) -> str:
        # Round-trip github://owner/repo/commit/sha -> https URL.
        prefix = "github://"
        if item_id.startswith(prefix):
            rest = item_id[len(prefix) :]
            return f"https://github.com/{rest}"
        return f"https://github.com/{item_id}"

    def sensitivity_for(self, _item_id: str) -> Any:
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Fake GitHub connector cursor — returns the configurable test token or None."""
        return getattr(self, "_next_cursor_token", None)

    def load_from_checkpoint(self, _container: Any, _checkpoint: Any) -> Any:
        return self.list_changes(None)

    def iter_containers(self, cc_pair_id: int) -> Any:
        from kairix.core.protocols import Container

        seen: set[str] = set()
        for repo in self._repos:
            full_name = str(repo.get("full_name", "fake/repo"))
            if full_name in seen:
                continue
            seen.add(full_name)
            yield Container(
                cc_pair_id=cc_pair_id,
                container_id=full_name,
                access_state="ACCESSIBLE",
                cursor_token=None,
                last_synced_at=None,
            )

    def list_changes_for_container(self, container: Any) -> Any:
        from kairix.core.protocols import ChangeEvent

        events: list[ChangeEvent] = []
        for repo in self._repos:
            full_name = str(repo.get("full_name", "fake/repo"))
            if full_name != container.container_id:
                continue
            sha = str(repo.get("sha", "fake-sha"))
            events.append(
                ChangeEvent(
                    op="modified",
                    item_id=f"github://{full_name}/commit/{sha}",
                    modified_at=str(repo.get("committed_at", "2026-05-23T00:00:00Z")),
                    metadata={
                        "sensitivity": self._sensitivity,
                        "repo": full_name,
                    },
                )
            )
        return iter(events)

    def retrieve_all_slim_docs(self, container: Any) -> Any:
        for repo in self._repos:
            if repo.get("full_name") != container.container_id:
                continue
            sha = str(repo.get("sha", "fake-sha"))
            yield f"github://{container.container_id}/commit/{sha}"

    def retrieve_all_slim_docs_with_perms(self, container: Any) -> Any:
        import json as _json

        for item_id in self.retrieve_all_slim_docs(container):
            yield item_id, _json.dumps({"visibility": "private"})

    def reindex(self, failed_item_ids: tuple[str, ...], *, include_permissions: bool = False) -> Any:
        from datetime import datetime, timezone

        from kairix.core.protocols import ChangeEvent

        _ = include_permissions
        for item_id in failed_item_ids:
            yield ChangeEvent(
                op="modified",
                item_id=item_id,
                modified_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                metadata={"sensitivity": self._sensitivity, "reindex": True},
            )

    def load_hierarchy(self, cc_pair_id: int) -> Any:
        from kairix.core.protocols import HierarchyNode

        emitted: set[str] = set()
        # F58: orgs first, then repos.
        for repo in self._repos:
            full_name = str(repo.get("full_name", "fake/repo"))
            if "/" not in full_name:
                continue
            org = full_name.split("/", 1)[0]
            org_node_id = f"github://{org}"
            if org_node_id not in emitted:
                emitted.add(org_node_id)
                yield HierarchyNode(
                    cc_pair_id=cc_pair_id,
                    raw_node_id=org_node_id,
                    raw_parent_id=None,
                    display_name=org,
                    link=f"https://github.com/{org}",
                    node_type="FOLDER",
                    external_access_json=None,
                    sensitivity_hint=None,
                )
        for repo in self._repos:
            full_name = str(repo.get("full_name", "fake/repo"))
            if "/" not in full_name:
                continue
            org = full_name.split("/", 1)[0]
            org_node_id = f"github://{org}"
            repo_node_id = f"github://{full_name}"
            if repo_node_id in emitted:
                continue
            emitted.add(repo_node_id)
            yield HierarchyNode(
                cc_pair_id=cc_pair_id,
                raw_node_id=repo_node_id,
                raw_parent_id=org_node_id,
                display_name=full_name,
                link=f"https://github.com/{full_name}",
                node_type="FOLDER",
                external_access_json=None,
                sensitivity_hint=None,
            )

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        return credentials

    @classmethod
    def oauth_authorization_url(cls, state: str) -> str:
        return f"https://github.com/login/oauth/authorize?state={state}"

    @classmethod
    def oauth_code_to_token(cls, code: str) -> dict[str, Any]:
        return {"auth_kind": "github_user_oauth", "code": code}

    def subscribe(self, callback_url: str) -> str | None:
        return f"fake-sub-{callback_url}"

    def renew_subscription(self, subscription_id: str) -> str:
        return subscription_id

    def unsubscribe(self, _subscription_id: str) -> None:
        return None

    def handle_event(self, event: dict[str, Any]) -> Any:
        # Mimic real connector: verify signature + dedup deliveries.
        from kairix.connectors.github.webhook import translate_event, verify_and_parse

        body = event.get("body", b"")
        headers = event.get("headers", {})
        secret = event.get("webhook_secret") or self._webhook_secret or ""
        if not isinstance(body, bytes):
            body = str(body).encode("utf-8")
        envelope = verify_and_parse(
            body=body,
            headers=headers if isinstance(headers, dict) else {},
            webhook_secret=secret,
        )
        if envelope.delivery_id in self._seen_deliveries:
            return
        self._seen_deliveries.add(envelope.delivery_id)
        yield from translate_event(envelope)

    def metadata_for(self, item_id: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only).

        ADR-021 (Wave E.5): real envelope extraction lives on the
        shipped :class:`GitHubConnector`.
        """
        del item_id
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeSlackConnector:
    """Scripted :class:`kairix.core.protocols.SourceConnector` for the
    Slack plugin's contract test.

    Constructor takes the channel + message envelopes to emit; the
    fake satisfies the SourceConnector surface without touching the
    Slack Web API or Socket Mode WebSocket. This is the canonical fake
    F43 pairs with the real
    :class:`kairix.connectors.slack.SlackConnector` inside
    ``tests/contracts/test_slack_protocol.py``.

    Sensitivity tier defaults to the channel-kind-derived value per
    slack.md §1 (public_channel → internal, private_channel / mpim →
    client-confidential, im → personal) so the fake structurally
    mirrors the real-connector's F39 routing.
    """

    name: str = "slack"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = None

    def __init__(
        self,
        *,
        channels: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self._channels: list[dict[str, Any]] = list(channels) if channels is not None else []
        self._messages: list[dict[str, Any]] = list(messages) if messages is not None else []
        self._kind_by_channel: dict[str, str] = {
            str(c.get("id")): str(c.get("kind", "public_channel")) for c in self._channels
        }
        self._by_item_id: dict[str, dict[str, Any]] = {
            f"{m.get('channel_id', '')}:{m.get('ts', '')}": m for m in self._messages
        }

    def list_changes(self, cursor: Any | None = None) -> Any:
        """Yield one ``created`` ChangeEvent per seeded message."""
        _ = cursor
        from kairix.core.protocols import ChangeEvent

        events: list[ChangeEvent] = []
        for m in self._messages:
            channel_id = str(m.get("channel_id", ""))
            ts = str(m.get("ts", ""))
            events.append(
                ChangeEvent(
                    op="created",
                    item_id=f"{channel_id}:{ts}",
                    modified_at=str(m.get("modified_at", "2026-05-23T00:00:00Z")),
                    metadata={"channel_id": channel_id, "ts": ts, "user": m.get("user")},
                )
            )
        return iter(events)

    def fetch(self, item_id: str) -> Any:
        import json as _json
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        match = self._by_item_id.get(item_id, {})
        payload = _json.dumps(match, sort_keys=True).encode("utf-8")
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=payload, mime="application/json", fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        channel_id, _, ts = item_id.partition(":")
        return f"slack://channel/{channel_id}/p{ts.replace('.', '')}"

    def sensitivity_for(self, item_id: str) -> Any:
        channel_id, _, _ts = item_id.partition(":")
        kind = self._kind_by_channel.get(channel_id, "public_channel")
        return {
            "public_channel": "internal",
            "private_channel": "client-confidential",
            "mpim": "client-confidential",
            "im": "personal",
        }.get(kind, "personal")

    def next_cursor(self) -> str | None:
        """Fake Slack connector cursor — returns the configurable test token or None."""
        return getattr(self, "_next_cursor_token", None)

    def metadata_for(self, item_id: str) -> Any:
        """Return empty :class:`SourceMetadata` (Protocol-shape compliance only).

        ADR-021 (Wave E.5): real envelope extraction lives on the
        shipped :class:`SlackConnector`.
        """
        del item_id
        from kairix.core.protocols import SourceMetadata

        return SourceMetadata()


class FakeSocketModeTransport:
    """In-process :class:`kairix.connectors.slack.socket_mode.SocketModeTransport`.

    Drives :class:`SlackSocketModeHandler` without a WebSocket. Knobs:

    - ``open_script`` — per-``open()`` outcome, in order: ``None`` opens
      cleanly, an exception instance is raised. The LAST entry repeats
      once the script is exhausted (an empty script always opens), so a
      ``[None, ConnectionResetError(...)]`` script runs one session and
      then fails every reconnect — the handler's budget then trips it
      to POLL_ONLY instead of looping forever.
    - ``events`` — raw envelopes yielded by the FIRST ``iter_events``
      session only; later sessions yield nothing.
    - ``iter_raises`` — raised from ``iter_events`` after the scripted
      events (a mid-stream WebSocket drop).
    - ``ack_raises`` / ``close_raises`` — raised from ``ack`` / ``close``.

    Recorders: ``open_calls``, ``close_calls``, ``acked`` (envelope ids
    whose ack was attempted).
    """

    def __init__(
        self,
        *,
        open_script: tuple[BaseException | None, ...] = (),
        events: tuple[Mapping[str, Any], ...] = (),
        iter_raises: BaseException | None = None,
        ack_raises: BaseException | None = None,
        close_raises: BaseException | None = None,
    ) -> None:
        self._open_script = list(open_script)
        self._events = list(events)
        self._iter_raises = iter_raises
        self._ack_raises = ack_raises
        self._close_raises = close_raises
        self.open_calls: int = 0
        self.close_calls: int = 0
        self.acked: list[str] = []

    def open(self) -> None:
        if self._open_script:
            outcome = self._open_script[min(self.open_calls, len(self._open_script) - 1)]
        else:
            outcome = None
        self.open_calls += 1
        if outcome is not None:
            raise outcome

    def close(self) -> None:
        self.close_calls += 1
        if self._close_raises is not None:
            raise self._close_raises

    def iter_events(self) -> Iterator[Mapping[str, Any]]:
        events, self._events = self._events, []
        yield from events
        if self._iter_raises is not None:
            raise self._iter_raises

    def ack(self, envelope_id: str) -> None:
        self.acked.append(envelope_id)
        if self._ack_raises is not None:
            raise self._ack_raises


class FakeGmailClient:
    """Wire-level stand-in for :class:`kairix.connectors.gmail.GmailClient`.

    Lets contract tests drive the REAL :class:`GmailConnector` through
    its public ``client=`` seam with no Gmail roundtrip. Seed
    ``messages`` with :class:`~kairix.connectors.gmail.client.GmailMessage`
    rows; the History API surfaces every seeded id after any start
    historyId.

    F68 knob: ``history_raises`` — when set, ``iter_history_message_ids``
    raises it (the expired / invalid ``startHistoryId`` shape: Gmail
    answers HTTP 404 for a historyId older than ~7 days).
    """

    def __init__(
        self,
        *,
        messages: list[Any] | None = None,
        history_raises: BaseException | None = None,
        profile_history_id: str = "cold-start-tip",
    ) -> None:
        self._messages: list[Any] = list(messages or [])
        self._by_id: dict[str, Any] = {m.message_id: m for m in self._messages}
        self._history_raises = history_raises
        self._profile_history_id = profile_history_id
        self._last_history_id: str | None = None

    def get_profile_history_id(self) -> str:
        return self._profile_history_id

    def iter_history_message_ids(self, *, start_history_id: str) -> Any:
        del start_history_id
        if self._history_raises is not None:
            raise self._history_raises
        self._last_history_id = "final-history-id"
        return iter([m.message_id for m in self._messages])

    def last_history_id(self) -> str | None:
        return self._last_history_id

    def get_message(self, message_id: str) -> Any:
        return self._by_id[message_id]

    def stats(self) -> Any:
        from kairix.connectors.gmail.client import GmailStatsSnapshot

        return GmailStatsSnapshot(requests=0, rate_limited_403_total=0, token_refreshes=0)

    def invalidate_token(self) -> None:
        return None


class FakeGmailConnector:
    """Scripted :class:`kairix.core.protocols.SourceConnector` for the Gmail plugin.

    Constructor takes the message envelopes the fake should emit; the
    fake satisfies the SourceConnector + PollConnector + CheckpointedConnector
    surface without touching the Gmail REST API. Canonical fake F43
    pairs with the real :class:`kairix.connectors.gmail.GmailConnector`
    inside ``tests/contracts/test_gmail_protocol.py``.

    Default sensitivity tier is ``client-confidential`` per the Gmail
    spec brief (email is more sensitive than docs by default); the
    constructor accepts a ``sensitivity`` override so contract
    assertions can pin both the default and an override tier.

    Each message dict accepts these keys:
      ``id`` — message id (defaults to ``"fake-msg"``)
      ``thread_id`` — thread id (defaults to ``"fake-thread"``)
      ``from`` — From header value
      ``to`` — To header value (single addr or comma-separated)
      ``subject`` — Subject header value
      ``date`` — Date header value (ISO-8601 UTC)
      ``body`` — bytes / str body content (defaults to ``b"fake body"``)
    """

    name: str = "gmail"
    per_tick_max_items: int = 500
    disk_watermark_min_free_bytes: int | None = 5_000_000_000

    def __init__(
        self,
        *,
        user_email: str = "agent-alpha@example.com",
        messages: list[dict[str, Any]] | None = None,
        sensitivity: str = "client-confidential",
        profile_history_id: str = "fake-profile-tip",
        final_history_id: str = "fake-history-tip",
    ) -> None:
        self._user = user_email
        self._messages: list[dict[str, Any]] = list(messages) if messages is not None else []
        self._sensitivity = sensitivity
        self._by_id: dict[str, dict[str, Any]] = {
            str(m.get("id", f"fake-msg-{i}")): m for i, m in enumerate(self._messages)
        }
        # Mirrors the real GmailConnector cursor lifecycle: ``None`` until
        # the first tick, the live profile tip after a cold start
        # (``list_changes(None)``), the final historyId after a warm drain.
        self._profile_history_id = profile_history_id
        self._final_history_id = final_history_id
        self._next_cursor_token: str | None = None
        # Per-tick envelope cache — populated by a warm drain, read by
        # ``metadata_for`` (cache miss → empty SourceMetadata, as the real).
        self._cache: dict[str, dict[str, Any]] = {}
        self._next_cursor_by_container: dict[str, str | None] = {}

    def _drain_events(self, extra_metadata: dict[str, str] | None = None) -> list[Any]:
        from kairix.core.protocols import ChangeEvent

        events: list[ChangeEvent] = []
        for entry_id, entry in self._by_id.items():
            self._cache[entry_id] = entry
            metadata = {"sensitivity": self._sensitivity, **(extra_metadata or {})}
            events.append(
                ChangeEvent(
                    op="created",
                    item_id=entry_id,
                    modified_at=str(entry.get("date", "2026-05-28T10:00:00Z")),
                    metadata=metadata,
                )
            )
        return events

    def list_changes(self, cursor: Any | None = None) -> Any:
        if cursor is None:
            # Cold start — seed the cursor at the live tip, emit nothing.
            self._next_cursor_token = self._profile_history_id
            return iter([])
        events = self._drain_events()
        self._next_cursor_token = self._final_history_id
        return iter(events)

    def fetch(self, item_id: str) -> Any:
        from datetime import datetime, timezone

        from kairix.core.protocols import RawArtefact

        entry = self._by_id.get(item_id, {})
        body = entry.get("body", b"fake body")
        if not isinstance(body, bytes):
            body = str(body).encode("utf-8")
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return RawArtefact(raw=body, mime="text/plain", fetched_at=fetched_at)

    def source_link(self, item_id: str) -> str:
        from urllib.parse import quote

        return f"https://mail.google.com/mail/u/0/#inbox/{quote(item_id, safe='')}"

    def sensitivity_for(self, _item_id: str) -> Any:
        return self._sensitivity

    def next_cursor(self) -> str | None:
        """Fake Gmail connector cursor — returns the configurable test token or None."""
        return self._next_cursor_token

    def metadata_for(self, item_id: str) -> Any:
        from kairix.core.protocols import SourceMetadata

        entry = self._cache.get(item_id, {})
        if not entry:
            return SourceMetadata()
        from_addr = entry.get("from")
        to_value = entry.get("to", "")
        to_addrs = tuple(addr.strip() for addr in str(to_value).split(",") if addr.strip())
        properties: dict[str, str] = {}
        if entry.get("subject"):
            properties["subject"] = str(entry["subject"])
        thread_id = entry.get("thread_id")
        if thread_id:
            properties["thread_id"] = str(thread_id)
        return SourceMetadata(
            modified_at=str(entry.get("date")) if entry.get("date") else None,
            created_at=str(entry.get("date")) if entry.get("date") else None,
            author=str(from_addr) if from_addr else None,
            author_email=str(from_addr) if from_addr and "@" in str(from_addr) else None,
            tags=to_addrs,
            properties=properties,
        )

    def load_from_checkpoint(self, _container: Any, checkpoint: Any) -> Any:
        return self.list_changes(checkpoint)

    def iter_containers(self, cc_pair_id: int) -> Any:
        from kairix.core.protocols import Container

        yield Container(
            cc_pair_id=cc_pair_id,
            container_id=self._user,
            access_state="ACCESSIBLE",
            cursor_token=None,
            last_synced_at=None,
        )

    def list_changes_for_container(self, container: Any) -> Any:
        mailbox = container.container_id
        if container.cursor_token is None:
            self._next_cursor_by_container[mailbox] = self._profile_history_id
            return iter([])
        events = self._drain_events({"mailbox": mailbox})
        self._next_cursor_by_container[mailbox] = self._final_history_id
        return iter(events)

    def next_cursor_for_container(self, container_id: str) -> str | None:
        return self._next_cursor_by_container.get(container_id)

    def load_hierarchy(self, cc_pair_id: int) -> Any:
        from kairix.core.protocols import HierarchyNode

        yield HierarchyNode(
            cc_pair_id=cc_pair_id,
            raw_node_id="gmail",
            raw_parent_id=None,
            display_name=f"Gmail ({self._user})",
            link="https://mail.google.com/mail/u/0/",
            node_type="FOLDER",
            external_access_json=None,
            sensitivity_hint=None,
        )


class FakeExtractor:
    """Capture-only :class:`kairix.core.protocols.Extractor`.

    Decodes bytes as UTF-8 and returns an ``ExtractedDocument`` carrying
    that text as ``markdown``. ``quality_ok`` returns True whenever the
    decoded text is non-empty. Used by the connector-pipeline
    integration tests where the chunk content matters but the format
    detail does not.
    """

    name: str = "fake-extractor"
    version: str = "0.0.0"

    def __init__(
        self,
        *,
        metadata: Any | None = None,
        raise_on_can_extract: Exception | None = None,
        raise_on_extract: Exception | None = None,
        raise_on_quality_ok: Exception | None = None,
        raise_on_metadata_for: Exception | None = None,
        quality_ok_returns: bool | None = None,
        claimed_mime_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        from kairix.core.protocols import DocMetadata, ExtractedDocument

        # ``claimed_mime_prefixes`` — when set, ``can_extract`` claims only
        # mimes starting with one of these prefixes (e.g. ``("text/",)``,
        # matching the shipped PassthroughExtractor). ``None`` keeps the
        # historical claim-everything default.
        self._claimed_mime_prefixes = claimed_mime_prefixes

        self._DocMetadata = DocMetadata
        self._ExtractedDocument = ExtractedDocument
        self.extract_calls: list[tuple[bytes, str]] = []
        # ADR-021 (Wave E.5): scripted SourceMetadata override. ``None``
        # collapses to an empty SourceMetadata at metadata_for() call
        # time so tests that don't care stay terse.
        self._metadata_override = metadata
        # F68 (ADR-024 Bundle A) — per-method failure-injection knobs.
        # Each ``raise_on_*`` accepts an exception instance; when set,
        # the next call to that method raises it. ``quality_ok_returns``
        # overrides the default truthy-on-non-empty-markdown behaviour
        # so tests can drive the ``returns_empty`` / partial-output
        # branches without contorting the input.
        self._raise_on_can_extract = raise_on_can_extract
        self._raise_on_extract = raise_on_extract
        self._raise_on_quality_ok = raise_on_quality_ok
        self._raise_on_metadata_for = raise_on_metadata_for
        self._quality_ok_returns = quality_ok_returns

    def can_extract(self, mime: str, magic_bytes: bytes) -> bool:
        del magic_bytes
        if self._raise_on_can_extract is not None:
            raise self._raise_on_can_extract
        # Like the shipped extractors, the mime hint is consulted as a
        # string — a malformed (non-str) hint raises rather than being
        # silently claimed.
        normalised = mime.lower()
        if self._claimed_mime_prefixes is None:
            return True
        return normalised.startswith(self._claimed_mime_prefixes)

    def extract(self, raw: bytes, mime: str) -> Any:
        self.extract_calls.append((raw, mime))
        if self._raise_on_extract is not None:
            raise self._raise_on_extract
        text = raw.decode("utf-8", errors="replace")
        return self._ExtractedDocument(
            markdown=text,
            pages=(),
            images=(),
            metadata=self._DocMetadata(
                title=None,
                author=None,
                created_date=None,
                language=None,
                page_count=None,
            ),
            confidence=1.0,
        )

    def quality_ok(self, doc: Any) -> bool:
        if self._raise_on_quality_ok is not None:
            raise self._raise_on_quality_ok
        if self._quality_ok_returns is not None:
            return self._quality_ok_returns
        return bool(doc.markdown.strip())

    def metadata_for(self, raw: bytes, mime: str) -> Any:
        """Return the scripted body-derived :class:`SourceMetadata`.

        ADR-021 (Wave E.5): tests pass a ``metadata=SourceMetadata(...)``
        override at construction; the fake returns it verbatim. Missing
        override collapses to empty :class:`SourceMetadata` (the
        default for formats with no body metadata, e.g. plaintext).
        """
        del raw, mime
        from kairix.core.protocols import SourceMetadata

        if self._raise_on_metadata_for is not None:
            raise self._raise_on_metadata_for
        if isinstance(self._metadata_override, SourceMetadata):
            return self._metadata_override
        return SourceMetadata()


class FakeEntityGraphSink:
    """Capture-only :class:`kairix.core.protocols.EntityGraphSink`.

    Records every staged batch in ``staged`` (a list of tuples). Used
    by the connector-pipeline integration tests to assert that Silver
    output reaches the sink. Returns the count of signals staged.

    F68 (ADR-024 Bundle A) knobs:

      * ``raise_on_stage`` — when set, every call to :meth:`stage`
        raises this exception. The connector pipeline's
        ``_process_item`` does NOT wrap the sink call in a try/except,
        so the exception propagates and the per-chunk transaction
        rolls back (canonical ``raises`` failure mode).
      * ``available`` — models the DOWNSTREAM delivery target (Curator
        drain → Neo4j) being unreachable. Faithful to the production
        ``_SqliteEntityGraphSink`` (PLA-472): staging is durable and
        decoupled from delivery, so the batch is STILL staged and its
        count returned — the signals simply wait (``pushed_to_neo4j=0``
        in production) for the drain to recover. ``unavailable_calls``
        counts the buffers made during the outage window.
    """

    def __init__(
        self,
        *,
        raise_on_stage: Exception | None = None,
        available: bool = True,
    ) -> None:
        self.staged: list[tuple[Any, ...]] = []
        self._raise_on_stage = raise_on_stage
        self._available = available
        # ``unavailable_calls`` counts the times stage was invoked while
        # the sink was unavailable — lets contract tests assert the
        # caller did attempt the write (vs the more common "no call at
        # all" false-positive).
        self.unavailable_calls: int = 0

    def buffer(self, signals: Any) -> int:
        if self._raise_on_stage is not None:
            raise self._raise_on_stage
        if not self._available:
            # Downstream outage — staging still succeeds (durable), the
            # signals wait for the drain. Count the outage-window call.
            self.unavailable_calls += 1
        batch = tuple(signals)
        self.staged.append(batch)
        return len(batch)

    def set_available(self, value: bool) -> None:
        """Test helper — flip the sink's availability mid-scenario."""
        self._available = value


class FakeDrainGraphRepository:
    """GH #334 — Protocol-compliant fake for the Neo4j drain.

    Satisfies :class:`kairix.core.curator.protocols.DrainGraphRepository`.
    Records every ``cypher`` call in ``cypher_calls`` so tests can
    assert which MERGE statements landed and with what parameters.

    Knobs:
      * ``available`` — controls the ``available`` property; default True.
      * ``raise_on_value`` — when set to a string, the next ``cypher``
        call whose ``params["value"]`` equals this string raises a
        ``RuntimeError``. Used by the partial-failure scenarios to
        prove the drain marks one row as failed and continues.
      * ``raise_always`` — when True, every ``cypher`` call raises a
        ``RuntimeError`` until cleared. Used to prove total-outage
        handling.
    """

    def __init__(
        self,
        *,
        available: bool = True,
        raise_on_value: str | None = None,
        raise_always: bool = False,
    ) -> None:
        self._available = available
        self.raise_on_value: str | None = raise_on_value
        self.raise_always: bool = raise_always
        # Each entry: (cypher_query, params_dict)
        self.cypher_calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def available(self) -> bool:
        return self._available

    def set_available(self, value: bool) -> None:
        """Test helper — flip availability mid-scenario for recovery proofs."""
        self._available = value

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        recorded_params: dict[str, Any] = dict(params or {})
        self.cypher_calls.append((query, recorded_params))
        if self.raise_always:
            raise RuntimeError("FakeDrainGraphRepository: raise_always set")
        if self.raise_on_value is not None and recorded_params.get("value") == self.raise_on_value:
            raise RuntimeError(f"FakeDrainGraphRepository: scripted failure on value={self.raise_on_value!r}")
        return []


class FakeBronzeStore:
    """In-memory :class:`kairix.core.protocols.BronzeStore`.

    Faithful to the shipped
    :class:`kairix.core.connectors.streaming_bronze.StreamingBronzeStore`
    (the only production BronzeStore since streaming-bronze Phase 7):

      * ``write`` records ``(source_name, item_id, mime, fetched_at,
        content_hash)`` keyed on ``(source_name, item_id)`` — repeated
        writes replace the row — and discards the raw bytes, returning a
        :class:`BronzeRef` with ``raw_path=None``.
      * ``read`` ALWAYS raises
        :class:`~kairix.core.connectors.streaming_bronze.BronzeNotPersistedError`
        — streaming bronze retains no bytes.
      * ``replay`` yields the rows for one ``source_name`` oldest first,
        optionally narrowed to ``fetched_at >= since``.

    F68 knob: ``raise_on_write`` — when set, :meth:`write` raises it
    (the in-memory analogue of the SQLite layer rejecting the INSERT,
    e.g. ``bronze_records`` absent).
    """

    def __init__(self, *, raise_on_write: BaseException | None = None) -> None:
        self._raise_on_write = raise_on_write
        self._rows: dict[tuple[str, str], Any] = {}

    def write(self, source_name: str, item_id: str, raw: bytes, mime: str) -> Any:
        import hashlib
        from datetime import datetime, timezone

        from kairix.core.protocols import BronzeRef

        if self._raise_on_write is not None:
            raise self._raise_on_write
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ref = BronzeRef(
            source_name=source_name,
            item_id=item_id,
            raw_path=None,
            mime=mime,
            fetched_at=fetched_at,
            content_hash=hashlib.sha256(raw).hexdigest(),
        )
        self._rows[(source_name, item_id)] = ref
        return ref

    def read(self, ref: Any) -> tuple[bytes, str]:
        from kairix.core.connectors.streaming_bronze import BronzeNotPersistedError

        raise BronzeNotPersistedError(
            f"streaming bronze does not retain raw bytes for ({ref.source_name}, {ref.item_id}). "
            "fix: route through ``connector.fetch(item_id)`` to re-fetch from source instead of "
            "``bronze.read(ref)``."
        )

    def replay(self, source_name: str, since: Any = None) -> Any:
        rows = [r for (src, _item), r in self._rows.items() if src == source_name]
        if since is not None:
            rows = [r for r in rows if r.fetched_at >= since.isoformat()]
        return iter(sorted(rows, key=lambda r: r.fetched_at))


class FakeParagraphChunker:
    """Paragraph-splitting :class:`kairix.core.protocols.Chunker`.

    Holds the Protocol-level invariants every shipped chunker plugin
    honours (``kairix/chunkers/**``):

      * declares a non-empty ``version: str`` (F55);
      * empty / whitespace-only ``text`` yields ``()`` — the Silver
        layer relies on this to skip blank documents;
      * any other text yields a non-empty ``tuple`` of :class:`Chunk`
        (one per blank-line-separated paragraph), each carrying
        ``chunker_version=self.version`` (F55) and the input
        ``source_uri`` (F39);
      * malformed input is not masked — like the plugins, the fake
        calls ``text.strip()`` first, so a non-``str`` section (e.g.
        ``None`` from an image section with no OCR text) raises
        instead of silently returning ``()``.
    """

    name: str = "fake-paragraph"
    version: str = "0.0.0-fake"

    def chunk(self, *, text: str, section_kind: str, source_uri: str) -> tuple[Any, ...]:
        import hashlib

        from kairix.core.protocols import Chunk

        stripped = text.strip()
        if not stripped:
            return ()
        paragraphs = [p.strip() for p in stripped.split("\n\n") if p.strip()]
        return tuple(
            Chunk(
                text=p,
                content_hash=hashlib.sha256(p.encode("utf-8")).hexdigest(),
                source_name="",
                source_uri=source_uri,
                source_modified_at="",
                source_page=None,
                sensitivity="internal",
                chunker_version=self.version,
                metadata={"section_kind": section_kind},
            )
            for p in paragraphs
        )


class FakeChunkWriter:
    """Capture-only :class:`kairix.core.protocols.ChunkWriter`.

    Records every upsert call's chunks in ``writes`` (a list of tuples)
    and every delete call's source_uri in ``deletes``. Returns counts
    matching the production writer's contract. Tests assert against
    ``writes`` / ``deletes`` to verify chunk content + that the rollback
    or re-projection path issued the expected calls.

    The internal ``_by_uri`` dict mirrors the SQLite ``documents`` view
    keyed by ``source_uri`` so ``delete_by_source_uri`` can return the
    real row count + the next ``upsert`` for the same URI behaves like
    production (replace-on-key). Tests that care about idempotency
    drive the fake through both methods rather than asserting raw call
    counts.
    """

    def __init__(
        self,
        *,
        raise_on_upsert: BaseException | None = None,
        raise_on_delete: BaseException | None = None,
    ) -> None:
        self.writes: list[tuple[Any, ...]] = []
        self.deletes: list[str] = []
        self._by_uri: dict[str, int] = {}
        # F68 knobs — the in-memory analogue of the production SQLite
        # writer's store rejecting the statement (IntegrityError, locked,
        # table absent). Both propagate; the writer never swallows.
        self._raise_on_upsert = raise_on_upsert
        self._raise_on_delete = raise_on_delete

    def upsert(self, chunks: Any) -> int:
        if self._raise_on_upsert is not None:
            raise self._raise_on_upsert
        batch = tuple(chunks)
        self.writes.append(batch)
        for c in batch:
            uri = getattr(c, "source_uri", "")
            if uri:
                self._by_uri[uri] = self._by_uri.get(uri, 0) + 1
        return len(batch)

    def delete_by_source_uri(self, source_uri: str) -> int:
        """Drop every chunk for ``source_uri``; record the call.

        Returns the count of rows that were tracked under this URI
        before the delete (matches the production writer's
        ``cursor.rowcount`` contract). Idempotent: calling twice
        returns N then 0.
        """
        if self._raise_on_delete is not None:
            raise self._raise_on_delete
        self.deletes.append(source_uri)
        return int(self._by_uri.pop(source_uri, 0))


class FakeEntitySummaryProjector:
    """Scriptable :class:`kairix.core.protocols.EntitySummaryProjector` (ADR-036).

    Records every :meth:`tick` call's ``per_tick_max_items`` in
    ``ticks`` so tests can assert the cap was honoured. The result is
    pre-scripted via the constructor's ``result`` kwarg so a test can
    pin "tick returns projected=5, failed=0" without wiring real
    Neo4j + ChunkWriter.

    F1/F2-clean by construction — this stands in via the normal
    constructor seam, not a monkey-patch.
    """

    def __init__(
        self,
        *,
        result: Any = None,
        raises: BaseException | None = None,
    ) -> None:
        from kairix.core.protocols import EntitySummaryProjectionResult

        self.ticks: list[int] = []
        self._result: Any = result if result is not None else EntitySummaryProjectionResult()
        # F68 knob — a tick-level fault (not a per-entity one, which the
        # production projector counts in ``failed``) propagates to the
        # worker boundary, like the real projector's injected-clock fault.
        self._raises = raises

    def tick(self, *, per_tick_max_items: int = 200) -> Any:
        self.ticks.append(per_tick_max_items)
        if self._raises is not None:
            raise self._raises
        return self._result


class FakeFeatureFlagResolver:
    """In-memory :class:`kairix.core.protocols.FeatureFlagResolver`.

    Protocol-compliant fake — tests pin specific flag states without
    touching the global :data:`kairix.core.features.registry.REGISTRY`
    or monkey-patching env vars. The fake never reads
    ``kairix.config.yaml`` or ``KAIRIX_FEATURE_*`` (F2/F4-clean by
    construction).

    Use the :meth:`with_flag` builder to thread declarations through a
    test fluently:

        >>> resolver = FakeFeatureFlagResolver().with_flag(
        ...     "obsidian_connector_primary", True
        ... )
        >>> resolver.get("obsidian_connector_primary")
        True

    Unknown flags raise ``KeyError`` — matching the production resolver's
    behaviour so tests catch typos the same way production does.
    """

    def __init__(self, flags: dict[str, bool] | None = None) -> None:
        self._flags: dict[str, bool] = dict(flags or {})

    def with_flag(self, name: str, value: bool) -> FakeFeatureFlagResolver:
        """Builder — return a copy with ``name`` set to ``value``.

        Returns a *new* resolver so chained ``with_flag`` calls don't
        mutate a shared instance. The pattern matches the FakePaths-
        style immutable construction used elsewhere in this module.
        """
        merged = dict(self._flags)
        merged[name] = value
        return FakeFeatureFlagResolver(merged)

    def get(self, name: str) -> bool:
        if name not in self._flags:
            raise KeyError(
                f"unknown feature flag {name!r}. fix: declare it via FakeFeatureFlagResolver().with_flag(name, value)."
            )
        return self._flags[name]

    def iter_all(self) -> Any:
        """Yield ``FlagStatus`` snapshots for every declared flag.

        Builds the snapshots lazily — the import of
        :class:`kairix.core.features.resolver.FlagStatus` happens at
        call time so tests that never iterate keep the fake free of any
        kairix.core.features dependency.
        """
        from kairix.core.features.resolver import FlagStatus

        for name in sorted(self._flags):
            value = self._flags[name]
            yield FlagStatus(
                name=name,
                default=value,
                effective=value,
                source="default",
                stage="introduce",
                introduced_in="v0.0.0",
                target_retire_in="v9999.0.0",
                owner="test",
                related_spec=None,
            )


# =============================================================================
# Topology Wave B — canonical capability-mix-in Protocol fakes
# =============================================================================
# Minimal stubs satisfying each new capability Protocol from
# ``kairix.core.protocols``. Used by ``tests/contracts/test_capability_protocols.py``
# to prove the Protocol shape is satisfied by both the canonical Fake
# AND the shipped connectors' Wave B shims. OAuthConnector +
# CredentialsConnector deliberately don't get dedicated Fakes — their
# methods don't carry observable state, so the shipped connectors'
# shim path is the contract proof.


class FakePollConnector:
    """Scripted :class:`kairix.core.protocols.PollConnector`.

    Yields the pre-seeded change events from
    :meth:`list_changes_for_container` regardless of the container's
    cursor token. Used by contract tests to assert the Protocol shape.

    ``raises`` is the failure-injection knob: when set, the poll raises
    it (mirrors a real connector whose source backend fails mid-poll —
    the error propagates, it is never swallowed into "no changes").
    """

    def __init__(self, *, events: list[Any] | None = None, raises: BaseException | None = None) -> None:
        self._events = list(events) if events is not None else []
        self._raises = raises

    def list_changes_for_container(self, container: Any) -> Any:
        del container
        if self._raises is not None:
            raise self._raises
        return iter(self._events)


class FakeCheckpointedConnector:
    """Scripted :class:`kairix.core.protocols.CheckpointedConnector`.

    F68 knob: ``raises`` — when set, :meth:`load_from_checkpoint` raises
    it (the source rejected the checkpoint: expired delta token / HTTP
    410), matching the shipped connectors, which propagate rather than
    swallow so the orchestrator can fall back to a full re-sync.
    """

    def __init__(self, *, events: list[Any] | None = None, raises: BaseException | None = None) -> None:
        self._events = list(events) if events is not None else []
        self._raises = raises

    def load_from_checkpoint(self, container: Any, checkpoint: str | None) -> Any:
        del container, checkpoint
        if self._raises is not None:
            raise self._raises
        return iter(self._events)


class FakeSlimConnector:
    """Scripted :class:`kairix.core.protocols.SlimConnector`.

    Yields the pre-seeded item_id strings from
    :meth:`retrieve_all_slim_docs`. Used by prune-cycle contract tests.
    ``raises=`` makes the id-listing raise on iteration (lazily, like the
    shipped connectors' generator-based listing).
    """

    def __init__(self, *, item_ids: list[str] | None = None, raises: BaseException | None = None) -> None:
        self._item_ids = list(item_ids) if item_ids is not None else []
        self._raises = raises

    def retrieve_all_slim_docs(self, container: Any) -> Any:
        del container
        if self._raises is not None:
            return _raising_iter(self._raises)
        return iter(self._item_ids)


class FakeSlimConnectorWithPermSync:
    """Scripted :class:`kairix.core.protocols.SlimConnectorWithPermSync`.

    ``raises=`` makes the perm-sync listing raise on iteration (backend
    failure or expired credentials).
    """

    def __init__(
        self,
        *,
        entries: list[tuple[str, str]] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._entries = list(entries) if entries is not None else []
        self._raises = raises

    def retrieve_all_slim_docs_with_perms(self, container: Any) -> Any:
        del container
        if self._raises is not None:
            return _raising_iter(self._raises)
        return iter(self._entries)


class FakeEventConnector:
    """Scripted :class:`kairix.core.protocols.EventConnector`.

    Records every subscribe / renew / unsubscribe call and replays
    seeded events from :meth:`handle_event`. Used by webhook-path
    contract tests.
    """

    def __init__(self, *, events: list[Any] | None = None, supported: bool = True) -> None:
        self._events = list(events) if events is not None else []
        # ``supported=False`` models a deployment with no push surface
        # (e.g. Slack without Socket Mode wired): ``subscribe`` returns
        # ``None`` so the framework falls back to polling — the shipped
        # connectors' "unsupported" sentinel.
        self._supported = supported
        self.subscribe_calls: list[str] = []
        self.renew_calls: list[str] = []
        self.unsubscribe_calls: list[str] = []
        self.handled_payloads: list[Any] = []

    def subscribe(self, callback_url: str) -> str | None:
        self.subscribe_calls.append(callback_url)
        if not self._supported:
            return None
        return f"sub-{len(self.subscribe_calls)}"

    def renew_subscription(self, subscription_id: str) -> str:
        self.renew_calls.append(subscription_id)
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> None:
        self.unsubscribe_calls.append(subscription_id)

    def handle_event(self, event: Any) -> Any:
        self.handled_payloads.append(event)
        return iter(self._events)


class FakeResolver:
    """Scripted :class:`kairix.core.protocols.Resolver`.

    Records every reindex call's failed_item_ids + include_permissions
    flag; yields seeded ChangeEvent items from :meth:`reindex`. Used
    by per-document failure-replay contract tests. ``raises=`` makes the
    replay raise on iteration (systemic refetch failure).
    """

    def __init__(self, *, events: list[Any] | None = None, raises: BaseException | None = None) -> None:
        self._events = list(events) if events is not None else []
        self._raises = raises
        self.reindex_calls: list[tuple[tuple[str, ...], bool]] = []

    def reindex(self, failed_item_ids: tuple[str, ...], *, include_permissions: bool = False) -> Any:
        self.reindex_calls.append((failed_item_ids, include_permissions))
        if self._raises is not None:
            return _raising_iter(self._raises)
        return iter(self._events)


class FakeHierarchyConnector:
    """Scripted :class:`kairix.core.protocols.HierarchyConnector`.

    Yields the pre-seeded HierarchyNode list parent-before-child.
    Pass ``raises=`` to make the walk raise AFTER yielding the seeded
    nodes — the shape the real connectors exhibit when the backend
    enumeration fails mid-walk (root emitted, then the source call raises).
    """

    def __init__(self, *, nodes: list[Any] | None = None, raises: BaseException | None = None) -> None:
        self._nodes = list(nodes) if nodes is not None else []
        self._raises = raises

    def load_hierarchy(self, cc_pair_id: int) -> Any:
        del cc_pair_id
        if self._raises is None:
            return iter(self._nodes)
        return self._raising_walk(self._raises)

    def _raising_walk(self, exc: BaseException) -> Any:
        yield from self._nodes
        raise exc


# ---------------------------------------------------------------------------
# Bulk-seed helpers for the soak tier (ADR-024 Bundle F).
#
# Soak tests seed N >= 10**4 rows through canonical fakes; the helpers
# live here (not in per-soak-test files) so they remain reusable across
# tests/soak/ and the F72 integrity-invariants soak variants from Bundle E.
# ---------------------------------------------------------------------------


def build_bulk_source_connector(
    *,
    name: str = "soak-source",
    n_events: int = 10_000,
    body_template: str = "soak body {i}\n",
    per_tick_max_items: int | None = None,
) -> FakeSourceConnector:
    """Construct a :class:`FakeSourceConnector` pre-seeded with ``n_events`` items.

    Each event has a deterministic item_id ``soak-item-{i:06d}``, a
    body of ``body_template.format(i=i)`` encoded UTF-8, and a
    monotonically increasing ``modified_at`` so cursor-tracking works
    if the test enables it.

    ``per_tick_max_items`` defaults to ``n_events`` so a single
    ``run_batch`` drains the whole backlog without budget-yield —
    soak tests that want to measure multi-tick progress pass a
    smaller value.

    Used by the soak tier's bronze-coverage-parity test and the
    cross-bundle integrity invariants (F72 soak variants).
    """
    from kairix.core.protocols import ChangeEvent

    effective_budget = per_tick_max_items if per_tick_max_items is not None else n_events
    events: list[Any] = []
    content: dict[str, bytes] = {}
    for i in range(n_events):
        item_id = f"soak-item-{i:06d}.md"
        modified_at = f"2026-01-01T00:00:{i % 60:02d}Z" if i < 60 else f"2026-01-{(i // 60) % 28 + 1:02d}T00:00:00Z"
        events.append(
            ChangeEvent(
                op="created",
                item_id=item_id,
                modified_at=modified_at,
            )
        )
        content[item_id] = body_template.format(i=i).encode("utf-8")
    return FakeSourceConnector(
        name=name,
        events=events,
        content=content,
        per_tick_max_items=effective_budget,
    )


def seed_bulk_entity_signals(
    db: Any,
    *,
    n_rows: int = 10_000,
    kind: str = "person",
    value_template: str = "person-{i:06d}",
    pushed_to_neo4j: int = 0,
    push_attempt_count: int = 0,
    base_modified_at: str = "2026-01-01T00:00:00Z",
) -> int:
    """Bulk-insert ``n_rows`` into the ``entity_signals`` staging table.

    Returns the number of rows inserted. Uses ``executemany`` for
    throughput so a 10k-row seed completes in ~50 ms on the soak
    runner (the production write path is per-row to keep the
    transactional shape narrow; soak tests bypass the writer to
    populate state directly because they're testing the *drain*, not
    the *stage*).

    F47-friendly: the test composes ``factory.build_neo4j_drainer``
    with a real ``db`` connection. The state under test is the rows
    this helper inserts; the drain then advances them.
    """
    rows = [
        (
            kind,
            value_template.format(i=i),
            f"soak://{value_template.format(i=i)}",
            base_modified_at,
            0.85,
            "internal",
            pushed_to_neo4j,
            push_attempt_count,
        )
        for i in range(n_rows)
    ]
    db.executemany(
        "INSERT INTO entity_signals (kind, value, source_uri, modified_at, confidence, "
        "sensitivity, pushed_to_neo4j, push_attempt_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()
    return n_rows


def seed_bulk_content_rows(
    db: Any,
    *,
    n_rows: int = 10_000,
    collection: str = "soak",
    body_template: str = "soak content body {i}\n",
) -> int:
    """Bulk-insert ``n_rows`` into ``documents`` + ``content``.

    Used by the vector-index-drift soak test — seeds the chunks the
    embed pipeline will pick up via ``_gather_pending_chunks``. The
    SQL mirrors the production write surface exactly (UPSERT shape
    on ``documents``; INSERT OR REPLACE on ``content``).

    Returns the number of rows inserted (one document + one content
    row per ``i``).
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    doc_rows = []
    content_rows = []
    for i in range(n_rows):
        content_hash = f"soakhash{i:08d}"
        path = f"soak/doc-{i:06d}.md"
        doc_rows.append(
            (
                collection,
                path,
                content_hash,
                "soak-source",
                f"soak://doc-{i:06d}",
                now,
                None,
                "internal",
                now,
                now,
            )
        )
        content_rows.append((content_hash, body_template.format(i=i), now))
    db.executemany(
        "INSERT INTO documents "
        "(collection, path, hash, source_name, source_uri, source_modified_at, "
        "source_page, sensitivity, created_at, modified_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
        "ON CONFLICT (collection, path) DO UPDATE SET hash = excluded.hash",
        doc_rows,
    )
    db.executemany(
        "INSERT OR REPLACE INTO content (hash, doc, created_at) VALUES (?, ?, ?)",
        content_rows,
    )
    db.commit()
    return n_rows


class FakeSecretsLoader:
    """In-memory :class:`kairix.secrets.SecretsResolver` for tests.

    Pass ``values={(scope, area, instance, leaf): "secret-value"}`` —
    the loader returns each value verbatim from :meth:`get`; any miss
    returns ``None`` and :meth:`require` raises
    :class:`kairix.secrets.SecretNotFoundError`.

    Designed to be the F2-clean drop-in for tests that historically
    used ``monkeypatch.setenv("KAIRIX_*")`` — pass a populated
    ``FakeSecretsLoader`` through the production code's ``secrets=``
    kwarg instead.
    """

    def __init__(
        self,
        *,
        values: dict[tuple[str, str, str | None, str], str] | None = None,
    ) -> None:
        self._values: dict[tuple[str, str, str | None, str], str] = dict(values or {})
        # Call history for tests that want to assert the loader was
        # asked for a particular identity (e.g. "verify the connector
        # required client-secret, not just bot-token").
        self.get_calls: list[tuple[str, str, str | None, str]] = []

    def get(
        self,
        scope: str,
        area: str,
        instance: str | None,
        leaf: str,
    ) -> str | None:
        self.get_calls.append((scope, area, instance, leaf))
        return self._values.get((scope, area, instance, leaf))

    def require(
        self,
        scope: str,
        area: str,
        instance: str | None,
        leaf: str,
    ) -> str:
        value = self.get(scope, area, instance, leaf)
        if value is None:
            from kairix.secrets import SecretNotFoundError, canonical_secret_name

            raise SecretNotFoundError(
                f"Required secret not available: {canonical_secret_name(scope, area, instance, leaf)}.",  # type: ignore[arg-type]  # F3 rationale: FakeSecretsLoader accepts plain str scope for test ergonomics; canonical_secret_name expects Literal.
            )
        return value


# ---------------------------------------------------------------------------
# kairix.connect — OAuth2 connect flow fakes (ADR-032 Phase 1)
# ---------------------------------------------------------------------------


class FakeCallbackListener:
    """In-memory ``CallbackListener`` that returns a pre-seeded callback.

    Tests pre-populate either ``callback`` (success path) or one of
    ``timeout`` / ``denied`` (failure-injection path). ``redirect_uri``
    is a configured string — no socket is bound.

    Per F1 / F2 — tests construct this directly, never monkeypatch the
    real :class:`kairix.connect.listener.LocalhostCallbackListener`.
    """

    def __init__(
        self,
        *,
        callback: Any = None,
        timeout: bool = False,
        denied: bool = False,
        denied_message: str = "consent denied",
        redirect_uri: str = "http://127.0.0.1:8080/oauth2callback",
        port: int = 8080,
    ) -> None:
        self._callback = callback
        self._timeout = timeout
        self._denied = denied
        self._denied_message = denied_message
        self._redirect_uri = redirect_uri
        self.port = port
        self.wait_calls: list[float] = []
        self.closed = False

    @property
    def redirect_uri(self) -> str:
        return self._redirect_uri

    def wait_for_callback(self, timeout_s: float = 120.0) -> Any:
        from kairix.connect.protocols import (
            CallbackDeniedError,
            CallbackResult,
            CallbackTimeoutError,
        )

        self.wait_calls.append(timeout_s)
        if self._timeout:
            raise CallbackTimeoutError(
                f"fake listener: simulated timeout after {timeout_s:.0f}s. "
                "fix: pre-seed FakeCallbackListener(callback=...) for the success path. "
                "next: see tests/fakes.py FakeCallbackListener docstring. "
                "run: pytest tests/unit/test_connect_listener.py -k success",
            )
        if self._denied:
            raise CallbackDeniedError(
                f"fake listener: {self._denied_message}. "
                "fix: pre-seed FakeCallbackListener(callback=...) for the success path. "
                "next: see tests/fakes.py FakeCallbackListener docstring. "
                "run: pytest tests/unit/test_connect_listener.py -k consent",
            )
        if isinstance(self._callback, CallbackResult):
            return self._callback
        # Default — synthesise a deterministic test callback.
        return CallbackResult(code="fake-code-001", state=None)

    def close(self) -> None:
        self.closed = True


class FakeTokenStore:
    """In-memory ``TokenStore`` that records every store() call.

    Tests pull the recorded payloads from ``self.writes`` to assert the
    correct canonical names + values were written. The default
    behaviour returns a synthetic :class:`WriteReport`; pass
    ``raises=`` to exercise the unauthorized branch.

    Mirrors the production stores' dynamic leaf-derivation shape
    (per ADR-032 Phase 2 follow-up): leaves are derived from the
    ``client`` + ``tokens`` dataclass fields at write time so the fake
    reports the SAME canonical names a real store would write — Google
    writes 4 (client-id, client-secret, refresh-token, access-token);
    Slack writes 3 (client-id, client-secret, bot-token) plus app-token
    when present. Tests can assert against the recorded names without
    knowing which service shape is in play.
    """

    def __init__(self, *, raises: BaseException | None = None, backend: str = "fake") -> None:
        self.writes: list[dict[str, Any]] = []
        self._raises = raises
        self._backend = backend

    def store(self, **kwargs: Any) -> Any:
        from kairix.connect.protocols import WriteReport
        from kairix.connect.store.leaves import leaf_pairs
        from kairix.secrets.naming import canonical_env_var

        if self._raises is not None:
            raise self._raises
        self.writes.append(kwargs)
        scope = kwargs["scope"]
        area = kwargs["area"]
        instance = kwargs.get("instance")
        client = kwargs["client"]
        tokens = kwargs["tokens"]
        names = tuple(canonical_env_var(scope, area, instance, leaf) for leaf, _ in leaf_pairs(client, tokens))
        return WriteReport(canonical_names=names, backend=self._backend, target="<fake>")


class FakeRefreshableToken:
    """Configurable ``RefreshableToken`` — pinned token, optional expiry.

    Faithful to :class:`kairix.connect.refresh.GoogleRefreshableToken`: an
    empty ``token`` (none ever set) reports expired, and a failing refresh
    surfaces as :class:`~kairix.connect.protocols.RefreshUnavailableError`
    (the injected ``refresh_raises`` is chained as ``__cause__``).
    """

    def __init__(
        self,
        *,
        token: str = "fake-access-token",
        expired: bool = False,
        refresh_raises: BaseException | None = None,
    ) -> None:
        self._token = token
        self._expired = expired
        self._raises = refresh_raises
        self.refresh_calls = 0

    def headers(self) -> dict[str, str]:
        if self.is_expired():
            self.refresh()
        return {"Authorization": f"Bearer {self._token}"}

    def is_expired(self) -> bool:
        return self._expired or not self._token

    def refresh(self) -> None:
        from kairix.connect.protocols import RefreshUnavailableError

        self.refresh_calls += 1
        if isinstance(self._raises, RefreshUnavailableError):
            raise self._raises
        if self._raises is not None:
            raise RefreshUnavailableError("fake connect: access-token refresh failed.") from self._raises
        self._expired = False
        self._token = self._token or "fake-refreshed-access-token"


class FakeBrowserLauncher:
    """``BrowserLauncher`` that records every URL it was asked to open."""

    def __init__(self, *, result: bool = True) -> None:
        self.opened: list[str] = []
        self._result = result

    def open(self, url: str) -> bool:
        self.opened.append(url)
        return self._result


class FakeScopeProfileResolver:
    """In-memory :class:`ScopeProfileResolver` for #372 / #373 test discipline.

    Returns a pre-seeded ``ResolvedScope`` keyed on the actor tuple. Used
    by :class:`TopologyCollectionResolver` unit / contract tests so they
    don't have to seed the topology SQL tables to exercise the resolver
    logic itself (the contract test covers the SQL → ResolvedScope path
    separately).

    Construct with ``with_actor(name, entries=[...])`` builder. Each entry
    is one of:

      * 3-tuple ``(collection_name, mode, max_sensitivity)`` — back-compat
        shape from GH #372. The ``default_in_scope`` flag is implicitly
        ``True`` (back-compat with the pre-#373 schema where every entry
        is in the default superset).
      * 4-tuple ``(collection_name, mode, max_sensitivity, default_in_scope)``
        — GH #373 shape. ``default_in_scope`` is a bool controlling whether
        the entry surfaces under ``resolve(default_only=True)``.

    ``mode`` is one of ``'read'`` / ``'write'`` / ``'read_write'``.
    Entries with mode='write' are EXCLUDED from the ``collections=`` field
    (they fail the can_read filter the real ScopeProfileResolver enforces);
    they surface in ``excluded_collections`` instead.

    Examples:
        >>> from tests.fakes import FakeScopeProfileResolver
        >>> # Back-compat 3-tuple shape (pre-#373)
        >>> fake = FakeScopeProfileResolver().with_actor(
        ...     "agent-alpha",
        ...     entries=[
        ...         ("sharepoint-all", "read", "internal"),
        ...         ("memory-bucket", "read_write", "restricted"),
        ...     ],
        ... )
        >>> scope = fake.resolve(actors=("agent-alpha",))
        >>> {c.name for c in scope.collections}
        {'sharepoint-all', 'memory-bucket'}

        >>> # #373 4-tuple shape with default_in_scope
        >>> fake = FakeScopeProfileResolver().with_actor(
        ...     "agent-alpha",
        ...     entries=[
        ...         ("sharepoint", "read", "internal", True),
        ...         ("reflib", "read", "public", False),
        ...     ],
        ... )
        >>> scope = fake.resolve(actors=("agent-alpha",), default_only=True)
        >>> {c.name for c in scope.collections}
        {'sharepoint'}

    F1-clean substitute: production code constructs the real
    :class:`ScopeProfileResolver`; tests pass this fake via the
    ``scope_profile_resolver=`` kwarg on
    :class:`TopologyCollectionResolver`.

    Planned production extension (GH #373, ``topology_default_in_scope``
    feature flag): the real ``ScopeProfileResolver.resolve`` will accept a
    new ``default_only: bool = False`` kwarg. When True, entries with
    ``default_in_scope=0`` are filtered out of ``ResolvedScope.collections``
    (they would normally surface). This fake honours that contract today so
    unit tests can pin the resolver wiring before the production change
    lands.
    """

    def __init__(self) -> None:
        # actor tuple → entries list of either 3-tuple (name, mode, max_sens)
        # or 4-tuple (name, mode, max_sens, default_in_scope).
        self._actors: dict[tuple[str, ...], list[tuple]] = {}
        self._raises_on_resolve: Exception | None = None
        # Capture the most recent ``default_only`` kwarg the resolver saw —
        # contract tests assert the Adapter propagates the kwarg through.
        self.last_default_only: bool | None = None

    def with_actor(
        self,
        actor: str,
        *,
        entries: list[tuple],
    ) -> FakeScopeProfileResolver:
        """Builder — declare an actor's scope-entry set.

        Accepts either the back-compat 3-tuple shape
        ``(name, mode, max_sensitivity)`` or the #373 4-tuple shape
        ``(name, mode, max_sensitivity, default_in_scope)``.

        Returns a new fake (immutable-builder pattern, matches
        :class:`FakeFeatureFlagResolver`).
        """
        clone = FakeScopeProfileResolver()
        clone._actors = {k: list(v) for k, v in self._actors.items()}
        clone._actors[(actor,)] = list(entries)
        clone._raises_on_resolve = self._raises_on_resolve
        return clone

    def with_raises(self, exc: Exception) -> FakeScopeProfileResolver:
        """Builder — pin a scripted exception for the next ``resolve`` call.

        Used by F68 failure-injection contract tests to prove the
        Adapter propagates resolver errors rather than silently
        swallowing them.
        """
        clone = FakeScopeProfileResolver()
        clone._actors = {k: list(v) for k, v in self._actors.items()}
        clone._raises_on_resolve = exc
        return clone

    def resolve(
        self,
        *,
        actors: tuple[str, ...],
        default_only: bool = False,
        **_: Any,
    ) -> Any:
        """Return a synthesized :class:`ResolvedScope` for ``actors``.

        Mirrors :meth:`ScopeProfileResolver.resolve` — the can_read
        filter is applied here so ``mode='write'`` entries land in
        ``excluded_collections`` rather than the ``collections`` tuple.

        Per #373: when ``default_only=True``, entries whose 4-tuple
        ``default_in_scope`` flag is False are dropped from
        ``collections``. 3-tuple entries are treated as
        ``default_in_scope=True`` (back-compat with pre-#373 callers).
        """
        self.last_default_only = default_only

        if self._raises_on_resolve is not None:
            raise self._raises_on_resolve

        from kairix.core.connectors.scope_profile_resolver import (
            ExcludedCollection,
            ResolvedCollection,
            ResolvedScope,
        )

        entries = self._actors.get(actors, [])
        collections = []
        excluded = []
        for raw in entries:
            # Unpack — support 3-tuple and 4-tuple entry shapes.
            if len(raw) == 4:
                name, mode, max_sens, default_in_scope = raw
            else:
                name, mode, max_sens = raw
                default_in_scope = True
            can_read = mode in ("read", "read_write")
            if not can_read:
                excluded.append(
                    ExcludedCollection(
                        name=name,
                        reason="actor_lacks_read",
                        escalation_hint=(f"grant can_read=True to {actors!r} for {name!r}"),
                    )
                )
                continue
            # #373 — default_only filter drops entries flagged out of default.
            if default_only and not default_in_scope:
                continue
            collections.append(
                ResolvedCollection(
                    name=name,
                    max_sensitivity=max_sens,  # type: ignore[arg-type]  # F3-rationale: F39Tier is Literal; fake accepts the str alias from the test seed
                    weight=1.0,
                )
            )
        return ResolvedScope(
            collections=tuple(collections),
            excluded_collections=tuple(excluded),
        )


def seed_bulk_scope_entries(
    db: Any,
    *,
    n_agents: int = 100,
    entries_per_agent: int = 100,
    default_in_scope_ratio: float = 0.7,
) -> int:
    """Soak-tier helper — seed N agents x M entries into topology_scope_*.

    Idempotent: drops any pre-existing rows for the ``agent-soak-*``
    actor prefix before inserting fresh ones. Deterministic: collection
    names + default_in_scope assignments derive from the loop indices,
    not from any RNG, so repeated calls produce identical row content.

    Used by :mod:`tests.soak.test_scope_resolver_at_scale` to drive the
    ScopeProfileResolver against a production-scale (10k+ row) scope-
    entries surface for the p95 latency assertion.

    Returns the number of scope_entries rows inserted (``n_agents *
    entries_per_agent``) so the caller can assert against the seed.

    Planned production extension (GH #373): ``topology_scope_entries``
    will gain a ``default_in_scope INTEGER NOT NULL DEFAULT 1`` column.
    This helper writes to that column when present and falls back to the
    pre-migration shape (no column) when not — so the soak helper
    survives the migration in either direction.
    """
    cur = db.execute(
        "SELECT id FROM topology_scope_profiles WHERE actor_id LIKE 'agent-soak-%'",
    )
    stale_ids = [row[0] for row in cur.fetchall()]
    if stale_ids:
        placeholders = ",".join("?" for _ in stale_ids)
        db.execute(
            f"DELETE FROM topology_scope_entries WHERE scope_profile_id IN ({placeholders})",
            stale_ids,
        )
        db.execute(
            f"DELETE FROM topology_scope_profiles WHERE id IN ({placeholders})",
            stale_ids,
        )

    # Detect whether the ``default_in_scope`` column exists. The soak helper
    # has to survive the migration in either direction so the post-impl
    # soak run uses the column and the pre-impl run silently skips it.
    cols = {row[1] for row in db.execute("PRAGMA table_info(topology_scope_entries)").fetchall()}
    has_default_col = "default_in_scope" in cols

    now = "2026-06-01T00:00:00Z"
    inserted = 0
    for agent_idx in range(n_agents):
        actor_id = f"agent-soak-{agent_idx:04d}"
        cur = db.execute(
            "INSERT INTO topology_scope_profiles "
            "(actor_id, actor_kind, inherits_from_json, created_at, updated_at) "
            "VALUES (?, 'agent', '[]', ?, ?)",
            (actor_id, now, now),
        )
        profile_id = cur.lastrowid
        for entry_idx in range(entries_per_agent):
            collection_name = f"collection-soak-{entry_idx:04d}"
            default_in_scope = 1 if (entry_idx / entries_per_agent) < default_in_scope_ratio else 0
            if has_default_col:
                db.execute(
                    "INSERT INTO topology_scope_entries "
                    "(scope_profile_id, collection_name, can_read, can_write, "
                    "max_sensitivity, default_in_scope) "
                    "VALUES (?, ?, 1, 0, 'internal', ?)",
                    (profile_id, collection_name, default_in_scope),
                )
            else:
                db.execute(
                    "INSERT INTO topology_scope_entries "
                    "(scope_profile_id, collection_name, can_read, can_write, max_sensitivity) "
                    "VALUES (?, ?, 1, 0, 'internal')",
                    (profile_id, collection_name),
                )
            inserted += 1
    db.commit()
    return inserted


# ---------------------------------------------------------------------------
# FakeMcpDispatchClient — for tests/cli/test_route_via_mcp.py (#411)
# ---------------------------------------------------------------------------


class FakeMcpDispatchClient:
    """Fake :class:`kairix.agents.mcp.client_dispatcher.McpDispatchClient`.

    Tests configure ``responsive`` and ``envelope`` upfront; the fake
    records every call so assertions can inspect the dispatched tool
    name and kwargs. Replaces ad-hoc ``@patch`` of ``requests.head`` —
    F1-clean by construction. Mirrors the Protocol surface defined in
    ``kairix/agents/mcp/client_dispatcher.py``: ``is_responsive`` +
    ``call_tool``.

    Example:
        >>> client = FakeMcpDispatchClient(
        ...     responsive=True,
        ...     envelope={"results": [{"id": "doc-1"}]},
        ... )
        >>> deps = DispatcherDeps(client=client)
        >>> exit_code = try_dispatch_via_mcp("search", ["foo", "--json"], deps=deps)
        >>> assert exit_code == 0
        >>> assert client.calls == [("search", {"query": "foo"})]
    """

    def __init__(
        self,
        *,
        responsive: bool = True,
        envelope: dict[str, Any] | None = None,
        is_error: bool = False,
        responsiveness_delay_s: float = 0.0,
        raise_on_call: Exception | None = None,
    ) -> None:
        self._responsive = responsive
        self._envelope = envelope if envelope is not None else {"status": "fake-ok"}
        self._is_error = is_error
        self._responsiveness_delay_s = responsiveness_delay_s
        self._raise_on_call = raise_on_call
        # Recorders — tests assert on these.
        self.responsive_calls: list[tuple[str, float]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def is_responsive(self, endpoint: str, timeout_s: float) -> bool:
        self.responsive_calls.append((endpoint, timeout_s))
        if self._responsiveness_delay_s > 0:
            import time as _time

            _time.sleep(self._responsiveness_delay_s)
        return self._responsive

    def call_tool(
        self,
        endpoint: str,
        tool_name: str,
        kwargs: dict[str, Any],
    ) -> Any:
        """Return a configured :class:`McpToolResult`-shaped object.

        Returns the dataclass from ``client_dispatcher`` (lazy import to
        avoid coupling fakes.py to the dispatcher module at import
        time — keeps the fake usable from tests that don't import the
        dispatcher).
        """
        _ = endpoint
        self.calls.append((tool_name, dict(kwargs)))
        if self._raise_on_call is not None:
            raise self._raise_on_call
        from kairix.agents.mcp.client_dispatcher import McpToolResult

        return McpToolResult(payload=dict(self._envelope), is_error=self._is_error)


class FakeReadinessGate:
    """In-memory :class:`kairix.agents.mcp.readiness.ReadinessGate`.

    Starts not-ready (the cold-start default) unless ``ready=True``;
    ``mark_ready`` flips the flag and is idempotent. ``mark_ready_calls``
    records how many times the startup hook fired.
    """

    def __init__(self, *, ready: bool = False) -> None:
        self._ready = ready
        self.mark_ready_calls: int = 0

    def is_ready(self) -> bool:
        return self._ready

    def mark_ready(self) -> None:
        self.mark_ready_calls += 1
        self._ready = True


class FakeStage:
    """Scriptable :class:`kairix.core.observability.stage.Stage`.

    Knobs:

    - ``outcome`` — the :class:`StageOutcome` ``process`` returns on the
      happy path (defaults to ``FETCH_OK``).
    - ``process_raises`` — raised from ``process`` instead of returning.
    - ``classify_code`` — the :class:`StatusCode` ``classify_exception``
      maps every exception to (defaults to ``PIPELINE_STAGE_NO_EMIT``).
    - ``classify_raises`` — raised from ``classify_exception`` (an
      unmapped exception class blowing up the classifier itself).

    Recorders: ``processed`` (item ids seen) and ``classified`` (the
    exceptions handed to the classifier).
    """

    def __init__(
        self,
        *,
        name: str = "fetch",
        outcome: Any = None,
        process_raises: BaseException | None = None,
        classify_code: Any = None,
        classify_raises: BaseException | None = None,
    ) -> None:
        self.name = name
        self._outcome = outcome
        self._process_raises = process_raises
        self._classify_code = classify_code
        self._classify_raises = classify_raises
        self.processed: list[str] = []
        self.classified: list[BaseException] = []

    def process(self, ctx: Any) -> Any:
        from kairix.core.observability.stage import StageOutcome
        from kairix.core.observability.status_codes import StatusCode

        self.processed.append(ctx.item_id)
        if self._process_raises is not None:
            raise self._process_raises
        return self._outcome if self._outcome is not None else StageOutcome(code=StatusCode.FETCH_OK)

    def classify_exception(self, exc: BaseException) -> Any:
        from kairix.core.observability.status_codes import StatusCode

        self.classified.append(exc)
        if self._classify_raises is not None:
            raise self._classify_raises
        return self._classify_code if self._classify_code is not None else StatusCode.PIPELINE_STAGE_NO_EMIT


class FakeOpenclawContext:
    """In-memory openclaw plugin context (``OpenclawContext`` Protocol).

    Exposes ``agent_name`` and ``appendSystemContext``; appended text is
    captured in ``appended``. ``append_raises`` makes every
    ``appendSystemContext`` call raise (openclaw's prompt assembly
    failing), after recording the attempt in ``append_attempts``.
    """

    def __init__(self, *, agent_name: str = "agent-alpha", append_raises: BaseException | None = None) -> None:
        self.agent_name = agent_name
        self._append_raises = append_raises
        self.appended: list[str] = []
        self.append_attempts: list[str] = []

    def appendSystemContext(self, text: str) -> None:  # noqa: N802 — openclaw API name fixed by the OpenclawContext Protocol
        self.append_attempts.append(text)
        if self._append_raises is not None:
            raise self._append_raises
        self.appended.append(text)


# ---------------------------------------------------------------------------
# kairix.platform.setup.web — web setup wizard fakes (#474)
# ---------------------------------------------------------------------------


class FakeOAuth2Flow:
    """In-memory ``OAuth2Flow`` with scripted outcomes (#489).

    Drives the wizard's source-connect backend without any provider
    HTTP. Knobs:

    - ``tokens`` — the :class:`CapturedTokens` ``authorize`` returns;
      defaults to a Slack-shaped set (``bot_token`` populated).
    - ``raises`` — raised from ``authorize`` (pass a
      ``CallbackDeniedError`` for the denial path, any exception for
      the generic-failure path).
    - ``browser`` — when set, ``authorize`` calls ``browser.open`` with
      the scripted ``authorize_url`` first, mirroring the real flows so
      the wizard's consent phase becomes observable.
    - ``wait_for_listener`` — when True (default), ``authorize`` blocks
      on ``listener.wait_for_callback()`` exactly like the real flows,
      so tests exercise the full deliver/verify event dance.

    Recorders: ``redirect_uris`` (one per authorize call — proves the
    flow saw the origin-derived redirect URI) and ``callback_results``.
    """

    def __init__(
        self,
        *,
        service_area: str = "slack",
        scopes: tuple[str, ...] = ("channels:read",),
        tokens: Any = None,
        raises: BaseException | None = None,
        authorize_url: str = "https://provider.test/consent",
        browser: Any = None,
        wait_for_listener: bool = True,
        client_id: str = "fake-client-id",
        client_secret: str = "fake-client-secret",  # pragma: allowlist secret — fixture value
        client_secret_path: Any = None,
    ) -> None:
        # ``client_secret_path`` mirrors the real file-backed flows
        # (GoogleOAuth2Flow): when set and absent on disk,
        # ``discover_client_credentials`` raises FileNotFoundError.
        self._client_secret_path = client_secret_path
        self.service_area = service_area
        self.scopes = scopes
        self._tokens = tokens
        self._raises = raises
        self._authorize_url = authorize_url
        self._browser = browser
        self._wait_for_listener = wait_for_listener
        self._client_id = client_id
        self._client_secret = client_secret
        self.redirect_uris: list[str] = []
        self.callback_results: list[Any] = []

    def discover_client_credentials(self) -> Any:
        from kairix.connect.protocols import ClientCredentials

        if self._client_secret_path is not None and not Path(self._client_secret_path).exists():
            raise FileNotFoundError(f"FakeOAuth2Flow: client_secret.json not found at {self._client_secret_path}")
        return ClientCredentials(client_id=self._client_id, client_secret=self._client_secret)

    def authorize(self, *, listener: Any, timeout_s: float = 120.0) -> Any:
        from kairix.connect.protocols import CapturedTokens

        self.redirect_uris.append(listener.redirect_uri)
        if self._browser is not None:
            self._browser.open(self._authorize_url)
        if self._raises is not None:
            raise self._raises
        if self._wait_for_listener:
            self.callback_results.append(listener.wait_for_callback(timeout_s=timeout_s))
        if self._tokens is not None:
            return self._tokens
        return CapturedTokens(
            refresh_token="",
            access_token="",
            token_uri="https://provider.test/token",
            bot_token="xoxb-fake-bot-token",
        )


class FakeSetupService:
    """In-memory :class:`kairix.platform.setup.service.SetupService`.

    Protocol-compliant fake the web wizard's routes render against.
    Every screen-visible value is a constructor knob; failure-injection
    knobs (``validate_ok=False`` / ``scan_ok=False`` / ``index_error`` /
    ``handshake_ok=False``) drive the wizard's error rendering without
    any monkey-patching (F1/F2-clean by construction).

    ``index_status()`` advances one tick per call through a simple
    chunk counter so progress-polling tests observe running → done
    without sleeping: each call adds ``chunks_per_tick`` until
    ``chunks_total`` is reached.

    Recorders (``saved_providers`` / ``saved_sources`` /
    ``start_index_calls`` / ``validate_calls`` / ``search_queries``)
    let outcome tests assert what the wizard asked the service to do.
    """

    def __init__(
        self,
        *,
        validate_ok: bool = True,
        models: tuple[str, ...] = ("model-alpha", "model-beta"),
        validate_error: str | None = None,
        validate_deployment_missing: bool = False,
        scan_ok: bool = True,
        scan_files: int = 533,
        scan_words: int = 3_200_000,
        scan_cost_usd: float = 0.04,
        scan_error: str | None = None,
        save_provider_raises: Exception | None = None,
        save_source_raises: Exception | None = None,
        in_container: bool = False,
        suggested_folder: str = "",
        chunks_total: int = 100,
        chunks_per_tick: int = 50,
        index_error: str | None = None,
        index_statuses: tuple[Any, ...] | None = None,
        search_hits: tuple[Any, ...] | None = None,
        mcp_url: str = "http://127.0.0.1:8765/mcp",
        connect_snippets: tuple[Any, ...] | None = None,
        handshake_ok: bool = True,
        tools_count: int = 12,
        handshake_error: str | None = None,
        tour_agent: str = "agent-alpha",
        tour_prep_summary: str = "Across your documents the rollout plan is the main thread, with agent-alpha leading.",
        tour_prep_sources: tuple[str, ...] = ("notes/kickoff.md", "notes/rollout.md"),
        tour_prep_message: str = "",
        tour_remember_found: bool = True,
        tour_remember_message: str = "",
        tour_brief_preview: str = "Recent activity: the rollout kicked off and two decisions landed.",
        tour_brief_next_action: str = "",
        tour_brief_message: str = "",
        tour_timeline_hits: tuple[Any, ...] | None = None,
        tour_timeline_message: str = "",
        source_options: tuple[Any, ...] | None = None,
        source_auth_start_error: str | None = None,
        source_auth_statuses: tuple[Any, ...] | None = None,
        callback_ok: bool = True,
        callback_error: str | None = None,
        source_units: tuple[Any, ...] | None = None,
        source_units_pickable: bool = True,
        source_units_note: str = "",
        source_units_error: str | None = None,
        save_oauth_raises: Exception | None = None,
        save_oauth_error: str | None = None,
        save_oauth_summary: str = "2 channels selected — kairix will fetch and index messages from these channels.",
        config_file: str = "kairix.config.yaml",
    ) -> None:
        from kairix.platform.setup.service import ConnectSnippet, SearchPreviewHit, TourTimelineHit

        self._validate_ok = validate_ok
        self._models = models
        self._validate_error = validate_error
        self._validate_deployment_missing = validate_deployment_missing
        self._save_provider_raises = save_provider_raises
        self._save_source_raises = save_source_raises
        self._in_container = in_container
        self._suggested_folder = suggested_folder
        self._scan_ok = scan_ok
        self._scan_files = scan_files
        self._scan_words = scan_words
        self._scan_cost_usd = scan_cost_usd
        self._scan_error = scan_error
        self._chunks_total = chunks_total
        self._chunks_per_tick = chunks_per_tick
        self._index_error = index_error
        # Optional explicit status script — when provided, index_status()
        # returns these in order (last one repeats). Lets tests pin edge
        # states the counter model can't produce (e.g. running with an
        # unknown chunks_total of 0).
        self._index_statuses = index_statuses
        self._index_status_calls = 0
        self._search_hits = (
            search_hits
            if search_hits is not None
            else (
                SearchPreviewHit(
                    title="Project kickoff notes",
                    snippet="agent-alpha agreed the rollout starts next sprint…",
                    source="notes/kickoff.md",
                    score=0.92,
                ),
            )
        )
        self._mcp_url = mcp_url
        claude_code_config = '{"mcpServers": {"kairix": {"url": "' + mcp_url + '"}}}'
        self._connect_snippets = (
            connect_snippets
            if connect_snippets is not None
            else (
                ConnectSnippet(client="Claude Code", config_text=claude_code_config),
                ConnectSnippet(client="Generic MCP over HTTP", config_text=mcp_url),
            )
        )
        self._handshake_ok = handshake_ok
        self._tools_count = tools_count
        self._handshake_error = handshake_error
        # Capability tour knobs (#490).
        self._tour_agent = tour_agent
        self._tour_prep_summary = tour_prep_summary
        self._tour_prep_sources = tour_prep_sources
        self._tour_prep_message = tour_prep_message
        self._tour_remember_found = tour_remember_found
        self._tour_remember_message = tour_remember_message
        self._tour_brief_preview = tour_brief_preview
        self._tour_brief_next_action = tour_brief_next_action
        self._tour_brief_message = tour_brief_message
        self._tour_timeline_hits = (
            tour_timeline_hits
            if tour_timeline_hits is not None
            else (
                TourTimelineHit(
                    title="Sprint planning",
                    snippet="agent-alpha agreed the rollout starts next sprint…",
                    source="notes/kickoff.md",
                    date="2026-06-08",
                ),
            )
        )
        self._tour_timeline_message = tour_timeline_message
        # Source OAuth knobs (#489).
        self._source_options = source_options
        self._source_auth_start_error = source_auth_start_error
        self._source_auth_statuses = source_auth_statuses
        self._source_auth_status_calls = 0
        self._callback_ok = callback_ok
        self._callback_error = callback_error
        self._source_units = source_units
        self._source_units_pickable = source_units_pickable
        self._source_units_note = source_units_note
        self._source_units_error = source_units_error
        self._save_oauth_raises = save_oauth_raises
        self._save_oauth_error = save_oauth_error
        self._save_oauth_summary = save_oauth_summary
        # The config file wizard saves land in (#492) — shown on the
        # source-saved and done screens.
        self._config_file = config_file
        # Recorders + mutable wizard state.
        self.saved_providers: list[tuple[str, str, str | None, str | None, str | None]] = []
        self.saved_sources: list[str] = []
        self.start_index_calls: int = 0
        self.validate_calls: list[tuple[str, str, str | None, str | None]] = []
        self.search_queries: list[str] = []
        self.tour_prep_queries: list[str] = []
        self.tour_remember_contents: list[str] = []
        self.tour_brief_calls: int = 0
        self.tour_timeline_queries: list[str] = []
        self.source_auth_starts: list[tuple[str, dict[str, str], str]] = []
        self.callback_deliveries: list[tuple[str | None, dict[str, str]]] = []
        self.saved_oauth_sources: list[tuple[str, str, tuple[str, ...]]] = []
        self._chunks_done = 0
        self._index_running = False

    def status(self) -> Any:
        from kairix.platform.setup.service import SetupStatus

        return SetupStatus(
            provider_done=bool(self.saved_providers),
            source_done=bool(self.saved_sources),
            index_done=self._chunks_done >= self._chunks_total,
        )

    def validate_provider(
        self,
        provider: str,
        api_key: str,
        endpoint: str | None,
        deployment: str | None = None,
    ) -> Any:
        from kairix.platform.setup.service import ProviderValidation

        self.validate_calls.append((provider, api_key, endpoint, deployment))
        if self._validate_deployment_missing:
            error = self._validate_error or (
                f"Your key works, but this Azure resource has no deployment named '{deployment or 'embed-model'}'."
            )
            return ProviderValidation(ok=False, models=(), error=error, deployment_missing=True)
        if not self._validate_ok:
            error = self._validate_error or "Authentication failed — your key was rejected by the provider."
            return ProviderValidation(ok=False, models=(), error=error)
        return ProviderValidation(ok=True, models=self._models, error=None)

    def save_provider(
        self,
        provider: str,
        api_key: str,
        endpoint: str | None,
        model: str | None,
        deployment: str | None = None,
    ) -> None:
        if self._save_provider_raises is not None:
            raise self._save_provider_raises
        self.saved_providers.append((provider, api_key, endpoint, model, deployment))

    def scan_folder(self, path: str) -> Any:
        from kairix.platform.setup.service import FolderScan

        if not self._scan_ok:
            error = self._scan_error or f"Folder not found or not readable: {path}"
            return FolderScan(ok=False, files=0, words_estimate=0, cost_estimate_usd=0.0, error=error)
        return FolderScan(
            ok=True,
            files=self._scan_files,
            words_estimate=self._scan_words,
            cost_estimate_usd=self._scan_cost_usd,
            error=None,
        )

    def save_source(self, path: str) -> None:
        if self._save_source_raises is not None:
            raise self._save_source_raises
        self.saved_sources.append(path)

    def config_file_path(self) -> str:
        return self._config_file

    def source_hint(self) -> Any:
        from kairix.platform.setup.service import SourceHint

        return SourceHint(in_container=self._in_container, suggested_path=self._suggested_folder)

    def start_index(self) -> None:
        self.start_index_calls += 1
        self._index_running = True
        self._chunks_done = 0

    def index_status(self) -> Any:
        from kairix.platform.setup.backends import EMPTY_INDEX_MESSAGE
        from kairix.platform.setup.service import IndexStatus

        if self._index_statuses is not None:
            idx = min(self._index_status_calls, len(self._index_statuses) - 1)
            self._index_status_calls += 1
            return self._index_statuses[idx]
        if self._index_error is not None:
            return IndexStatus(
                running=False,
                done=False,
                chunks_done=self._chunks_done,
                chunks_total=self._chunks_total,
                error=self._index_error,
            )
        if self._chunks_total == 0:
            # Mirror the real backend (review M1): an empty corpus is
            # only "done" after a run actually happened, and it carries
            # the honest 0-documents copy instead of a clean finish.
            ran = self.start_index_calls > 0
            return IndexStatus(
                running=False,
                done=ran,
                chunks_done=0,
                chunks_total=0,
                error=EMPTY_INDEX_MESSAGE if ran else None,
            )
        if self._index_running and self._chunks_done < self._chunks_total:
            self._chunks_done = min(self._chunks_total, self._chunks_done + self._chunks_per_tick)
        done = self._chunks_done >= self._chunks_total
        if done:
            self._index_running = False
        return IndexStatus(
            running=self._index_running,
            done=done,
            chunks_done=self._chunks_done,
            chunks_total=self._chunks_total,
            error=None,
        )

    def first_search(self, query: str) -> Any:
        from kairix.platform.setup.service import SearchPreview

        self.search_queries.append(query)
        return SearchPreview(results=tuple(self._search_hits))

    def agent_connect_info(self) -> Any:
        from kairix.platform.setup.service import AgentConnectInfo

        return AgentConnectInfo(mcp_url=self._mcp_url, snippets=tuple(self._connect_snippets))

    def verify_agent_handshake(self) -> Any:
        from kairix.platform.setup.service import HandshakeResult

        if not self._handshake_ok:
            error = self._handshake_error or "No agent handshake observed on the MCP endpoint."
            return HandshakeResult(ok=False, tools_count=0, error=error)
        return HandshakeResult(ok=True, tools_count=self._tools_count, error=None)

    # ------------------------------------------------------------------
    # Capability tour (#490)
    # ------------------------------------------------------------------

    def tour_prep(self, query: str) -> Any:
        from kairix.platform.setup.service import TourPrep

        self.tour_prep_queries.append(query)
        if self._tour_prep_message:
            return TourPrep(summary="", sources=(), message=self._tour_prep_message)
        return TourPrep(summary=self._tour_prep_summary, sources=tuple(self._tour_prep_sources), message="")

    def tour_remember_roundtrip(self, content: str) -> Any:
        from kairix.platform.setup.service import TourRememberRoundtrip

        self.tour_remember_contents.append(content)
        if self._tour_remember_message:
            return TourRememberRoundtrip(
                saved=False,
                agent=self._tour_agent,
                path="",
                found=False,
                elapsed_ms=0,
                hits=(),
                message=self._tour_remember_message,
            )
        return TourRememberRoundtrip(
            saved=True,
            agent=self._tour_agent,
            path=f"04-Agent-Knowledge/{self._tour_agent}/2026-06-11-setup-finished.md",
            found=self._tour_remember_found,
            elapsed_ms=240,
            hits=tuple(self._search_hits) if self._tour_remember_found else (),
            message="",
        )

    def tour_brief(self) -> Any:
        from kairix.platform.setup.service import TourBrief

        self.tour_brief_calls += 1
        return TourBrief(
            agent=self._tour_agent,
            preview="" if self._tour_brief_message else self._tour_brief_preview,
            next_action=self._tour_brief_next_action,
            message=self._tour_brief_message,
        )

    def tour_timeline(self, query: str) -> Any:
        from kairix.platform.setup.service import TourTimeline

        self.tour_timeline_queries.append(query)
        if self._tour_timeline_message:
            return TourTimeline(hits=(), message=self._tour_timeline_message)
        return TourTimeline(hits=tuple(self._tour_timeline_hits), message="")

    # Source OAuth connect (#489)
    # ------------------------------------------------------------------

    def source_options(self) -> Any:
        from kairix.platform.setup.source_oauth import DEFAULT_SOURCE_OPTIONS

        return tuple(self._source_options) if self._source_options is not None else DEFAULT_SOURCE_OPTIONS

    def start_source_auth(self, provider: str, fields: Any, origin: str) -> Any:
        from kairix.platform.setup.service import SourceAuthStart

        self.source_auth_starts.append((provider, dict(fields), origin))
        if self._source_auth_start_error is not None:
            return SourceAuthStart(ok=False, error=self._source_auth_start_error)
        return SourceAuthStart(ok=True, error=None)

    def source_auth_status(self) -> Any:
        from kairix.platform.setup.service import SourceAuthStatus

        if self._source_auth_statuses:
            idx = min(self._source_auth_status_calls, len(self._source_auth_statuses) - 1)
            self._source_auth_status_calls += 1
            return self._source_auth_statuses[idx]
        return SourceAuthStatus(provider="", phase="idle", authorize_url=None, error=None)

    def complete_source_callback(self, state: str | None, params: Any) -> Any:
        from kairix.platform.setup.service import CallbackOutcome

        self.callback_deliveries.append((state, dict(params)))
        if not self._callback_ok:
            error = self._callback_error or (
                "No source connection is waiting for a sign-in response."
                " fix: start the connection from the wizard's source step."
                " next: open the source step and pick a source."
            )
            return CallbackOutcome(ok=False, error=error)
        return CallbackOutcome(ok=True, error=None)

    def discover_source_units(self, provider: str) -> Any:
        from kairix.platform.setup.service import SourceUnit, SourceUnits

        units = (
            tuple(self._source_units)
            if self._source_units is not None
            else (
                SourceUnit(unit_id="C001", name="#general", detail="public channel"),
                SourceUnit(unit_id="C002", name="#engineering", detail="public channel"),
            )
        )
        return SourceUnits(
            provider=provider,
            units=units,
            pickable=self._source_units_pickable,
            note=self._source_units_note,
            error=self._source_units_error,
        )

    def save_oauth_source(self, provider: str, instance: str, picks: tuple[str, ...]) -> Any:
        from kairix.platform.setup.service import SavedSource

        if self._save_oauth_raises is not None:
            raise self._save_oauth_raises
        self.saved_oauth_sources.append((provider, instance, tuple(picks)))
        if self._save_oauth_error is not None:
            return SavedSource(ok=False, summary="", error=self._save_oauth_error)
        return SavedSource(ok=True, summary=self._save_oauth_summary, error=None, config_file=self._config_file)


class FakeMcpTransportServer:
    """Minimal FastMCP-shaped object for ``build_mcp_app`` composition.

    Exposes exactly the surface the transport composer consumes:
    ``settings`` (with ``stateless_http`` / ``json_response``),
    ``streamable_http_app()`` and ``sse_app(mount_path)``. Lets BDD and
    integration tests compose the real Starlette app without the
    ``mcp`` package's real FastMCP and without a network listener.
    """

    class _Settings:
        """Mutable stand-in for FastMCP's pydantic Settings model."""

        def __init__(self) -> None:
            self.stateless_http = False
            self.json_response = False

    def __init__(self) -> None:
        self.settings = FakeMcpTransportServer._Settings()

    def streamable_http_app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def handler(_request: Any) -> Any:
            return PlainTextResponse("fake-streamable-ok")

        return Starlette(routes=[Route("/mcp", handler, methods=["GET", "POST"])])

    def sse_app(self, mount_path: str = "/sse") -> Any:
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def handler(_request: Any) -> Any:
            return PlainTextResponse("fake-sse-ok")

        return Starlette(routes=[Route(mount_path, handler, methods=["GET"])])


class FakeCorpusSource:
    """In-memory ``CorpusSource`` — returns crafted tarball bytes + a sha256.

    Builds a real gzip tarball containing a small fake corpus tree so the
    production extract path runs unchanged. The advertised sha256 is the
    honest hash of those bytes, unless ``corrupt`` is set — then it's a
    deliberately-wrong hash so the production fail-closed verify raises.
    No network. Records the requested ``version``/``url`` for assertions.

    ``raise_on_fetch`` injects a network-layer failure (the F68
    failure-mode for ``CorpusSource.fetch``): when set, ``fetch`` raises
    that exception instead of returning a corpus — mirrors a
    ``urllib``/``URLError`` when the release asset is unreachable.
    """

    def __init__(
        self,
        *,
        corrupt: bool = False,
        files: dict[str, str] | None = None,
        raise_on_fetch: BaseException | None = None,
    ) -> None:
        self.corrupt = corrupt
        self.files = files or {
            "reference-library/CATALOGUE.md": "# fake catalogue\n",
            "reference-library/reflib/agentic-ai/agent-loop-patterns.md": "fake doc body\n",
        }
        self.raise_on_fetch = raise_on_fetch
        self.requested_version: str | None = None
        self.requested_url: str | None = None

    def fetch(self, *, version: str, url: str | None) -> FetchedCorpus:
        import hashlib
        import io
        import tarfile

        from kairix.quality.benchmark.corpus import FetchedCorpus

        self.requested_version = version
        self.requested_url = url
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, body in self.files.items():
                raw = body.encode("utf-8")
                info = tarfile.TarInfo(name=name)
                info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))
        data = buf.getvalue()

        honest = hashlib.sha256(data).hexdigest()
        advertised = ("0" * 64) if self.corrupt else honest
        return FetchedCorpus(data=data, sha256=advertised, version=version, url=url or "fake://release")


class FakeCorpusDownloader:
    """Canonical fake for ``BenchmarkCLIDeps.download_corpus`` (#450).

    Drop-in replacement for ``corpus.default_download_corpus`` that runs
    the REAL fetch → verify → extract pipeline
    (``corpus.install_corpus``) over an in-memory ``CorpusSource`` — so
    the production sha256 fail-closed check is exercised, not stubbed.

    ``corrupt=True`` makes the source advertise a wrong sha256; the
    production verify then raises ``CorpusInstallError`` (the fail-closed
    path the install-corpus outcome test asserts). The downloader records
    the version/url it was asked for so tests can assert the CLI wired the
    installed kairix version through.
    """

    def __init__(self, *, corrupt: bool = False, files: dict[str, str] | None = None) -> None:
        self._source = FakeCorpusSource(corrupt=corrupt, files=files)
        self.calls: list[dict[str, Any]] = []

    @property
    def requested_version(self) -> str | None:
        return self._source.requested_version

    @property
    def requested_url(self) -> str | None:
        return self._source.requested_url

    def __call__(
        self,
        *,
        install_dir: Path,
        version: str,
        url: str | None = None,
        force: bool = False,
    ) -> Path:
        from kairix.quality.benchmark.corpus import install_corpus

        self.calls.append({"install_dir": install_dir, "version": version, "url": url, "force": force})
        return install_corpus(
            self._source,
            install_dir=install_dir,
            version=version,
            url=url,
            force=force,
        )


# ---------------------------------------------------------------------------
# F43 parity fakes (PLA-472 slice f43b, helper B) — memory store / OAuth /
# onboarding-scanner contract tests run ONE body over real + fake.
# ---------------------------------------------------------------------------


class FakeMemoryDirSearchPipeline:
    """Index stand-in for ``KairixNativeMemoryStore``'s ``pipeline`` seam.

    The real adapter writes ``<document_root>/memories/<id>.md`` and
    relies on ``kairix embed`` + :class:`SearchPipeline` to make them
    searchable. This fake collapses "embed then search" into a live
    scan of that directory, so the REAL adapter's add/update/delete
    file IO is exercised end-to-end through its public surface.

    Hits are the REAL :class:`SearchPipeline` row shape —
    ``BudgetedResult`` wrapping a ``FusedResult`` whose ``path`` is the
    document-relative ``memories/<id>.md`` — so the adapter's own
    hit → Memory projection (and its id round-trip into update/delete) is
    what's exercised. Scoring matches :class:`FakeMemoryStore`
    (query-word overlap ratio, best first).
    """

    def __init__(self, paths: KairixPaths) -> None:
        self._dir = Path(paths.document_root) / "memories"
        self.calls: list[str] = []

    @staticmethod
    def _body(text: str) -> str:
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end != -1:
                text = text[end + len("\n---\n") :]
        return text.rstrip("\n")

    def search(self, query: str, **kwargs: Any) -> Any:
        del kwargs
        self.calls.append(query)
        q_words = set(query.lower().split())
        from kairix.core.search.budget import BudgetedResult
        from kairix.core.search.rrf import FusedResult

        hits: list[BudgetedResult] = []
        files = sorted(self._dir.glob("*.md")) if self._dir.is_dir() else []
        for md in files:
            body = self._body(md.read_text(encoding="utf-8"))
            overlap = len(q_words & set(body.lower().split()))
            if overlap == 0:
                continue
            score = overlap / max(len(q_words), 1)
            fused = FusedResult(
                path=f"memories/{md.name}", collection="memories", title=md.stem, snippet=body, boosted_score=score
            )
            hits.append(BudgetedResult(result=fused, tier="L0", token_estimate=len(body) // 4, content=body))
        hits.sort(key=lambda h: h.result.boosted_score, reverse=True)
        return _FakeSearchResult(results=list(hits))


class FakeClientCredentialsOAuthConnector:
    """Canonical fake for a client-credentials-only :class:`OAuthConnector`.

    Mirrors the shipped app-only connectors (SharePoint, M365 calendar,
    M365 email headers): there is no three-legged consent flow, so both
    OAuthConnector classmethods raise an actionable
    ``NotImplementedError`` rather than returning a malformed URL or an
    empty token envelope.
    """

    _MESSAGE = (
        "fake_client_credentials: client-credentials flow only; OAuth user flow not supported for this plugin. "
        "fix: drive auth via the configured tenant_id / client_id / client_secret triple. "
        "next: see tests/fakes.py FakeClientCredentialsOAuthConnector."
    )

    @classmethod
    def oauth_authorization_url(cls, _state: str) -> str:
        raise NotImplementedError(cls._MESSAGE)

    @classmethod
    def oauth_code_to_token(cls, _code: str) -> dict[str, Any]:
        raise NotImplementedError(cls._MESSAGE)


class FakeOnboardScanner:
    """In-memory stand-in for :mod:`kairix.agents.onboarding.scanner`.

    Exposes ``scan_for_agents`` / ``discover_single_agent`` with the real
    keyword surface and returns real ``ProposedScope`` values. Models the
    real scanner's no-detector path: every ``memory_root`` subdirectory
    carrying ``.md`` files becomes a ``generic`` / ``medium`` proposal
    with one ``memory`` surface (plus a ``workspace`` surface when
    ``workspace_root/<name>/`` exists). ``detectors`` / ``harness`` are
    accepted for signature parity and ignored. ``discover_single_agent``
    raises ``ValueError`` naming the agent when nothing is found.
    """

    def _propose(self, agent_name: str, candidate: Path, workspace_root: Path | None) -> Any:
        from kairix.agents.onboarding.scanner import ProposedScope
        from kairix.core.agents.scope import AgentSurface

        mds = [p for p in candidate.rglob("*.md") if p.is_file()] if candidate.is_dir() else []
        if not mds:
            return None
        surfaces = [AgentSurface(path=candidate, glob="**/*.md", label="memory")]
        if workspace_root is not None and (workspace_root / agent_name).is_dir():
            ws = workspace_root / agent_name
            surfaces.append(AgentSurface(path=ws, glob="**/*.md", label="workspace"))
            mds.extend(p for p in ws.rglob("*.md") if p.is_file())
        return ProposedScope(
            name=agent_name,
            surfaces=tuple(surfaces),
            harness="generic",
            confidence="medium",
            file_count=len(mds),
            most_recent_mtime=max(p.stat().st_mtime for p in mds),
        )

    def scan_for_agents(
        self,
        *,
        memory_root: Path,
        workspace_root: Path | None = None,
        detectors: Any = None,
    ) -> tuple[Any, ...]:
        del detectors
        children = sorted(memory_root.iterdir()) if memory_root.is_dir() else []
        proposals = [self._propose(c.name, c, workspace_root) for c in children if c.is_dir()]
        return tuple(sorted((p for p in proposals if p is not None), key=lambda p: p.name))

    def discover_single_agent(
        self,
        agent_name: str,
        *,
        memory_root: Path,
        workspace_root: Path | None = None,
        harness: str | None = None,
        detectors: Any = None,
    ) -> Any:
        del harness, detectors
        proposal = self._propose(agent_name, memory_root / agent_name, workspace_root)
        if proposal is None:
            raise ValueError(f"no detector proposed surfaces for agent {agent_name!r} under {memory_root}")
        return proposal


class FakeSuggestionFilter:
    """In-memory :class:`kairix.knowledge.entities.protocols.SuggestionFilter`.

    Mirrors the shipped filter strategies' shape (``RolePhraseFilter``,
    ``KnownEntityAllowlist``, ...): ``apply`` returns a NEW list and drops
    every suggestion whose ``text`` is in ``drop_texts``; everything else
    passes through unchanged. ``raises=`` makes ``apply`` raise verbatim —
    the same propagation a real filter's code-level error has through
    :class:`~kairix.knowledge.entities.filters.ChainedSuggestionFilter`.
    """

    def __init__(
        self,
        *,
        drop_texts: tuple[str, ...] = (),
        raises: BaseException | None = None,
    ) -> None:
        self._drop_texts = frozenset(drop_texts)
        self._raises = raises
        self.calls: list[tuple[list[Any], str]] = []

    def apply(self, suggestions: list[Any], context: str) -> list[Any]:
        self.calls.append((list(suggestions), context))
        if self._raises is not None:
            raise self._raises
        return [s for s in suggestions if s.get("text", "") not in self._drop_texts]


class FakeKeyVaultSecretClient:
    """Stand-in for ``azure.keyvault.secrets.SecretClient``.

    Plugs into the ``client_factory=`` seam of
    :class:`kairix.connect.store.azure_kv_store.AzureKeyVaultTokenStore`
    via :meth:`factory`. Records every ``set_secret`` call in
    ``secrets``; ``raises=`` makes ``set_secret`` raise (a vault that
    rejects the write — e.g. the identity only holds Secrets User).
    """

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self._raises = raises
        self.secrets: dict[str, str] = {}
        self.vault_urls: list[str] = []

    def factory(self, vault_url: str, credential: object) -> FakeKeyVaultSecretClient:
        del credential
        self.vault_urls.append(vault_url)
        return self

    def set_secret(self, name: str, value: str) -> None:
        if self._raises is not None:
            raise self._raises
        self.secrets[name] = value


class FakeVectorIndex:
    """In-memory stand-in for :class:`kairix.core.search.vec_index.VectorIndex`.

    The ``index=`` seam of
    :class:`kairix.core.search.vector_repository.UsearchVectorRepository`.
    ``search`` returns the configured ``results`` (collection-filtered and
    ``k``-capped like the real index); per-method ``*_raises=`` knobs make
    ``search`` / ``add_vectors`` / ``__len__`` raise — a corrupt or
    unreachable index handle.
    """

    def __init__(
        self,
        results: list[dict[str, Any]] | None = None,
        *,
        search_raises: BaseException | None = None,
        add_raises: BaseException | None = None,
        len_raises: BaseException | None = None,
    ) -> None:
        self._results: list[dict[str, Any]] = list(results or [])
        self._hash_seqs: list[str] = []
        self._search_raises = search_raises
        self._add_raises = add_raises
        self._len_raises = len_raises

    def search(self, query_vec: Any, k: int = 10, collections: list[str] | None = None) -> list[dict[str, Any]]:
        del query_vec
        if self._search_raises is not None:
            raise self._search_raises
        rows = [r for r in self._results if not collections or r.get("collection") in collections]
        return rows[:k]

    def add_vectors(self, hash_seqs: list[str], vectors: list[list[float]]) -> int:
        del vectors
        if self._add_raises is not None:
            raise self._add_raises
        self._hash_seqs.extend(hash_seqs)
        return len(hash_seqs)

    def __len__(self) -> int:
        if self._len_raises is not None:
            raise self._len_raises
        return len(self._hash_seqs) + len(self._results)


class FakeChunker:
    """Generic in-memory :class:`kairix.core.protocols.Chunker`.

    Models the contract the shipped chunker plugins share: blank text
    yields ``()``; text within ``max_tokens_per_chunk`` (whitespace word
    count — the shipped chunkers' token proxy) is emitted verbatim as one
    chunk; over-cap text is sub-split into cap-sized word runs. Every chunk
    carries ``source_uri`` (F39) and ``chunker_version=self.version`` (F55).
    The constructor rejects a non-positive cap with a ``ValueError`` naming
    the parameter, like the real chunkers.
    """

    version: str = "0.0.0-fake"

    def __init__(self, *, max_tokens_per_chunk: int = 500) -> None:
        if max_tokens_per_chunk <= 0:
            raise ValueError(
                "FakeChunker: max_tokens_per_chunk must be > 0. fix: pass a positive integer.",
            )
        self._cap = max_tokens_per_chunk
        self.calls: list[dict[str, str]] = []

    def chunk(self, *, text: str, section_kind: str, source_uri: str) -> tuple[Any, ...]:
        import hashlib

        from kairix.core.protocols import Chunk

        self.calls.append({"text": text, "section_kind": section_kind, "source_uri": source_uri})
        tokens = text.split()
        if not tokens:
            return ()
        if len(tokens) <= self._cap:
            pieces: list[str] = [text.strip()]
        else:
            pieces = [" ".join(tokens[i : i + self._cap]) for i in range(0, len(tokens), self._cap)]
        return tuple(
            Chunk(
                text=piece,
                content_hash=hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                source_name="",
                source_uri=source_uri,
                source_modified_at="",
                source_page=None,
                sensitivity="internal",
                chunker_version=self.version,
            )
            for piece in pieces
        )


class FakeNeo4jDriverClass:
    """Stand-in for the ``neo4j.GraphDatabase`` class injected through
    :class:`kairix.knowledge.graph.client.Neo4jClient`'s ``driver_cls=`` seam.

    ``driver(uri, auth=...)`` returns a :class:`FakeNeo4jDriver`. Knobs:

      * ``rows`` — result rows returned by every ``session.run``.
      * ``responder`` — ``(query, params) -> rows`` callable; wins over
        ``rows`` so a test can model query-dependent results (e.g. a
        name-filtered lookup).
      * ``run_raises`` — every ``session.run`` raises this (backend
        error / query failure). Constraint-init queries issued at connect
        time are exempt so the client still connects.
      * ``connect_raises`` — ``verify_connectivity`` raises this (Neo4j
        unreachable), leaving the client ``available is False``.

    Records every ``(query, params, access_mode)`` in ``run_calls``.
    """

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        responder: Any = None,
        run_raises: BaseException | None = None,
        connect_raises: BaseException | None = None,
    ) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])
        self.responder = responder
        self.run_raises = run_raises
        self.connect_raises = connect_raises
        self.run_calls: list[tuple[str, dict[str, Any], str | None]] = []

    def driver(self, uri: str, auth: Any = None) -> FakeNeo4jDriver:
        del uri, auth
        return FakeNeo4jDriver(self)


class FakeNeo4jDriver:
    """Driver returned by :meth:`FakeNeo4jDriverClass.driver`."""

    def __init__(self, owner: FakeNeo4jDriverClass) -> None:
        self._owner = owner

    def verify_connectivity(self) -> None:
        if self._owner.connect_raises is not None:
            raise self._owner.connect_raises

    def session(self, default_access_mode: str | None = None) -> FakeNeo4jSession:
        return FakeNeo4jSession(self._owner, default_access_mode)

    def close(self) -> None:
        """Intentionally empty — no live connection to release."""


class FakeNeo4jSession:
    """Context-managed session returned by :meth:`FakeNeo4jDriver.session`."""

    def __init__(self, owner: FakeNeo4jDriverClass, access_mode: str | None) -> None:
        self._owner = owner
        self._access_mode = access_mode

    def __enter__(self) -> FakeNeo4jSession:
        return self

    def __exit__(self, *exc: object) -> None:
        del exc

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self._owner.run_calls.append((query, dict(params), self._access_mode))
        if query.lstrip().upper().startswith("CREATE CONSTRAINT"):
            return []
        if self._owner.run_raises is not None:
            raise self._owner.run_raises
        if self._owner.responder is not None:
            return list(self._owner.responder(query, dict(params)))
        return list(self._owner.rows)


class FakeHarnessDetector:
    """In-memory :class:`kairix.core.agents.detectors.HarnessDetector`.

    Mirrors the shipped detectors' contract: a missing / non-directory
    candidate root or one carrying none of ``markers`` yields ``()``
    (never ``None``, never a raise); otherwise one ``memory``
    :class:`AgentSurface` anchored at the candidate root.
    """

    def __init__(self, *, name: str = "fake-harness", markers: tuple[str, ...] = ("FAKE.md",)) -> None:
        self.name = name
        self._markers = markers

    def propose_surfaces(self, agent_name: str, candidate_root: Path) -> tuple[Any, ...]:
        del agent_name
        from kairix.core.agents.scope import AgentSurface

        if not candidate_root.is_dir():
            return ()
        if not any((candidate_root / marker).exists() for marker in self._markers):
            return ()
        return (AgentSurface(path=candidate_root, glob="**/*.md", label="memory"),)


class FakePromptGradingChatBackend:
    """``ChatBackend`` that grades the relevance-judge prompt by stem.

    Reads the ``[<label>] <stem>: <document>`` lines the real
    :class:`kairix.quality.eval.judge.LLMJudge` renders and replies with a
    JSON object mapping each label whose stem appears in ``grades_by_stem``
    to its grade — stems absent from the map are omitted from the reply
    (a partial LLM answer). Deterministic regardless of the judge's
    candidate shuffle.
    """

    def __init__(self, *, grades_by_stem: dict[str, int]) -> None:
        self._grades_by_stem = dict(grades_by_stem)
        self.calls: list[str] = []

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
        import json
        import re

        del api_key, endpoint, deployment, system, temperature, timeout_s
        self.calls.append(prompt)
        reply: dict[str, int] = {}
        for label, stem in re.findall(r"^\[([A-Z])\] (.+?): <document>", prompt, re.MULTILINE):
            if stem in self._grades_by_stem:
                reply[label] = self._grades_by_stem[stem]
        return json.dumps(reply)


# ---------------------------------------------------------------------------
# F43 limb-2 parity support (PLA-472, slice f43b-C) — faithful fakes + scripted
# wire stubs that let the failure-mode / protocol contract tests run ONE
# assertion body over the real production impl AND the fake.
# ---------------------------------------------------------------------------


class FakeScoringStrategy:
    """Faithful :class:`kairix.core.protocols.ScoringStrategy` fake.

    Mirrors the shipped scorers (``NDCGScorer`` / ``ExactMatchScorer``): an
    empty ``retrieved`` list or an empty ``gold`` list scores ``0.0`` ("nothing
    to score"); otherwise the configured ``score``. ``raises=`` makes every call
    raise (a scorer whose gold parse / backend crashed). Distinct from
    :class:`FakeScorer`, which returns its fixed score unconditionally.
    """

    def __init__(self, *, score: float = 1.0, raises: BaseException | None = None) -> None:
        self._score = score
        self._raises = raises

    def score(self, retrieved: list[str], gold: list[dict[str, Any]]) -> float:
        if self._raises is not None:
            raise self._raises
        if not retrieved or not gold:
            return 0.0
        return self._score


class FakeSlideChunker:
    """Faithful single-page :class:`kairix.core.protocols.Chunker` for the slide plugin.

    Mirrors :class:`kairix.chunkers.slide.SlideChunker` on Silver's per-page
    path: blank text yields ``()``; any other text collapses to exactly one
    chunk carrying the stripped text, ``source_uri`` (F39) and
    ``chunker_version=self.version`` (F55). Multi-slide header splitting is
    deliberately not modelled.
    """

    def __init__(self, *, name: str = "slide", version: str = "0.0.0-fake-slide") -> None:
        self.name = name
        self.version = version

    def chunk(self, *, text: str, section_kind: str, source_uri: str) -> tuple[Any, ...]:
        import hashlib

        from kairix.core.protocols import Chunk

        del section_kind
        stripped = text.strip()
        if not stripped:
            return ()
        return (
            Chunk(
                text=stripped,
                content_hash=hashlib.sha256(stripped.encode("utf-8")).hexdigest(),
                source_name="",
                source_uri=source_uri,
                source_modified_at="",
                source_page=1,
                sensitivity="internal",
                chunker_version=self.version,
                metadata={"slide_number": "1", "slide_title": ""},
            ),
        )


class FakeSheetRowChunker:
    """Faithful :class:`kairix.core.protocols.Chunker` for the sheet-row plugin.

    Mirrors :class:`kairix.chunkers.sheet_row.SheetRowChunker`: blank text or a
    table with no data rows yields ``()``; a sheet with at most
    ``small_sheet_threshold`` data rows becomes one whole-sheet chunk; larger
    sheets emit one ``header + separator + row`` chunk per data row. Every
    chunk carries ``source_uri`` (F39) and ``chunker_version`` (F55).
    """

    def __init__(
        self,
        *,
        small_sheet_threshold: int = 50,
        name: str = "sheet_row",
        version: str = "0.0.0-fake-sheet-row",
    ) -> None:
        self.name = name
        self.version = version
        self.small_sheet_threshold = small_sheet_threshold

    def chunk(self, *, text: str, section_kind: str, source_uri: str) -> tuple[Any, ...]:
        import hashlib

        from kairix.core.protocols import Chunk

        del section_kind
        stripped = text.strip()
        rows = [line for line in stripped.splitlines() if line.lstrip().startswith("|")]
        if len(rows) < 3:
            return ()
        header, separator, data_rows = rows[0], rows[1], rows[2:]
        if len(data_rows) <= self.small_sheet_threshold:
            pieces = [stripped]
        else:
            pieces = ["\n".join([header, separator, row]) for row in data_rows]
        return tuple(
            Chunk(
                text=piece,
                content_hash=hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                source_name="",
                source_uri=source_uri,
                source_modified_at="",
                source_page=None,
                sensitivity="internal",
                chunker_version=self.version,
            )
            for piece in pieces
        )


class FakeChunkerRegistry:
    """In-memory chunker registry (the ``DefaultSilverProcessor(chunker_registry=)`` seam).

    ``dispatch`` returns the configured chunker (default :class:`FakeChunker`)
    and records every ``(kind, mime, section_kind)`` request. ``raises=`` makes
    ``dispatch`` raise — the seam for injecting a Silver chunking failure into
    the real processor.
    """

    def __init__(self, *, chunker: Any | None = None, raises: BaseException | None = None) -> None:
        self._chunker = chunker
        self._raises = raises
        self.dispatch_calls: list[tuple[str, str, str]] = []

    def dispatch(self, *, kind: str, mime: str, section_kind: str) -> Any:
        self.dispatch_calls.append((kind, mime, section_kind))
        if self._raises is not None:
            raise self._raises
        return self._chunker if self._chunker is not None else FakeChunker()


class FakeSilverProcessor:
    """Faithful :class:`kairix.core.protocols.SilverProcessor` fake.

    Mirrors :class:`kairix.core.connectors.silver.DefaultSilverProcessor` at
    the contract level: blank markdown yields ``SilverOutput(chunks=(),
    entity_signals=())``; non-blank markdown yields one chunk per blank-line
    separated paragraph carrying ``source_uri`` / ``source_modified_at`` /
    ``sensitivity`` (F39). ``raises=`` makes ``process`` raise (a chunking or
    signal-extraction crash).
    """

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self._raises = raises
        self.calls: list[str] = []

    def process(
        self,
        raw: Any,
        extracted: Any,
        source_uri: str,
        source_modified_at: str,
        sensitivity: Any,
        connector_metadata: Any | None = None,
        extractor_metadata: Any | None = None,
        extractor_name: str | None = None,
        extractor_version: str | None = None,
        extraction_status: str = "ok",
    ) -> Any:
        import hashlib

        from kairix.core.protocols import Chunk, SilverOutput

        del connector_metadata, extractor_metadata, extractor_name, extractor_version, extraction_status
        self.calls.append(source_uri)
        if self._raises is not None:
            raise self._raises
        paragraphs = [p.strip() for p in str(extracted.markdown).split("\n\n") if p.strip()]
        chunks = tuple(
            Chunk(
                text=paragraph,
                content_hash=hashlib.sha256(paragraph.encode("utf-8")).hexdigest(),
                source_name=raw.source_name,
                source_uri=source_uri,
                source_modified_at=source_modified_at,
                source_page=None,
                sensitivity=sensitivity,
                chunker_version="fake-silver",
            )
            for paragraph in paragraphs
        )
        return SilverOutput(chunks=chunks, entity_signals=())


class FakeSlackWebApi:
    """Scripted Slack Web API behind ``httpx.MockTransport`` for real-connector legs.

    ``responses`` maps a Web API method name (``"conversations.history"``)
    to either a JSON payload dict (merged over an ``ok: true`` empty-page
    default) or an ``int`` HTTP status. Unscripted methods answer
    ``ok: true`` with empty ``messages`` / ``members`` / ``channels``.
    :meth:`build_connector` returns a real
    :class:`kairix.connectors.slack.SlackConnector` wired to this stub through
    its ``web_client_factory`` seam — no real Slack call is ever made.
    """

    _TOKEN = "xoxb-test-fake-token-value"  # pragma: allowlist secret — fixture value

    def __init__(self, *, responses: dict[str, Any] | None = None) -> None:
        self._responses = dict(responses or {})
        self.calls: list[str] = []

    def handle(self, request: Any) -> Any:
        import httpx

        method = str(request.url).rsplit("/", 1)[-1]
        self.calls.append(method)
        scripted = self._responses.get(method)
        if isinstance(scripted, int):
            return httpx.Response(scripted, json={"ok": False})
        payload: dict[str, Any] = {
            "ok": True,
            "messages": [],
            "members": [],
            "channels": [],
            "response_metadata": {"next_cursor": ""},
        }
        if scripted:
            payload.update(scripted)
        return httpx.Response(200, json=payload)

    def build_connector(self) -> Any:
        import httpx

        from kairix.connectors.slack import SlackConnector, SlackCredentials, SlackWebClient

        shared = httpx.Client(transport=httpx.MockTransport(self.handle))

        def _builder(_credentials: Any) -> Any:
            return SlackWebClient(token=self._TOKEN, http_client=shared)

        return SlackConnector(credentials=SlackCredentials(bot_token=self._TOKEN), web_client_factory=_builder)


class FakeSharePointGraphApi:
    """Scripted Microsoft Graph behind ``httpx.MockTransport`` for real-connector legs.

    ``items`` are drive-item dicts (``id`` / ``name`` / ``mime`` /
    ``content`` bytes) emitted on the delta page; ``/content`` for an id in
    ``content_failures`` answers that HTTP status (``int``) or raises that
    exception (e.g. ``httpx.ReadTimeout``). :meth:`build_connector` returns a
    real :class:`kairix.connectors.sharepoint.SharePointConnector` wired through
    its ``auth`` / ``client_builder`` seams with a no-op retry sleeper.
    """

    drive_id = "b!drive-fake-graph"
    _SECRET = "fake-secret-value"  # pragma: allowlist secret — fixture value

    def __init__(
        self,
        *,
        items: list[dict[str, Any]] | None = None,
        content_failures: dict[str, Any] | None = None,
    ) -> None:
        self._items = list(items or [])
        self._content_failures = dict(content_failures or {})

    def _delta_page(self) -> dict[str, Any]:
        return {
            "value": [
                {
                    "id": item["id"],
                    "name": item.get("name", f"{item['id']}.md"),
                    "lastModifiedDateTime": "2026-05-22T10:00:00Z",
                    "webUrl": f"https://contoso.sharepoint.com/sites/team/Documents/{item['id']}",
                    "file": {"mimeType": item.get("mime", "text/markdown")},
                    "parentReference": {"driveId": self.drive_id},
                    "size": len(item.get("content", b"")),
                }
                for item in self._items
            ],
            "@odata.deltaLink": f"https://graph.microsoft.com/v1.0/drives/{self.drive_id}/root/delta?token=fake",
        }

    def handle(self, request: Any) -> Any:
        import httpx

        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(200, json={"access_token": "fake-bearer", "expires_in": 3600, "token_type": "Bearer"})
        if url.endswith("/content"):
            for item in self._items:
                if f"/items/{item['id']}/content" not in url:
                    continue
                failure = self._content_failures.get(item["id"])
                if isinstance(failure, BaseException):
                    raise failure
                if isinstance(failure, int):
                    return httpx.Response(failure)
                return httpx.Response(200, content=item.get("content", b""))
            return httpx.Response(404)
        return httpx.Response(200, json=self._delta_page())

    def build_connector(self) -> Any:
        import httpx

        from kairix.connectors.sharepoint import (
            SharePointConnector,
            SharePointCredentials,
            SharePointDriveSpec,
            SharePointGraphClient,
        )
        from kairix.transport.auth.oauth2_client_creds import OAuth2ClientCredsAuth

        shared = httpx.Client(transport=httpx.MockTransport(self.handle))
        auth = OAuth2ClientCredsAuth(
            tenant_id="fake-tenant",
            client_id="fake-client",
            client_secret=self._SECRET,
            scope="https://graph.microsoft.com/.default",
            http_client=shared,
        )
        return SharePointConnector(
            drives=[SharePointDriveSpec(drive_id=self.drive_id)],
            credentials=SharePointCredentials(
                tenant_id="fake-tenant", client_id="fake-client", client_secret=self._SECRET
            ),
            auth=auth,
            client_builder=lambda a: SharePointGraphClient(auth=a, http_client=shared, sleep_fn=lambda _s: None),
        )


class _RaisingIterator:
    """Iterator that raises ``exc`` on first ``next()`` — lazy failure like a real paged listing."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __iter__(self) -> _RaisingIterator:
        return self

    def __next__(self) -> Any:
        raise self._exc


def _raising_iter(exc: BaseException) -> Iterator[Any]:
    """Return an iterator whose first ``next()`` raises ``exc`` (deferred, not at call time)."""
    return _RaisingIterator(exc)
