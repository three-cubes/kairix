# kairix/corpus/

Unified corpus-ingest layer. The shared primitive every conversational
ingest path eventually routes through: `kairix ingest-chat`, the
`SuiteRunner` reference-library evals, and the LoCoMo benchmark
harness. Spike C1 documented the three divergent paths the codebase
carried before this module landed; the goal of `kairix/corpus/` is to
collapse them onto one well-tested entry point.

## Public surface

```python
from kairix.corpus.ingest import (
    SessionPayload,  # one conversational session, source-agnostic
    IngestRequest,  # a corpus-shaped unit of work
    IngestResult,  # counters returned to operators + tests
    ingest_corpus,  # the single shared primitive
)
```

`ingest_corpus(request, *, paths, fact_store, fact_extractor, ...)`
composes the optional `DocumentWriter`, `CorpusEmbedder`, and
`ConsolidationPass` collaborators. Both `DocumentWriter` and
`CorpusEmbedder` are nullable — passing `None` is the documented
opt-out for chunks-only or facts-only modes.

## Where each piece lives

- `ingest.py` — `ingest_corpus` + the four dataclasses. Pure
  orchestration; no I/O of its own beyond what the injected
  collaborators perform.
- `__init__.py` — exports the public symbols.

Production wire-ups live in `wiring.py` — the single composition
root every use case calls when its caller passes no
`fact_extractor` / `document_writer` / `embedder` / `consolidation`
override:

```python
from kairix.corpus.wiring import (
    make_production_fact_extractor,  # ACTIVE: wraps LLMFactExtractor
    make_production_document_writer,  # Phase 2 deferral (raises F21-formatted NotImplementedError)
    make_production_embedder,  # Phase 2 deferral
    make_production_consolidation,  # Phase 3 deferral
)
```

`make_production_fact_extractor(llm)` closed the LoCoMo verification
gap (`kairix eval` returned 0/N facts on conversational corpora
because the default fell to `_NullFactExtractor`). The other three
factories raise `NotImplementedError` with `fix:` / `next:` / `run:`
markers until their upstream implementations land — they exist so a
premature caller gets an actionable error rather than a Null fallback.

`wiring.py` is the F26 composition-root carve-out: it MAY import
concrete provider/transport implementations (the F26 prohibition
applies to `kairix/core/**`, not to `kairix/corpus/**`). Domain code
still talks to the Protocols.

## Where each test lives

- `tests/core/corpus/test_ingest.py` — unit tests pinning the public
  contract: dataclass round-trips, happy-path orchestration, the
  null-writer / null-embedder / null-consolidation branches, the
  Lever A `session_metadata` propagation, and the `skipped_sessions`
  partial-failure mode.
- Protocols (`DocumentWriter`, `CorpusEmbedder`) are pinned in
  `tests/contracts/` alongside the other Protocol conformance tests.

## What does NOT belong here

- CLI argument parsing — that's `kairix/use_cases/ingest_chat.py:main`
  (and equivalent dispatch points for `kairix eval`, etc.).
- Production-wire helpers (`_resolve_production_fact_store`, etc.) —
  those move into `wiring.py` in later phases.
- LoCoMo / suite-runner adapter glue — each caller keeps its own
  CLI-input concerns; the corpus layer is what they call into.
- Document-shaped (non-conversational) ingest — reference-library
  NDCG suites still go through `kairix embed`'s document-scan
  pipeline. `ingest_corpus` is for **conversational** corpora only.

## Architecture fitness functions touched

- **F22**: `kairix/corpus/*.py` snake_case. `__init__.py` and
  `ingest.py` satisfy.
- **F23**: this README is the resolver for `kairix/corpus/` — what
  belongs, what doesn't, where to find tests.
- **F26**: `kairix/corpus/` may import `kairix.core.protocols`,
  `kairix.paths`, `kairix.core.facts.consolidation`. Must NOT
  import `kairix.providers.*` or `kairix.transport.*`.

See `docs/architecture/fitness-functions.md` for the canonical
listing.
