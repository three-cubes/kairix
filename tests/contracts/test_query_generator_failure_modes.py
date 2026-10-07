"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`QueryGenerator`.

One method (``generate``). Failure surface:

  * ``returns_empty`` — generator returns 0 queries when the LLM produces
    nothing usable for the title (empty array, sanitiser filters
    everything).
  * ``returns_partial`` — generator returns fewer than ``n`` queries
    when the LLM produces fewer (the "0..n" Protocol contract).

F43: every test runs ONE assertion body over the real
:class:`kairix.quality.eval.generate.QueryGenerator` (driven by a
:class:`tests.fakes.FakeChatBackend` through its ``chat_backend`` seam)
AND :class:`tests.fakes.FakeQueryGenerator`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest

from kairix.quality.eval.generate import QueryGenerator
from tests.fakes import FakeChatBackend, FakeQueryGenerator

pytestmark = pytest.mark.contract

_TITLE = "deploy.md"

GenerateFn = Callable[..., list[Any]]


def _real(queries: list[tuple[str, str]]) -> GenerateFn:
    """Real generator whose LLM answers with ``queries`` as the JSON array."""
    payload = json.dumps([{"query": text, "intent": intent} for text, intent in queries])
    gen = QueryGenerator(chat_backend=FakeChatBackend(responses=[payload, payload]))
    # Credentials are per-call kwargs on the real impl (kept out of fixture state).
    return partial(gen.generate, api_key="fake-key", endpoint="https://fake.endpoint")


def _fake(queries: list[tuple[str, str]]) -> GenerateFn:
    """Fake generator pre-configured with ``queries`` for :data:`_TITLE`."""
    configured = [SimpleNamespace(text=text, intent=intent) for text, intent in queries]
    gen = FakeQueryGenerator(queries_by_title={_TITLE: configured} if configured else {})
    return gen.generate


_IMPLS = [_real, _fake]


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_generate_returns_empty_when_no_queries_configured(
    factory: Callable[[list[tuple[str, str]]], GenerateFn],
) -> None:
    """When the generator has nothing usable for the title it yields an
    empty list — it must not invent queries.

    Sabotage proof: in ``kairix.quality.eval.generate.parse_llm_query_response``
    change the final ``return queries`` to
    ``return queries or [GeneratedQuery(query="phantom", ...)]``.
    Re-run: the real leg's ``== []`` assertion fails. Restored.
    """
    generate = factory([])
    out = generate(_TITLE, "body content", n=3, categories=["semantic"])
    assert out == [], f"nothing usable must yield []; got {out!r}"


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_generate_returns_partial_when_configured_count_below_n(
    factory: Callable[[list[tuple[str, str]]], GenerateFn],
) -> None:
    """When fewer queries than n are available, the generator returns
    the available ones — the Protocol contract is "0..n", not
    "exactly n".

    Sabotage proof: in ``kairix.quality.eval.generate.QueryGenerator.generate``
    pad the result (``return (lambda out: out + out * n)(generate_queries(...))``).
    Re-run: the real leg's ``len == 1`` assertion fails because it now
    over-returns. Restored.
    """
    generate = factory([("q1", "semantic")])
    out = generate(_TITLE, "body content", n=5, categories=["semantic"])
    assert len(out) == 1, f"asked for n=5, only 1 available; got {len(out)}"
