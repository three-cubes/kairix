"""Unit tests for :mod:`kairix.providers.bedrock` entry-point factory.

Covers ``make_provider()`` — the entry-point discovery target. The
factory wraps an internal ``_resolve_aws_credentials`` helper that
delegates to boto3's default credential chain. We drive every branch
through ``make_provider()`` (F5: no private-name imports in tests) by
injecting a fake ``boto3`` module via ``sys.modules`` and observing
the resulting RuntimeError class / message.

The factory raises actionable RuntimeError in three failure modes:

1. boto3 not installed (``[bedrock]`` extra missing);
2. session.get_credentials() returned ``None`` (chain didn't match);
3. region unresolved from override or session.

Test seam: ``sys.modules['boto3']`` injection — stdlib-shape, F1-clean
(no ``@patch``) and F2-clean (no ``monkeypatch.setenv``).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest

from kairix.providers import Provider
from kairix.providers.bedrock import BedrockProvider, make_provider


class _FrozenCreds:
    """Stand-in for boto3's ``Credentials.get_frozen_credentials()`` return."""

    def __init__(
        self,
        access_key: str = "AKIATEST",  # pragma: allowlist secret
        secret_key: str = "SECRETKEY",  # pragma: allowlist secret
        token: str | None = "SESSIONTOKEN",  # pragma: allowlist secret
    ) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = token


class _FakeAwsCredentials:
    def __init__(self, frozen: _FrozenCreds) -> None:
        self._frozen = frozen

    def get_frozen_credentials(self) -> _FrozenCreds:
        return self._frozen


class _FakeSession:
    def __init__(
        self,
        creds: _FakeAwsCredentials | None,
        region: str | None = "us-east-1",
    ) -> None:
        self._creds = creds
        self.region_name = region

    def get_credentials(self) -> _FakeAwsCredentials | None:
        return self._creds


class _FakeBoto3Module(ModuleType):
    """A boto3-shaped stub that constructs a configured ``Session()``."""

    def __init__(self, session: _FakeSession | None) -> None:
        super().__init__("boto3")
        self._session = session

    def Session(self) -> _FakeSession:  # noqa: N802 — mirrors boto3's class-shaped factory
        assert self._session is not None
        return self._session


class _BlockBoto3ImportFinder:
    """A ``sys.meta_path`` finder that pretends ``boto3`` is missing.

    Installing it ahead of every other finder ensures
    ``import boto3`` raises ``ModuleNotFoundError`` even when the SDK
    is pip-installed in the test environment. Removing the finder
    restores normal import resolution.
    """

    def find_spec(
        self,
        fullname: str,
        _path: object = None,
        _target: object = None,
    ) -> object:
        if fullname == "boto3" or fullname.startswith("boto3."):
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None


@pytest.fixture
def _swap_boto3() -> Iterator[Any]:
    """Yield a setter that installs / removes the fake boto3 module.

    Each invocation replaces ``sys.modules['boto3']`` with the supplied
    value. When replacement is ``None`` the fixture installs a
    meta-path finder that makes ``import boto3`` raise even if the
    real SDK is installed (mirrors the dev/CI environment differences
    between hosts).
    """
    saved = sys.modules.pop("boto3", None)
    finder = _BlockBoto3ImportFinder()
    installed_finder = False

    def _set(replacement: ModuleType | None) -> None:
        nonlocal installed_finder
        if replacement is None:
            sys.modules.pop("boto3", None)
            if not installed_finder:
                sys.meta_path.insert(0, finder)
                installed_finder = True
        else:
            if installed_finder and finder in sys.meta_path:
                sys.meta_path.remove(finder)
                installed_finder = False
            sys.modules["boto3"] = replacement

    try:
        yield _set
    finally:
        if installed_finder and finder in sys.meta_path:
            sys.meta_path.remove(finder)
        if saved is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = saved


