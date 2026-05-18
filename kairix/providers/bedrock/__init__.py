"""AWS Bedrock provider plugin.

Wire-level translator that adapts the universal
:class:`kairix.providers.Provider` Protocol to AWS Bedrock-Runtime
(``bedrock-runtime.<region>.amazonaws.com``). Outbound requests are
SigV4-signed (by boto3 internally in production; the recording fake
in the test seam mirrors the same ``Authorization: AWS4-HMAC-SHA256``
shape).

Operator selection model: this plugin owns its own AWS credential
resolution via :func:`_resolve_aws_credentials`, which delegates to
boto3's default credential chain (env → shared credentials file →
IAM role / instance metadata → SSO). That differs from the Azure
plugins, which read :func:`kairix.credentials.get_credentials` —
because AWS already has a battle-tested credential chain operators
expect and there is no benefit to re-implementing it inside kairix.

The ``boto3`` dependency is declared under the ``bedrock`` extra in
``pyproject.toml`` (``pip install kairix-agentic-knowledge-mgt[bedrock]``)
so operators not on AWS don't pay the wheel-size cost. The dep is
imported lazily inside :func:`_resolve_aws_credentials` and inside
:meth:`kairix.providers.bedrock.BedrockProvider._client` so the
``ImportError`` only surfaces when an operator actually tries to use
the plugin without the extra installed.

The plugin is discovered by ``EntryPointRegistry`` through the
``[project.entry-points."kairix.providers"]`` table in kairix's
``pyproject.toml`` — production callers resolve it by name:

.. code-block:: python

   from kairix.providers import get_provider

   provider = get_provider("bedrock")
   vectors = provider.embed_batch(["hello world"])

See ``docs/architecture/provider-plugin-architecture.md`` for the
ADR and ``tests/bdd/features/provider_bedrock.feature`` for the
wire-shape contract this plugin pins.
"""

from __future__ import annotations

from kairix.paths import (
    bedrock_chat_model,
    bedrock_embed_model,
    bedrock_region_override,
    embed_vector_dims,
)
from kairix.providers._base import Provider
from kairix.providers.bedrock.provider import (
    DEFAULT_CHAT_MAX_TOKENS,
    DEFAULT_CHAT_MODEL_ID,
    DEFAULT_EMBED_DIMENSION,
    DEFAULT_EMBED_MODEL_ID,
    PROVIDER_NAME,
    BedrockCredentials,
    BedrockProvider,
    bedrock_runtime_endpoint,
)


def _resolve_aws_credentials() -> BedrockCredentials:
    """Resolve AWS credentials via boto3's default credential chain.

    boto3 looks (in order):

    1. Environment variables (``AWS_ACCESS_KEY_ID`` /
       ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN`` /
       ``AWS_DEFAULT_REGION``).
    2. The shared credentials file (``~/.aws/credentials``) and config
       file (``~/.aws/config``) honouring ``AWS_PROFILE``.
    3. IAM role attached to the EC2 / ECS / Lambda runtime (instance
       metadata service or container metadata service).
    4. SSO credentials cached by ``aws sso login``.

    This factory does NOT call
    :func:`kairix.credentials.get_credentials` — that's the Azure
    pattern. Each provider plugin owns its credential resolution; AWS
    operators expect their existing credential chain to work without
    kairix-specific configuration.

    Model ids and region come from kairix-side configuration routed
    through :mod:`kairix.paths` (F4 — only ``paths.py`` / ``secrets.py``
    may read ``KAIRIX_*`` env vars):

    - :func:`kairix.paths.bedrock_region_override` overrides the
      boto3-resolved region (operators sometimes pin their inference
      region distinct from their control-plane region).
    - :func:`kairix.paths.bedrock_embed_model` /
      :func:`kairix.paths.bedrock_chat_model` set the model ids;
      defaults are Amazon Titan embed + Anthropic Claude 3.5 Sonnet.
    - :func:`kairix.paths.embed_vector_dims` sets the configured embed
      dimension (shared with all plugins).

    boto3 is imported lazily so installs without the ``bedrock`` extra
    don't pay the wheel cost; the ``ImportError`` here surfaces only
    when the operator actually selected ``KAIRIX_PROVIDER=bedrock``.
    """
    try:
        import boto3
    except ImportError as err:
        raise RuntimeError(
            "bedrock provider requires the 'boto3' package. "
            "fix: pip install 'kairix-agentic-knowledge-mgt[bedrock]' "
            "(or pip install boto3 directly); "
            "next: re-run with KAIRIX_PROVIDER=bedrock once boto3 is installed."
        ) from err

    session = boto3.Session()
    creds = session.get_credentials()
    if creds is None:
        raise RuntimeError(
            "bedrock provider could not resolve AWS credentials via the boto3 "
            "default credential chain. "
            "fix: configure AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in the "
            "environment, populate ~/.aws/credentials, or attach an IAM role "
            "to the runtime (EC2 instance / ECS task / Lambda function); "
            "next: re-run kairix probe-config to verify credentials are picked up."
        )
    # boto3.Credentials.get_frozen_credentials() returns a tuple-like with
    # access_key / secret_key / token slots — stable across boto3 versions
    # and resilient to live credential rotation (refreshable role creds).
    frozen = creds.get_frozen_credentials()

    region = bedrock_region_override() or session.region_name
    if not region:
        raise RuntimeError(
            "bedrock provider could not resolve an AWS region. "
            "fix: set KAIRIX_BEDROCK_REGION or AWS_DEFAULT_REGION, or configure "
            "a default region under ~/.aws/config; "
            "next: re-run with the region set."
        )

    embed_model = bedrock_embed_model(default=DEFAULT_EMBED_MODEL_ID)
    chat_model = bedrock_chat_model(default=DEFAULT_CHAT_MODEL_ID)
    dims = embed_vector_dims(default=0)

    return BedrockCredentials(
        access_key_id=frozen.access_key,
        secret_access_key=frozen.secret_key,
        session_token=frozen.token,
        region=region,
        embed_model_id=embed_model,
        chat_model_id=chat_model,
        dims=dims,
    )


def make_provider() -> Provider:
    """Construct the Bedrock :class:`Provider` for entry-point discovery.

    Resolves AWS credentials via the boto3 default credential chain
    (env → shared file → IAM role → SSO) and constructs a
    :class:`BedrockProvider`. The boto3 ``bedrock-runtime`` client is
    constructed lazily inside
    :meth:`BedrockProvider._client` on the first ``embed_batch`` /
    ``chat`` call so import-time work stays minimal.

    Tests should NOT call ``make_provider()``; they construct
    :class:`BedrockProvider` directly with a :class:`BedrockCredentials`
    test instance and a recording ``transport_client``. This factory
    exists purely to satisfy the entry-point discovery contract.
    """
    creds = _resolve_aws_credentials()
    return BedrockProvider(credentials=creds)


__all__ = [
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_CHAT_MODEL_ID",
    "DEFAULT_EMBED_DIMENSION",
    "DEFAULT_EMBED_MODEL_ID",
    "PROVIDER_NAME",
    "BedrockCredentials",
    "BedrockProvider",
    "bedrock_runtime_endpoint",
    "make_provider",
]
