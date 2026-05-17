# Provider plugin architecture — three-layer split

> **Status**: proposed (awaiting orchestrator-led implementation). Names
> the architectural split that separates kairix's retrieval / memory /
> worker domain from the universal endpoint concerns (pool, retry,
> coalesce, cache) and the per-provider plugins (Azure Foundry / OpenAI /
> Bedrock / Ollama / LiteLLM-proxy / Anthropic). Settles plugin discovery
> on Python entry points and locks the separation with four new fitness
> functions (F26–F29). Performance instrumentation stays singular —
> `kairix/quality/probe/` measures every layer through one uniform
> timing contract, runnable both as a PVT release gate and as a
> post-deploy `kairix probe-config` health check end users invoke
> against their own setup.

## Context

The Phase-3 performance work accreted a small AI-gateway in process:
`EmbedCoalescer` (#288), `EmbedCache` (#285), `QueryResultCache` (#281),
retry/pool tuning (#280), plus a now-discovered TLS-handshake bug in
`kairix/_azure.py:_get_client` where every coalescer batch builds a
fresh `httpx.Client`. Each fix was sound on its own, but cumulatively
they recreate a transport layer inside the domain module, with the same
class of bugs that ship-stable transport infrastructure already solves.

A second forcing function: kairix has to be deployable on any VM
(operator-owned or any enterprise cloud) with a configuration-driven
choice of LLM/embed endpoint. #247 calls for Bedrock + OpenAI-direct +
Ollama + Anthropic alongside the existing Azure Foundry path. Treating
each as "add a code path inside `_azure.py`" doesn't scale.

The architectural cost of doing nothing: every new perf concern adds
another homegrown class to `kairix/core/`; every new provider mutates
`_azure.py` further; the probe code grows per-provider conditionals;
fitness functions police the domain but not the transport surface.

## Decision

Split kairix into three layers, with a Protocol-based seam between
each. **Core knows about Protocols, not implementations.**

```
kairix/
  core/         ← domain: search / memory / worker / retrieval
                ← already Protocol-fronted (kairix/core/protocols.py)
                ← only allowed import from below: kairix/core/protocols.py

  transport/    ← universal endpoint concerns; one implementation, all providers benefit
    auth/       ← credential resolution + secret lookup (get_credentials, get_secret)
    pool/       ← httpx client pool + make_openai_client + the TLS-handshake fix
    coalesce/   ← EmbedCoalescer (moved from kairix/core/embed/)
    cache/      ← EmbedCache + QueryResultCache (moved)
    retry/      ← retry/backoff policy
    timeout/    ← timeout policy + circuit-breaking
    telemetry/  ← uniform timings hook protocol the probe reads from

  providers/    ← per-endpoint plugins; one directory per provider
    _base.py    ← Provider Protocol + ProviderRegistry Protocol
    azure_foundry/   ← /openai/v1 alias, key-vault creds, Azure-specific error mapping
    azure_legacy/    ← AzureOpenAI SDK, api-version param
    openai/          ← base_url + api_key
    bedrock/         ← AWS SigV4, model IDs
    ollama/          ← localhost, no auth
    anthropic/       ← x-api-key header, message format
    litellm_proxy/   ← thin shim to a LiteLLM sidecar

  quality/probe/  ← UNCHANGED location; single performance-measurement surface
                  ← reads the transport/telemetry hook; no provider conditionals
                  ← invoked by PVT (release gate) AND by `kairix probe-config` (end-user)
```

## Provider Protocol contract

Defined once at `kairix/providers/_base.py`; every plugin satisfies it.
Core code never imports a concrete provider — only the Protocol.

```python
class Provider(Protocol):
    """One LLM/embed endpoint family. Implementations live under
    kairix/providers/<name>/ and register via the kairix.providers
    entry-point group in their pyproject.toml."""

    name: str  # "azure_foundry" | "openai" | "bedrock" | ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch embed N texts in one HTTP call. Never raises; returns
        [[]] entries for failures so callers can short-circuit."""

    def chat(self, messages: list[dict], *, max_tokens: int = 800) -> str:
        """Single chat completion. Never raises; returns "" on failure."""

    def dimension(self) -> int:
        """Embedding vector dimension; constant per deployed model."""

    def healthcheck(self) -> ProviderHealth:
        """Synchronous probe: does the configured endpoint respond?
        Used by `kairix probe-config` and the operator-facing CLI."""
```

`ProviderRegistry` is a sibling Protocol that resolves a configured
name to a `Provider` instance. Production wires it via the entry-points
mechanism below; tests inject `FakeProviderRegistry` from
`tests/fakes.py`.

## Plugin discovery — entry points, name-filtered

First-party providers register in kairix's own `pyproject.toml`:

```toml
[project.entry-points."kairix.providers"]
azure_foundry = "kairix.providers.azure_foundry:make_provider"
azure_legacy  = "kairix.providers.azure_legacy:make_provider"
openai        = "kairix.providers.openai:make_provider"
bedrock       = "kairix.providers.bedrock:make_provider"
ollama        = "kairix.providers.ollama:make_provider"
litellm_proxy = "kairix.providers.litellm_proxy:make_provider"
anthropic     = "kairix.providers.anthropic:make_provider"
```

Third parties ship a separate pip distribution that declares the same
entry-point group. `pip install kairix-provider-foo` then
`KAIRIX_PROVIDER=foo` works with zero kairix code change.

Resolution is name-filtered, not eager-scanned:

```python
def get_provider(name: str) -> Provider:
    eps = importlib.metadata.entry_points(group="kairix.providers", name=name)
    if not eps:
        installed = sorted(ep.name for ep in
                          importlib.metadata.entry_points(group="kairix.providers"))
        raise ProviderNotRegistered(name=name, available=installed)
    factory = eps[0].load()
    return factory()  # returns Provider instance
```

This is sub-10 ms on Python 3.10+ for name-filtered lookups, costs
nothing for unused providers, and surfaces a typed
`ProviderNotRegistered(name, available=[...])` error when an operator
typo's the env var.

The closest production analogue is **datasette's plugin model**; we
explicitly reject the LangChain partner-package style (direct module
import by users) because operators select providers by config string,
not by `import` statement.

## BDD coverage matrix — no duplication, full surface

| Layer | Feature files | Test seam | What it proves |
|---|---|---|---|
| `kairix/core/` | existing `search_*.feature`, `memory_*.feature`, `worker_*.feature` | FakeEmbeddingService / FakeLLMBackend from `tests/fakes.py` | Domain behaviour; provider-agnostic |
| `kairix/transport/` | `transport_pool.feature`, `transport_coalesce.feature`, `transport_cache.feature`, `transport_retry.feature`, `transport_timeout.feature` | FakeProvider (counts calls / controls latency / errors) | Universal endpoint concerns work for **any** provider |
| `kairix/providers/<name>/` | `provider_<name>.feature` per plugin | Per-provider HTTP fixture stubbing the wire | Auth shape, URL shape, error mapping, model-id semantics |
| **E2E journeys** | `e2e_provider_embed.feature`, `e2e_provider_chat.feature`, `e2e_provider_switch.feature` | Scenario Outline parameterised over all providers | "User configures provider X → embed/chat works" — same scenario, N rows |

The E2E feature files are **Scenario Outlines**, not one feature per
provider — adding a new provider is one new fixture + one new outline
row, not a copy-paste duplication. F28 (below) makes this mechanical.

## Performance — one probe, two consumers

`kairix/quality/probe/` stays where it is. It does NOT know which
provider is loaded; it measures through one uniform timing hook
exposed by every layer. Today that hook is the
`timings: dict[str, float]` kwarg on `VectorSearchBackend.search`;
this ADR generalises it.

**Uniform stage keys** the probe collects:

- Transport: `pool_acquire`, `coalesce_wait`, `cache_lookup`, `retry_attempts`
- Provider: `auth_resolve`, `request_serialize`, `http_roundtrip`, `response_parse`
- Core: `classify`, `resolve`, `dispatch.bm25`, `dispatch.vector_ann`, `fuse`, `enrich`, `boost`, `budget`

Probe just sums and reports — no per-provider conditionals.

**Two consumer paths, one implementation:**

1. **PVT pre-release** — `kairix probe --suite reflib --concurrency 10`
   runs against the project's reference provider. Existing PVT Gherkin
   scenarios (`tests/pvt/features/*.feature`) cover this.

2. **End-user setup verification** — new CLI `kairix probe-config`
   runs a small representative workload against **the user's
   configured plugin**, emits a JSON health report (cold/warm latency,
   coalesce ratio, cache hit rate, recommended pool/coalesce tuning
   for their endpoint distance). Operators share that file when
   opening a support issue.

Schema for the JSON report and Gherkin for the CLI surface land at
`docs/architecture/probe-config-schema.md` and
`tests/bdd/features/probe_config_health.feature` (SK-7).

## Fitness functions

Mechanical, blocking checks codify the separation:

- **F26** — `kairix/core/**` may not import `kairix/providers/**` or
  `kairix/transport/**` (only types via `kairix/core/protocols.py`).
  Blocks the regression class where domain code accretes transport
  knowledge. Pre-existing violations grandfathered in
  `.architecture/baseline/F26-files.txt`.

- **F27** — `kairix/providers/<name>/**` may not import another
  provider (`kairix/providers/<other>/**`). Cross-provider work goes
  through `kairix/transport/`. Keeps plugins independently shippable
  as third-party pip distributions.

- **F28** — every plugin directory under `kairix/providers/<name>/`
  has a matching `tests/bdd/features/provider_<name>.feature` AND
  appears as a row in the `e2e_provider_*.feature` Scenario Outlines.
  Stops new providers shipping without behaviour tests.

- **F29** — any new performance-measurement code may only land under
  `kairix/quality/probe/**`. Stops `transport/` and `providers/`
  growing parallel benchmark harnesses.

All four follow the existing F-rule template: action-marked failure
messages (F21), per-rule baseline file under `.architecture/baseline/`,
wired into pre-commit + `scripts/safe-commit.sh` + CI Stage 0.

## Resolved ambiguities

From the code survey (sub-agent `a0a453815356cd66b`):

1. **`make_openai_client` + three-branch endpoint detection** — goes to
   `kairix/transport/pool/`. The detection logic *is* client
   construction, not secret lookup; credentials only feed the
   parameters.

2. **`get_default_backend` factory** — goes to `kairix/core/llm/get_provider()`
   as a registry dispatcher. Protocol lives in core, so the dispatcher
   does too; the dispatcher takes a `ProviderRegistry` parameter (DI).

3. **`EmbedProvider` vs `LLMBackend` duplication** — unify on
   `LLMBackend` (the older, more-used protocol). `EmbedProvider` and
   its `AzureEmbedProvider`/`OpenAIEmbedProvider` impls retire; their
   logic merges into the per-provider plugin's `embed_batch` method.

## Migration plan

Phased, each phase a separate worktree-dispatched wave landing as
green-on-`develop` cherry-picks.

| Wave | Work items | Parallel? | Depends on |
|---|---|---|---|
| **0** | This ADR | foreground | nothing |
| **1 (scaffold)** | SK-1 transport/ skeleton + shims · SK-2 providers/ skeleton + Protocol · SK-3 transport BDD scenarios · SK-4 provider BDD scenarios · SK-5 E2E journey BDD · SK-6 F26–F29 fitness checks · SK-7 probe-config BDD + JSON schema | yes (7 worktrees) | ADR |
| **2 (extract)** | IM-1 client_pool with TLS-handshake fix · IM-2 coalescer move · IM-3 cache move · IM-4 azure_foundry plugin from `_azure.py` · IM-5 openai plugin (proves the contract) · IM-6 transport step impls · IM-7 provider step impls · IM-8 E2E step impls · IM-9 `kairix probe-config` CLI | yes (9 worktrees) | Wave 1 |
| **3 (verify)** | Deploy + probe at conc=10; measure embed_http with TLS-handshake fix in place; confirm provider switch works; CHANGELOG entry; runbook for end-user probe-config | sequential | Wave 2 |
| **4 (follow-up #247)** | bedrock/ ollama/ litellm_proxy/ anthropic/ plugins | yes (N worktrees) | Wave 3 |

Wave 1 + Wave 2 are the heavy parallelism. Wave 3 is the measurement
gate that proves the architecture didn't regress real workloads.
Wave 4 is additive — each new provider lands as its own commit
against the now-stable plugin contract.

## Open questions

None blocking. Items deferred to Wave 4:

- Per-provider rate-limit awareness (each plugin reports a recommended
  pool size from its known SLA — feeds the `probe-config` tuner).
- Streaming chat responses (current Provider contract returns `str`;
  AsyncIterator is a v2 addition).
- Multi-region failover (a Provider could front N endpoints; out of
  scope for the initial split).

## References

- Plugin discovery research: sub-agent `a435cda2ffb5f7eac` 2026-05-17
- Code survey (what-moves-where): sub-agent `a0a453815356cd66b` 2026-05-17
- Performance-testing approach (related): [`performance-testing-approach.md`](performance-testing-approach.md)
- Existing fitness-function canon: [`fitness-functions.md`](fitness-functions.md)