@pytest.mark.unit
def test_make_provider_returns_bedrock_provider_on_happy_path(_swap_boto3: Any) -> None:
    """boto3 + populated session + region → ``BedrockProvider`` instance.

    Sabotage-proof: comment the ``return BedrockProvider(...)`` in
    ``make_provider()`` — the function falls off the end and returns
    ``None``; the ``isinstance`` assertion fails immediately.
    """
    frozen = _FrozenCreds()
    session = _FakeSession(creds=_FakeAwsCredentials(frozen), region="us-west-2")
    _swap_boto3(_FakeBoto3Module(session))

    provider = make_provider()

    assert isinstance(provider, BedrockProvider)
    assert isinstance(provider, Provider)
    assert provider.name == "bedrock"


@pytest.mark.unit
def test_make_provider_raises_when_boto3_missing(_swap_boto3: Any) -> None:
    """Missing boto3 surfaces an actionable RuntimeError pointing at the extra.

    Sabotage-proof: drop the ``try/except ImportError`` block — the
    bare ImportError propagates without the ``pip install
    kairix-agentic-knowledge-mgt[bedrock]`` affordance; the
    ``match="pip install"`` clause fails.
    """
    _swap_boto3(None)

    with pytest.raises(RuntimeError, match="pip install"):
        make_provider()


@pytest.mark.unit
def test_make_provider_raises_when_session_has_no_credentials(_swap_boto3: Any) -> None:
    """A ``None`` from session.get_credentials() yields a typed RuntimeError.

    Sabotage-proof: drop the ``if creds is None: raise`` guard —
    ``creds.get_frozen_credentials()`` is called on a ``None``
    receiver and surfaces AttributeError instead of the actionable
    RuntimeError. The ``match="AWS credentials"`` assertion fails.
    """
    session = _FakeSession(creds=None, region="us-east-1")
    _swap_boto3(_FakeBoto3Module(session))

    with pytest.raises(RuntimeError, match="AWS credentials"):
        make_provider()


@pytest.mark.unit
def test_make_provider_raises_when_region_missing(_swap_boto3: Any) -> None:
    """No override and no session region → typed RuntimeError.

    Sabotage-proof: dropping the ``if not region: raise`` guard means
    a region-less ``BedrockCredentials`` is constructed; downstream
    botocore calls fail with a less-actionable ``NoRegionError``.
    The ``match="region"`` assertion fails.
    """
    frozen = _FrozenCreds()
    session = _FakeSession(creds=_FakeAwsCredentials(frozen), region=None)
    _swap_boto3(_FakeBoto3Module(session))

    with pytest.raises(RuntimeError, match="region"):
        make_provider()


@pytest.mark.unit
def test_make_provider_error_messages_carry_affordance(_swap_boto3: Any) -> None:
    """Each failure RuntimeError carries an F21-compliant affordance.

    Sabotage-proof: stripping ``fix:`` / ``next:`` markers from the
    three RuntimeError messages breaks F21 actionable-feedback
    compliance and the substring assertions fail.
    """
    # boto3-missing branch
    _swap_boto3(None)
    with pytest.raises(RuntimeError) as exc_info_no_boto3:
        make_provider()
    msg_no_boto3 = str(exc_info_no_boto3.value)
    assert "fix:" in msg_no_boto3
    assert "next:" in msg_no_boto3

    # no-credentials branch
    session_no_creds = _FakeSession(creds=None, region="us-east-1")
    _swap_boto3(_FakeBoto3Module(session_no_creds))
    with pytest.raises(RuntimeError) as exc_info_no_creds:
        make_provider()
    msg_no_creds = str(exc_info_no_creds.value)
    assert "fix:" in msg_no_creds
    assert "next:" in msg_no_creds

    # no-region branch
    frozen = _FrozenCreds()
    session_no_region = _FakeSession(creds=_FakeAwsCredentials(frozen), region=None)
    _swap_boto3(_FakeBoto3Module(session_no_region))
    with pytest.raises(RuntimeError) as exc_info_no_region:
        make_provider()
    msg_no_region = str(exc_info_no_region.value)
    assert "fix:" in msg_no_region
    assert "next:" in msg_no_region
