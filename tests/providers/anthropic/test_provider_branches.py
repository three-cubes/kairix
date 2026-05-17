"""Unit tests for the edge-of-helper branches in :mod:`kairix.providers.anthropic`.

Covers the residual lines below the F7 90% floor in
``kairix/providers/anthropic/provider.py``:

- ``_status_code_of`` second branch: code only on ``err.response.status_code``;
- ``_retry_after_of`` defensive branches:
  - ``response.headers`` is None,
  - ``headers.get()`` raises an exception,
  - ``Retry-After`` value is unparseable as a float;
- ``_is_connection_failure`` class-name branch (``APITimeoutError``);
- ``_extract_text_from_content_blocks`` string + None + non-list shapes;
- ``chat()`` response-is-dict branch (dict shape from the test fakes);
- ``_client()`` lazy default-construction branch +
  ``_build_default_client`` body (lazy ``import anthropic`` + RuntimeError
  affordance when the SDK is missing).

Every branch is driven through the public surface (``chat`` or
``embed_batch`` raising the right typed error, etc.) — no private-name
imports, no ``@patch`` on kairix internals.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import (
    AuthError,
    ProviderError,
    ProviderUnreachable,
    RateLimited,
)
from kairix.providers.anthropic import AnthropicProvider

# ---------------------------------------------------------------------------
# Fake transport surfaces
# ---------------------------------------------------------------------------


@dataclass
class _FakeTextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class _FakeMessagesResponse:
    content: Any = field(default_factory=list)


class _RaisingMessages:
    def __init__(self, err: BaseException) -> None:
        self._err = err

    def create(self, **_kwargs: Any) -> _FakeMessagesResponse:
        raise self._err


class _RaisingAnthropicClient:
    def __init__(self, err: BaseException) -> None:
        self.messages = _RaisingMessages(err)


def _creds() -> Credentials:
    return Credentials(
        api_key="anthropic-test-key",  # pragma: allowlist secret
        endpoint="https://api.anthropic.com",
        model="claude-3-5-sonnet-20241022",
        dims=0,
    )


# ---------------------------------------------------------------------------
# _status_code_of — code only on err.response.status_code
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_code_extracted_from_response_when_top_level_attribute_absent() -> None:
    """Error with only ``response.status_code`` still maps via the 429 path.

    Sabotage-proof: removing the ``response = getattr(err, "response", ...)``
    block in ``_status_code_of`` would make the helper return None and
    the 429-branch in ``_map_transport_error`` wouldn't fire — the
    raised error would be a bare ``ProviderError`` not ``RateLimited``,
    so ``pytest.raises(RateLimited)`` fails.
    """

    class _ResponseShape:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.headers: dict[str, str] = {}

    class _ResponseOnlyError(Exception):
        def __init__(self) -> None:
            self.response = _ResponseShape(429)
            super().__init__("upstream 429 reported via response.status_code")

    provider = AnthropicProvider(
        credentials=_creds(),
        transport_client=_RaisingAnthropicClient(_ResponseOnlyError()),
    )

    with pytest.raises(RateLimited):
        provider.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# _retry_after_of — defensive branches
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retry_after_of_returns_none_when_headers_absent() -> None:
    """No headers attribute on the response → no retry hint.

    Sabotage-proof: removing the ``if headers is None: return None``
    guard makes ``headers.get(...)`` AttributeError on the next line;
    the test would see a bare ProviderError instead of RateLimited.
    """

    class _ResponseNoHeaders:
        def __init__(self) -> None:
            self.status_code = 429
            # No `.headers` at all.

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.status_code = 429
            self.response = _ResponseNoHeaders()
            super().__init__("429 no headers")

    provider = AnthropicProvider(
        credentials=_creds(),
        transport_client=_RaisingAnthropicClient(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


@pytest.mark.unit
def test_retry_after_of_returns_none_when_headers_get_raises() -> None:
    """``headers.get`` raising is caught and the retry hint becomes None.

    Sabotage-proof: removing the ``except Exception: return None`` block
    around ``headers.get`` would let the exception propagate and surface
    as a *different* class than RateLimited; the ``isinstance(exc,
    RateLimited)`` check fails.
    """

    class _BrokenHeaders:
        def get(self, _key: str, _default: Any = None) -> Any:
            raise RuntimeError("simulated broken headers")

    class _Response:
        def __init__(self) -> None:
            self.status_code = 429
            self.headers = _BrokenHeaders()

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.status_code = 429
            self.response = _Response()
            super().__init__("429 broken headers")

    provider = AnthropicProvider(
        credentials=_creds(),
        transport_client=_RaisingAnthropicClient(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


@pytest.mark.unit
def test_retry_after_of_returns_none_when_value_is_unparseable() -> None:
    """A non-numeric Retry-After value is silently dropped.

    Sabotage-proof: removing the ``except (TypeError, ValueError):
    return None`` block would let the ValueError from ``float(...)``
    propagate and surface as a different class than RateLimited.
    """
    headers = {"Retry-After": "not-a-number"}

    class _Response:
        def __init__(self) -> None:
            self.status_code = 429
            self.headers = headers

    class _FakeError(Exception):
        def __init__(self) -> None:
            self.status_code = 429
            self.response = _Response()
            super().__init__("429 bad retry-after")

    provider = AnthropicProvider(
        credentials=_creds(),
        transport_client=_RaisingAnthropicClient(_FakeError()),
    )

    with pytest.raises(RateLimited) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.retry_after_s is None


# ---------------------------------------------------------------------------
# _is_connection_failure — APITimeoutError class-name branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_api_timeout_error_class_name_maps_to_provider_unreachable() -> None:
    """An exception class named ``APITimeoutError`` maps to ProviderUnreachable.

    Sabotage-proof: removing ``"APITimeoutError"`` from the recognised
    class-name set in ``_is_connection_failure`` causes the error to
    fall through to bare ``ProviderError``; ``pytest.raises(ProviderUnreachable)``
    fails.
    """

    class APITimeoutError(Exception):
        pass

    provider = AnthropicProvider(
        credentials=_creds(),
        transport_client=_RaisingAnthropicClient(APITimeoutError("timeout")),
    )

    with pytest.raises(ProviderUnreachable):
        provider.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# _extract_text_from_content_blocks — None, str, and non-list shapes
# ---------------------------------------------------------------------------


class _RecordingMessagesReturning:
    def __init__(self, response: _FakeMessagesResponse) -> None:
        self._response = response

    def create(self, **_kwargs: Any) -> _FakeMessagesResponse:
        return self._response


class _RecordingAnthropicClient:
    def __init__(self, response: _FakeMessagesResponse) -> None:
        self.messages = _RecordingMessagesReturning(response)


@pytest.mark.unit
def test_chat_returns_empty_string_when_response_has_no_content_attr() -> None:
    """A response missing ``.content`` returns the canonical "" sentinel.

    Sabotage-proof: removing the ``if content is None: return ""`` guard
    in ``_extract_text_from_content_blocks`` would surface a different
    type and the equality assertion fails.
    """

    @dataclass
    class _NoContent:
        # No content attribute at all.
        pass

    client = _RecordingAnthropicClient(_NoContent())  # type: ignore[arg-type] — deliberately wrong-shape response object to drive the missing-content guard
    provider = AnthropicProvider(credentials=_creds(), transport_client=client)

    out = provider.chat([{"role": "user", "content": "hi"}])

    assert out == ""


@pytest.mark.unit
def test_chat_returns_string_content_verbatim() -> None:
    """When content is a plain string the helper returns it as-is.

    Sabotage-proof: removing the ``if isinstance(content, str): return
    content`` branch would route through the list-handler with a string
    iterable, joining character-by-character; the assertion ``out ==
    "raw string content"`` fails.
    """
    client = _RecordingAnthropicClient(_FakeMessagesResponse(content="raw string content"))
    provider = AnthropicProvider(credentials=_creds(), transport_client=client)

    out = provider.chat([{"role": "user", "content": "hi"}])

    assert out == "raw string content"


@pytest.mark.unit
def test_chat_returns_empty_string_when_content_is_neither_str_nor_list() -> None:
    """An int / dict / object content returns the empty-string sentinel.

    Sabotage-proof: removing the ``if not isinstance(content, list):
    return ""`` guard would let the for-loop iterate a non-list and
    raise TypeError; the equality assertion fails.
    """
    client = _RecordingAnthropicClient(_FakeMessagesResponse(content=42))  # type: ignore[arg-type] — int content drives the non-list / non-str guard in _extract_text_from_content_blocks
    provider = AnthropicProvider(credentials=_creds(), transport_client=client)

    out = provider.chat([{"role": "user", "content": "hi"}])

    assert out == ""


@pytest.mark.unit
def test_chat_reads_content_from_dict_response() -> None:
    """A dict-shaped response is recognised and its ``content`` key extracted.

    Sabotage-proof: removing the ``if content is None and
    isinstance(response, dict): content = response.get("content")``
    branch in ``chat`` returns "" instead of the dict's content; the
    equality assertion fails.
    """

    class _DictMessages:
        def create(self, **_kwargs: Any) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": "from dict"}]}

    class _DictClient:
        def __init__(self) -> None:
            self.messages = _DictMessages()

    provider = AnthropicProvider(credentials=_creds(), transport_client=_DictClient())

    out = provider.chat([{"role": "user", "content": "hi"}])

    assert out == "from dict"


# ---------------------------------------------------------------------------
# _client() lazy build + _build_default_client body (anthropic SDK absent path)
# ---------------------------------------------------------------------------


class _BlockAnthropicImportFinder:
    """A ``sys.meta_path`` finder that pretends ``anthropic`` is missing.

    Installing it ahead of every other finder ensures
    ``import anthropic`` raises ``ModuleNotFoundError`` even when the
    SDK is pip-installed in the test environment — required because
    Python 3.14's anthropic SDK is shipped by default in the kairix
    dev image. Removing the finder restores normal import resolution.
    """

    def find_spec(
        self,
        fullname: str,
        _path: object = None,
        _target: object = None,
    ) -> object:
        if fullname == "anthropic" or fullname.startswith("anthropic."):
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None


@pytest.fixture
def _swap_anthropic_module() -> Iterator[Any]:
    """Yield a setter that installs / removes a fake ``anthropic`` module.

    Each invocation replaces ``sys.modules['anthropic']`` with the
    supplied value (or, when None, installs a meta-path finder that
    makes ``import anthropic`` raise ModuleNotFoundError even when the
    real SDK is installed — required because the dev image ships the
    SDK).
    """
    saved_module = sys.modules.pop("anthropic", None)
    finder = _BlockAnthropicImportFinder()
    installed_finder = False

    def _set(replacement: ModuleType | None) -> None:
        nonlocal installed_finder
        if replacement is None:
            sys.modules.pop("anthropic", None)
            if not installed_finder:
                sys.meta_path.insert(0, finder)
                installed_finder = True
        else:
            if installed_finder and finder in sys.meta_path:
                sys.meta_path.remove(finder)
                installed_finder = False
            sys.modules["anthropic"] = replacement

    try:
        yield _set
    finally:
        if installed_finder and finder in sys.meta_path:
            sys.meta_path.remove(finder)
        if saved_module is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = saved_module


@pytest.mark.unit
def test_chat_raises_actionable_runtime_error_when_anthropic_sdk_missing(
    _swap_anthropic_module: Any,
) -> None:
    """A provider with no transport_client and no anthropic SDK installed
    surfaces an actionable ``RuntimeError`` directing the operator to
    ``pip install anthropic``.

    This drives the production-only ``_client()`` lazy-build branch
    (line 309) and the ``_build_default_client`` body's ``except
    ModuleNotFoundError`` handler.

    Sabotage-proof: removing the ``try/except ModuleNotFoundError`` in
    ``_build_default_client`` makes the bare ModuleNotFoundError
    propagate; the ``match="pip install anthropic"`` clause fails on
    the wrong exception class.
    """
    _swap_anthropic_module(None)

    provider = AnthropicProvider(credentials=_creds(), transport_client=None)

    with pytest.raises(RuntimeError, match="pip install anthropic"):
        provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_chat_uses_lazily_built_client_when_transport_not_supplied(
    _swap_anthropic_module: Any,
) -> None:
    """When ``anthropic`` IS importable the lazy build path constructs
    an SDK client via ``_build_default_client``.

    We inject a fake ``anthropic`` module exposing a recording
    ``Anthropic`` constructor that returns a stub client; the chat call
    then drives both line 309 (lazy build) AND the ``_build_default_client``
    body (lines 445-454) — the body resolves the import, constructs
    ``Anthropic(...)``, and returns that instance which we observe
    failing later when its (deliberately broken) ``messages.create``
    raises a ProviderError.

    Sabotage-proof: removing the ``return _build_default_client(...)``
    line in ``_client()`` causes a TypeError ("'NoneType' object has
    no attribute 'messages'") that doesn't pattern-match
    ``ProviderError``; ``pytest.raises(ProviderError)`` fails.
    """

    class _RecordingConstructor:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def __call__(self, **kwargs: Any) -> Any:
            self.calls.append(dict(kwargs))

            class _StubClient:
                class messages:  # noqa: N801 — mirrors the anthropic SDK shape
                    @staticmethod
                    def create(**_create_kwargs: Any) -> Any:
                        raise ConnectionError("fake SDK declined")

            return _StubClient()

    recorder = _RecordingConstructor()
    fake_module = ModuleType("anthropic")
    fake_module.Anthropic = recorder  # type: ignore[attr-defined] — synthesised module exposes the same surface (Anthropic constructor) the production code imports
    _swap_anthropic_module(fake_module)

    provider = AnthropicProvider(credentials=_creds(), transport_client=None)

    with pytest.raises(ProviderError):
        provider.chat([{"role": "user", "content": "hi"}])

    # The default constructor was driven exactly once with the configured
    # api_key + endpoint + the pinned anthropic-version header. This
    # asserts that lines 445-454 actually executed.
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["api_key"] == "anthropic-test-key"  # pragma: allowlist secret
    assert call["base_url"] == "https://api.anthropic.com"
    assert call["default_headers"]["anthropic-version"]


@pytest.mark.unit
def test_unknown_class_with_no_status_attribute_falls_back_to_provider_error() -> None:
    """An exception with neither status_code nor response yields bare ProviderError.

    Sabotage-proof: removing the fall-through ``return
    ProviderError(...)`` would mean unknown errors propagate
    unchanged and ``pytest.raises(ProviderError)`` fails. The mapper
    must surface a typed error class for every upstream exception.

    Drives the ``return None`` line in ``_status_code_of`` (line 137).
    """

    class _UnknownError(Exception):
        pass

    provider = AnthropicProvider(
        credentials=_creds(),
        transport_client=_RaisingAnthropicClient(_UnknownError("unknown")),
    )

    with pytest.raises(ProviderError) as exc_info:
        provider.chat([{"role": "user", "content": "hi"}])

    # Specifically the bare ProviderError class — NOT a subclass like
    # AuthError/RateLimited.
    assert type(exc_info.value) is ProviderError
    # Confirm the helpers correctly determined no status code.
    assert not isinstance(exc_info.value, AuthError)
