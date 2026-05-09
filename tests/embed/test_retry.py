"""Unit tests for ``embed_batch`` using a fake OpenAI client.

Tests inject a ``_FakeOpenAIClient`` via the ``client=`` kwarg on
``embed_batch``. No ``with patch("openai.AzureOpenAI")``, no module-state
mutation, no autouse cache-reset fixture — the fake bypasses the cached
production client entirely.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.core.embed.embed import embed_batch


class _StubHTTPRequest:
    """Stub for ``response.request`` accessed by openai's exception ctor."""


class _StubHTTPResponse:
    """Minimal HTTPResponse-shaped object for openai.BadRequestError(response=...).

    The openai exception constructor reads ``response.request`` and
    ``response.headers.get("x-request-id")`` — both attributes are stubbed
    here. Status code is exposed for completeness.
    """

    def __init__(self, status_code: int = 400) -> None:
        self.status_code = status_code
        self.request = _StubHTTPRequest()
        self.headers: dict[str, str] = {}


API_KEY = "test-key"  # pragma: allowlist secret
ENDPOINT = "https://test.openai.azure.com"
DEPLOYMENT = "text-embedding-3-large"
DIMS = 1536

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake OpenAI client — mirrors the surface ``embed_batch`` consumes:
# ``client.embeddings.create(model=..., input=[...], dimensions=...).data``
# is a list of items each carrying ``.index`` and ``.embedding``.
# ---------------------------------------------------------------------------


class _EmbeddingItem:
    def __init__(self, index: int, value: float = 0.1) -> None:
        self.index = index
        self.embedding = [value] * DIMS


class _EmbeddingsResponse:
    def __init__(self, items: list[_EmbeddingItem]) -> None:
        self.data = items


class _Embeddings:
    def __init__(self, owner: _FakeOpenAIClient) -> None:
        self._owner = owner

    def create(self, *, model: str, input: list[str], dimensions: int) -> _EmbeddingsResponse:
        self._owner.calls.append({"model": model, "input": list(input), "dimensions": dimensions})
        return self._owner._respond(input)


class _FakeOpenAIClient:
    """Configurable test double for the OpenAI client surface used by ``embed_batch``.

    ``responder`` is a callable taking the input list and returning either an
    ``_EmbeddingsResponse`` (success) or raising an exception (error path).
    Defaults to a deterministic vector per input item.
    """

    def __init__(self, responder: Any | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responder = responder or self._default_responder
        self.embeddings = _Embeddings(self)

    def _respond(self, texts: list[str]) -> _EmbeddingsResponse:
        return self._responder(texts)

    @staticmethod
    def _default_responder(texts: list[str]) -> _EmbeddingsResponse:
        return _EmbeddingsResponse([_EmbeddingItem(i, 0.1 * (i + 1)) for i in range(len(texts))])


# ---------------------------------------------------------------------------
# embed_batch — happy paths
# ---------------------------------------------------------------------------


class TestEmbedBatch:
    def test_returns_one_vector_per_input_text(self) -> None:
        client = _FakeOpenAIClient()
        result = embed_batch(["hello", "world"], API_KEY, ENDPOINT, DEPLOYMENT, DIMS, client=client)
        assert len(result) == 2
        assert all(len(v) == DIMS for v in result)

    def test_empty_input_returns_empty_list_without_calling_client(self) -> None:
        client = _FakeOpenAIClient()
        result = embed_batch([], API_KEY, ENDPOINT, DEPLOYMENT, DIMS, client=client)
        assert result == []
        assert client.calls == []

    def test_results_are_reordered_by_response_index(self) -> None:
        """The OpenAI response may return items out of order; embed_batch sorts by .index."""

        def out_of_order(texts: list[str]) -> _EmbeddingsResponse:
            # Hand back items shuffled — embed_batch must sort by .index.
            return _EmbeddingsResponse(
                [
                    _EmbeddingItem(2, 0.3),
                    _EmbeddingItem(0, 0.1),
                    _EmbeddingItem(1, 0.2),
                ]
            )

        client = _FakeOpenAIClient(responder=out_of_order)
        result = embed_batch(["a", "b", "c"], API_KEY, ENDPOINT, DEPLOYMENT, DIMS, client=client)
        assert result[0][0] == pytest.approx(0.1)
        assert result[1][0] == pytest.approx(0.2)
        assert result[2][0] == pytest.approx(0.3)

    def test_passes_dimensions_kwarg_to_client(self) -> None:
        """``dims`` is forwarded as ``dimensions=`` on the create call."""
        client = _FakeOpenAIClient()
        embed_batch(["x"], API_KEY, ENDPOINT, DEPLOYMENT, dims=512, client=client)
        assert client.calls[0]["dimensions"] == 512


# ---------------------------------------------------------------------------
# embed_batch — BadRequestError split-and-recurse logic
# ---------------------------------------------------------------------------


class TestEmbedBatchBadRequestSplit:
    def test_bad_request_on_multi_item_batch_splits_and_retries_each_half(self) -> None:
        """A multi-item batch that fails with BadRequestError is split in half;
        each half re-runs through the same client. Two single-item retries
        succeed → 1 failed call + 2 retries = 3 client calls.
        """
        import openai

        def split_responder(texts: list[str]) -> _EmbeddingsResponse:
            if len(texts) > 1:
                raise openai.BadRequestError(
                    message="batch too large",
                    response=_StubHTTPResponse(status_code=400),
                    body=None,
                )
            return _EmbeddingsResponse([_EmbeddingItem(i, 0.1) for i in range(len(texts))])

        client = _FakeOpenAIClient(responder=split_responder)
        result = embed_batch(["a", "b"], API_KEY, ENDPOINT, DEPLOYMENT, DIMS, client=client)

        assert len(result) == 2
        # 1 multi-item call (failed) + 2 single-item retries = 3 total client invocations.
        assert len(client.calls) == 3
        # Single-item retries each carried exactly one input.
        single_calls = [c for c in client.calls if len(c["input"]) == 1]
        assert len(single_calls) == 2

    def test_bad_request_on_single_item_batch_propagates(self) -> None:
        """When the batch is already a single item, BadRequestError is not retried."""
        import openai

        def always_bad_request(_texts: list[str]) -> _EmbeddingsResponse:
            raise openai.BadRequestError(
                message="bad input",
                response=_StubHTTPResponse(status_code=400),
                body=None,
            )

        client = _FakeOpenAIClient(responder=always_bad_request)
        with pytest.raises(openai.BadRequestError):
            embed_batch(["a"], API_KEY, ENDPOINT, DEPLOYMENT, DIMS, client=client)
