"""Unit tests for ``kairix.use_cases.prep.run_prep``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.core.search.scope import Scope
from kairix.use_cases.prep import (
    PrepDeps,
    PrepOutput,
    default_chat_callable,
    default_search_callable,
    prep_output_to_envelope,
    run_prep,
)

pytestmark = pytest.mark.unit


@dataclass
class _FakeInner:
    title: str = ""
    path: str = ""


@dataclass
class _FakeBudgeted:
    result: _FakeInner
    content: str = ""


@dataclass
class _FakeSearchResult:
    results: list[_FakeBudgeted] = field(default_factory=list)


def _build_deps(
    *,
    sr: _FakeSearchResult | None = None,
    summary: str = "",
    search_raises: bool = False,
    chat_raises: bool = False,
) -> tuple[PrepDeps, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def fake_search(**kwargs: Any) -> _FakeSearchResult:
        captured["search"] = kwargs
        if search_raises:
            raise RuntimeError("search down")
        return sr or _FakeSearchResult()

    def fake_chat(**kwargs: Any) -> str:
        captured["chat"] = kwargs
        if chat_raises:
            raise RuntimeError("chat down")
        return summary

    return PrepDeps(search_fn=fake_search, chat_fn=fake_chat), captured


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


_LONG_SNIPPET = "Alpha is a sample document discussing the topic in detail across paragraphs."


def test_l0_summary_calls_chat_with_concise_system_message() -> None:
    sr = _FakeSearchResult(results=[_FakeBudgeted(result=_FakeInner(title="doc-a", path="/a"), content=_LONG_SNIPPET)])
    deps, captured = _build_deps(sr=sr, summary="brief alpha summary")
    out = run_prep("topic", tier="l0", deps=deps)

    assert out.error == ""
    assert out.tier == "l0"
    assert out.summary == "brief alpha summary"
    assert out.tokens > 0
    assert out.sources == ["doc-a"]
    # The system prompt mentions "2-3 sentences" for l0 only.
    assert "2-3 sentences" in captured["chat"]["messages"][0]["content"]
    # Search budget is 1500 for l0.
    assert captured["search"]["budget"] == 1500
    # Chat max_tokens is 150 for l0.
    assert captured["chat"]["max_tokens"] == 150


def test_l1_summary_uses_structured_overview_prompt() -> None:
    sr = _FakeSearchResult(results=[_FakeBudgeted(result=_FakeInner(title="doc-b"), content=_LONG_SNIPPET)])
    deps, captured = _build_deps(sr=sr, summary="structured overview")
    out = run_prep("topic", tier="l1", deps=deps)

    assert out.tier == "l1"
    assert "structured overview" in captured["chat"]["messages"][0]["content"]
    assert captured["search"]["budget"] == 3000
    assert captured["chat"]["max_tokens"] == 600


def test_no_results_returns_no_documents_message_no_chat_call() -> None:
    deps, captured = _build_deps(sr=_FakeSearchResult(results=[]))
    out = run_prep("obscure topic", deps=deps)
    assert "No relevant documents" in out.summary
    assert out.error == ""
    assert "chat" not in captured  # chat must not run when there's no context


def test_sources_use_path_when_title_empty() -> None:
    sr = _FakeSearchResult(
        results=[_FakeBudgeted(result=_FakeInner(title="", path="/notes/foo.md"), content=_LONG_SNIPPET)]
    )
    deps, _ = _build_deps(sr=sr, summary="ok")
    out = run_prep("topic", deps=deps)
    assert out.sources == ["/notes/foo.md"]


def test_only_top_5_results_become_sources() -> None:
    big = [_FakeBudgeted(result=_FakeInner(title=f"d{i}"), content=_LONG_SNIPPET) for i in range(8)]
    sr = _FakeSearchResult(results=big)
    deps, _ = _build_deps(sr=sr, summary="ok")
    out = run_prep("topic", deps=deps)
    assert len(out.sources) == 5


def test_thin_snippets_filtered_no_chat_call() -> None:
    """#254: title-only hits (empty / very short content) must not reach the LLM.

    When search returns matches but every snippet is title-equivalent (e.g.
    a frontmatter-only doc or stripped index page), calling the LLM with
    effectively zero grounding produces hallucinated 'generic filler'
    summaries. Thin hits are filtered upstream and the use case returns
    the no-results sentinel instead.
    """
    sr = _FakeSearchResult(
        results=[
            _FakeBudgeted(result=_FakeInner(title="doc-a"), content="see ref-001"),
            _FakeBudgeted(result=_FakeInner(title="doc-b"), content=""),
            _FakeBudgeted(result=_FakeInner(title="doc-c"), content="x"),
        ]
    )
    deps, captured = _build_deps(sr=sr, summary="should not be called")
    out = run_prep("topic", deps=deps)
    assert "No relevant documents" in out.summary
    assert out.error == ""
    assert "chat" not in captured, "LLM must not be called when all snippets are thin"


def test_thin_snippets_dropped_but_one_useful_snippet_reaches_chat() -> None:
    """Mixed-quality results: thin hits dropped, real hits forwarded to chat."""
    sr = _FakeSearchResult(
        results=[
            _FakeBudgeted(result=_FakeInner(title="doc-thin"), content="see ref-001"),
            _FakeBudgeted(result=_FakeInner(title="doc-real"), content=_LONG_SNIPPET),
        ]
    )
    deps, captured = _build_deps(sr=sr, summary="real summary")
    out = run_prep("topic", deps=deps)
    assert out.summary == "real summary"
    # Only the substantive hit is sourced — thin hit was dropped before context-build.
    assert out.sources == ["doc-real"]
    # The chat prompt mentions doc-real but not the thin doc.
    user_msg = captured["chat"]["messages"][1]["content"]
    assert "doc-real" in user_msg
    assert "doc-thin" not in user_msg


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_search_failure_yields_error_envelope() -> None:
    deps, _ = _build_deps(search_raises=True)
    out = run_prep("anything", deps=deps)
    assert out.error.startswith("RuntimeError:")
    assert out.summary == ""


def test_chat_failure_yields_error_envelope() -> None:
    sr = _FakeSearchResult(results=[_FakeBudgeted(result=_FakeInner(title="d"), content=_LONG_SNIPPET)])
    deps, _ = _build_deps(sr=sr, chat_raises=True)
    out = run_prep("topic", deps=deps)
    assert out.error.startswith("RuntimeError:")


# ---------------------------------------------------------------------------
# Pass-through
# ---------------------------------------------------------------------------


def test_agent_and_scope_pass_through() -> None:
    deps, captured = _build_deps()
    run_prep("q", agent="builder", scope=Scope.AGENT, deps=deps)
    assert captured["search"]["agent"] == "builder"
    assert captured["search"]["scope"] is Scope.AGENT


# ---------------------------------------------------------------------------
# Determinism — L0 prefix property
# ---------------------------------------------------------------------------


def test_l0_sources_are_prefix_of_l1_when_same_query_and_deps() -> None:
    """Pin the determinism contract called out by the 2026-05-02 dogfood (#116):
    when L0 and L1 see the same ranked search results, the L0 source list
    must be a prefix of L1's — different counts are expected (different
    budget caps) but the *ordering* of overlapping sources must match.

    Rebuts the dogfood concern that L0 vs L1 produces differently-ranked
    sources for the same query. The use-case implementation takes
    ``results[:5]`` from a deterministic search ranking; budget only
    affects which results survive ``apply_budget``, never the order
    among the survivors.
    """
    # Same SearchResult shape returned for both tiers — what differs is
    # how many BudgetedResults survive the budget cap, not their order.
    full_results = [
        _FakeBudgeted(result=_FakeInner(title=f"doc-{i}"), content=f"{_LONG_SNIPPET} ({i})") for i in range(7)
    ]

    captured: dict[str, Any] = {"l0": None, "l1": None}

    def _make_deps(tier: str) -> PrepDeps:
        # L0 (budget=1500) survives 3 results; L1 (budget=3000) survives 5.
        kept = 3 if tier == "l0" else 5

        def _search(**kwargs: Any) -> _FakeSearchResult:
            captured[tier] = kwargs
            return _FakeSearchResult(results=full_results[:kept])

        return PrepDeps(search_fn=_search, chat_fn=lambda **_: f"summary for {tier}")

    out_l0 = run_prep("topic", tier="l0", deps=_make_deps("l0"))
    out_l1 = run_prep("topic", tier="l1", deps=_make_deps("l1"))

    # L0's source list must be a prefix of L1's — overlap is identically ranked.
    assert out_l0.sources == out_l1.sources[: len(out_l0.sources)], (
        f"L0 sources must prefix L1 sources; got L0={out_l0.sources!r} L1={out_l1.sources!r}"
    )
    # And the budgets we passed are the documented L0/L1 caps.
    assert captured["l0"]["budget"] == 1500
    assert captured["l1"]["budget"] == 3000


# ---------------------------------------------------------------------------
# Envelope projection
# ---------------------------------------------------------------------------


def test_envelope_includes_all_fields() -> None:
    out = PrepOutput(query="q", tier="l1", summary="s", tokens=42, sources=["a", "b"])
    env = prep_output_to_envelope(out)
    assert env == {
        "query": "q",
        "tier": "l1",
        "summary": "s",
        "tokens": 42,
        "sources": ["a", "b"],
        "error": "",
    }


# ---------------------------------------------------------------------------
# default_search_callable — public production adapter
# ---------------------------------------------------------------------------


class _StubPipeline:
    """Pipeline-shaped fake that records the kwargs ``.search`` is called with."""

    def __init__(self, return_value: Any) -> None:
        self._return = return_value
        self.search_calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> Any:
        self.search_calls.append(kwargs)
        return self._return


def test_default_search_callable_invokes_pipeline_with_kwargs() -> None:
    """``default_search_callable`` forwards kwargs through the pipeline factory.

    Drive the production adapter via its public ``pipeline_factory`` seam so
    the search-side branch is exercised end-to-end without touching the real
    factory builder.
    """
    sentinel = _FakeSearchResult()
    stub = _StubPipeline(return_value=sentinel)
    out = default_search_callable(pipeline_factory=lambda: stub, query="topic", budget=1500)

    assert out is sentinel
    assert stub.search_calls == [{"query": "topic", "budget": 1500}]


def test_default_search_callable_propagates_factory_exception() -> None:
    """A failing ``pipeline_factory`` raises into the caller.

    The use-case ``run_prep`` wraps this in its own try/except — but the
    adapter itself must surface the failure rather than swallow it, so the
    upstream caller's error handling sees the real ``RuntimeError``.
    """

    def raising_factory() -> Any:
        raise RuntimeError("factory boom")

    with pytest.raises(RuntimeError, match="factory boom"):
        default_search_callable(pipeline_factory=raising_factory, query="x")


# ---------------------------------------------------------------------------
# default_chat_callable — public production adapter
# ---------------------------------------------------------------------------


class _StubBackend:
    """Backend-shaped fake that records the kwargs ``.chat`` is called with."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.chat_calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> str:
        self.chat_calls.append(kwargs)
        return self._reply


def test_default_chat_callable_resolves_provider_and_forwards_to_backend() -> None:
    """Happy path: name resolves, provider is built, backend.chat is called.

    The ``chat_backend_factory`` kwarg lets the test substitute the
    ProviderChatBackend constructor with a stub so the assertions stay on
    the wiring, not on the production backend.
    """
    seen_provider: list[Any] = []
    stub = _StubBackend(reply="ok")

    def fake_backend_factory(provider: Any) -> _StubBackend:
        seen_provider.append(provider)
        return stub

    out = default_chat_callable(
        provider_name_fn=lambda: "azure_foundry",
        provider_resolver=lambda name: f"provider-for-{name}",
        chat_backend_factory=fake_backend_factory,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=150,
    )

    assert out == "ok"
    assert seen_provider == ["provider-for-azure_foundry"]
    assert stub.chat_calls == [{"messages": [{"role": "user", "content": "hi"}], "max_tokens": 150}]


def test_default_chat_callable_raises_value_error_when_no_provider_configured() -> None:
    """No provider name → ``ValueError`` with the actionable config message.

    Surfaces a missing ``provider:`` field in ``kairix.config.yaml`` at the
    adapter boundary so the operator sees a config error instead of a
    downstream "provider plugin missing" stacktrace.
    """
    with pytest.raises(ValueError, match="provider:"):
        default_chat_callable(
            provider_name_fn=lambda: None,
            provider_resolver=lambda _name: pytest.fail("resolver must not be called"),
            chat_backend_factory=lambda _p: pytest.fail("backend factory must not be called"),
            messages=[],
            max_tokens=10,
        )


# ---------------------------------------------------------------------------
# _format_context — None-result branch
# ---------------------------------------------------------------------------


def test_run_prep_skips_results_with_none_inner_result_object() -> None:
    """A SearchResult entry whose ``.result`` is ``None`` is skipped silently.

    Defensive contract: the search pipeline produces ``BudgetedResult``
    wrappers; if an upstream layer ever emits a wrapper with no inner
    payload the use case must drop that row rather than NPE on it.
    """
    sr = _FakeSearchResult(
        results=[
            _FakeBudgeted(result=None, content=_LONG_SNIPPET),
            _FakeBudgeted(result=_FakeInner(title="doc-real"), content=_LONG_SNIPPET),
        ]
    )
    deps, captured = _build_deps(sr=sr, summary="real summary")
    out = run_prep("topic", deps=deps)

    assert out.error == ""
    assert out.summary == "real summary"
    assert out.sources == ["doc-real"]
    user_msg = captured["chat"]["messages"][1]["content"]
    assert "doc-real" in user_msg


# ---------------------------------------------------------------------------
# PrepDeps defaults — wire-up smoke test
# ---------------------------------------------------------------------------


def test_prep_deps_default_factories_resolve_to_public_callables() -> None:
    """``PrepDeps()`` with no kwargs resolves to the public default adapters.

    Pins the wiring so a future rename of the production callables can't
    silently drop the default factory link.
    """
    deps = PrepDeps()
    assert deps.search_fn is default_search_callable
    assert deps.chat_fn is default_chat_callable
