# Test Resilience Plan — failure-mode coverage across BDD/E2E/Integration/Contract

**Status:** ✅ Implemented — spent plan · **Owner:** kairix engineering · **Opened:** 2026-05-26. The failure-mode coverage shipped (the integration/contract suites it specifies exist; the F-rules it motivated — F62/F67/F70/F71 + F68/F69/F72 — are live per `docs/architecture/defect-catalogue.md` and ADR-024). Retained as design provenance — the failure-injection test suite cites it. Not active work.

## 1. Why this exists

The v2026.5.26a1 → v2026.5.27a2 SharePoint dogfood revealed five classes of real production defects that **the existing 8,130-test suite did not catch**. Each defect was structurally preventable with a failure-mode test that didn't exist:

| Defect class | Caught by | Should have been caught by |
|---|---|---|
| markitdown tmpfile leak on write-step ENOSPC (199fce34) | Production cascade | Integration test driving extractor against constrained tmpfs |
| Dockerfile missing per-format markitdown extras (#322) | First-run dead-letter wave | Container-image E2E test importing each declared extractor library |
| Silver chunker leaves oversized paragraphs as single chunks (Bug B) | Manual chunk-size inspection in production | Integration test with pathological paragraph shapes |
| Bronze raw bytes accumulate to 112GB on 8,783-item corpus | Operator disk-usage check | E2E test driving N-item ingest with disk-usage assertions |
| EscalatingExtractor documented as "the orchestrator's chain" but never wired | Production never used it for 7+ months | BDD scenario asserting end-to-end escalation path |

The pattern: **happy-path tests are saturated; failure-mode tests are sparse.** Existing `@error` BDD scenarios concentrate in a few files (`classify.feature`, `e2e_provider_*.feature`, `extractor_chain_escalation.feature`). Failure-injection integration tests exist for one pipeline (`test_connector_pipeline_long_batch_durability.py`) but not for the broader surface. E2E tests are all `composed_*_path.py` — every one a happy-path single-pass exercise.

This plan defines a failure-mode taxonomy, audits the current coverage gap, and lists 12 concrete tests to add across the four layers.

## 2. Failure-mode taxonomy

Five classes of failures that the dogfood evidence shows kairix encounters in production:

### Class A — Resource exhaustion mid-extract

- ENOSPC on scratch disk during write
- OOM on a pathological PPTX/PDF expansion
- File-descriptor exhaustion under high connector concurrency

**Test design:** Drive the real extractor against a bounded resource (small tmpfs, `prlimit`-constrained memory, FD-cap). Assert cleanup discipline + dead-letter routing rather than cascade-failure.

### Class B — Library/dependency drift

- Missing pip extra (#322 — markitdown[pdf] without [pptx])
- Library version skew (e.g. python-docx changes its API mid-minor)
- C++ library missing (pytesseract without tesseract-ocr installed)

**Test design:** Onboard-check style — import each declared extractor library at startup. F30 outcome test against `kairix onboard check` covering the registered set. Container-image E2E that builds the production image and runs the check inside.

### Class C — Chunker / silver boundary cases

- Oversized paragraph (Bug B)
- Single sentence > chunk budget
- Single word > chunk budget
- Empty extracted markdown
- Markdown with only headings (no body)
- Markdown with embedded code blocks larger than budget

**Test design:** Integration test against `DefaultSilverProcessor.process` with constructed `ExtractedDocument` shapes covering each pathological case. Assert chunk count, max chunk size, no information loss.

### Class D — Pipeline mid-batch failures + recovery

- One item raises during fetch → sibling items continue
- One item raises during extract → sibling items continue
- Silver/writer raises → batch rolls back, cursor doesn't advance past failure
- Worker SIGTERM mid-batch → resume from cursor
- DB lock contention between worker and reextract → backoff + retry

**Test design:** Integration tests using scripted-failure connectors that yield N items and fail at item K. Assert (a) bronze rows for items 0..K-1 are committed, (b) cursor at the right boundary, (c) dead-letter row for item K.

### Class E — Recovery path edge cases (Bug D / reextract)

- Bronze row exists, raw file missing on disk
- Dead-letter row exists, bronze row missing
- Re-extract while config no longer declares the connector
- Re-extract with chain config when original used single extractor
- Concurrent worker sync + reextract competing for DB lock

**Test design:** Integration tests with carefully-constructed pre-states (orphan bronze row, missing raw file, mid-cycle config change). Assert each path increments the right counter and never corrupts state.

## 3. Per-layer audit + redesign

### 3.1 Contract layer (current: 68 files, 35 marker-decorated)

**Catching well:**
- Protocol shape compliance (Wave 2 extractor plugins all have `tests/contracts/test_<name>_protocol.py`)
- Frozen-dataclass discipline (F42)
- Plugin registration (resolve_*/iter_* return what's expected)

**Missing:**
- **Cross-impl equivalence** — when two impls satisfy the same Protocol (FilesystemBronzeStore + StreamingBronzeStore today; will multiply as plugins land), there's no contract test that drives both against the same input and asserts equivalent outputs at the Protocol surface.
- **Plugin discovery edge cases** — what if two entry points register the same name? What if an entry point's factory raises? What if the entry point points at a module that's no longer importable?

**New tests to add (3):**
1. `test_bronze_store_cross_impl_equivalence` — both bronze impls receive the same write inputs; their `.replay()` outputs agree on every Protocol field except `raw_path` (which is intentionally store-specific). Sabotage-proof: change StreamingBronzeStore to swap source_name/item_id → equivalence assertion fails.
2. `test_extractor_registry_handles_duplicate_entry_points` — register two extractors under the same name; assert one wins deterministically (the alphabetically-first entry_points value, or last-loaded — pin which). Sabotage-proof: remove the dedup → both register and resolve_extractor returns one indeterminately.
3. `test_extractor_registry_handles_broken_factory` — entry point whose factory raises ImportError at first call; assert `resolve_extractor(name)` propagates with a fix-pointer.

### 3.2 Integration layer (current: 137 files, 124 marker-decorated)

**Catching well:**
- Factory-composed pipelines exercised through `build_*` (F47-clean)
- Per-connector sync E2E inside an `:memory:` SQLite + tmp_path bronze root
- Round-trip ingest → search assertions

**Missing:**
- **Scripted-failure connectors:** only `test_worker_connector_sync.py::test_failing_connector_logged_and_loop_continues` exists; needs per-failure-class coverage.
- **Resource-constrained scratch:** the markitdown tmpfile leak would have been caught by an integration test that drives the extractor against a tiny tmpfs scratch dir.
- **Chunker pathological inputs:** Bug B's oversized paragraph case had no integration cover; the unit tests against `DefaultSilverProcessor.process` cover happy-path only.
- **Worker lifecycle:** pause → resume, SIGTERM → restart, concurrent reextract + sync.

**New tests to add (5):**
1. `test_connector_pipeline_scripted_failure_at_item_n` — connector yields 10 items, fails fetch at item 5; assert items 0-4 bronze-written + chunks committed, dead_letter has row for item 5, items 6-9 still processed.
2. `test_silver_pathological_paragraph_shapes` — paragraph 2×/5×/20× target chunk size, single oversized sentence, single oversized word; assert chunk count > 1 and max chunk length within budget for each.
3. `test_extractor_under_tmpfs_pressure` — drive `MarkitdownExtractor` against `scratch_dir=tmp_path` populated to capacity beforehand; assert cleanup discipline holds (no leaked tmpfile placeholders).
4. `test_reextract_handles_missing_bronze_file` — pre-state: dead_letter row + bronze_records row + raw file UNLINKED; assert reextract counts `skipped_no_bronze` and doesn't blow up.
5. `test_connector_pipeline_mid_batch_writer_failure_rolls_back_chunk` — connector yields 100 items, chunk_writer fails at item 75; assert items 0-49 committed (first chunk boundary), items 50-99 rolled back, cursor at item 49.

### 3.3 BDD layer (current: 141 features, 134 binding modules, 177 marker-decorated)

**Catching well:**
- Per-extractor happy + claim-by-mime + version assertions
- Per-connector happy paths + cursor semantics
- Provider plugin parity (F28)

**Missing:**
- **Connector pipeline failure scenarios** — `connector_pipeline.feature` likely exists; checking what its @error coverage looks like.
- **Operator recovery scenarios** — Bug D-style "after a fix lands, run reextract" has no BDD coverage despite the CLI subcommand existing.
- **Worker lifecycle scenarios** — preflight, pause/resume, maintenance tick — some exist; cross-checking which.
- **Disk/scratch resource scenarios** — none.

**New scenarios to add (3 features × multiple scenarios = 8):**
1. `connector_pipeline_failure_modes.feature` (NEW) — 4 scenarios:
   - One item fetch raises → siblings still process, dead_letter has the failed item
   - Extractor raises on one item → siblings still process
   - chunk_writer raises mid-batch → that chunk rolls back, prior chunks remain
   - Connector raises during list_changes iteration → batch surfaces the error to worker loop
2. `worker_reextract_recovery.feature` (NEW) — 2 scenarios:
   - Operator runs reextract after a Dockerfile fix; dead-letter clears
   - Operator runs reextract dry-run; nothing commits, counts are accurate
3. `silver_oversized_inputs.feature` (NEW) — 2 scenarios:
   - Paragraph 5× target size → split at sentence boundary, max chunk under budget
   - Single word 2× target size → split at character boundary, max chunk under budget

### 3.4 E2E layer (current: 20 files, 16 marker-decorated)

**Catching well:**
- Happy-path composed-production-path for the alpha + each connector + topology
- Maintenance loop + bronze_ttl_gc

**Missing:**
- **Failure-mode E2E.** Every existing E2E is `composed_*_path.py` happy-path single-pass. None of these would catch the 2026-05-26 dogfood disk-space cascade because they don't operate under resource pressure.
- **Multi-batch scale.** Existing E2E typically ingests 2-3 documents. The 8,783-item dogfood revealed scale-dependent bugs (per-batch commit boundary effects, bronze growth, dead-letter cascade) that small-N tests can't surface.
- **Recovery composition.** No E2E exercises "ingest → fail → reextract → success" in one composed run.

**New tests to add (2):**
1. `test_composed_resource_pressure_path.py` — full composed pipeline against a tmp_path-rooted constrained scratch dir (manually filled before run); assert (a) no cascade failure, (b) dead-letter routes the items, (c) cleanup discipline holds (no leaked tmpfiles).
2. `test_composed_reextract_recovery_path.py` — composed pipeline runs against a synthetic corpus, an extractor scripted to fail on first pass, the operator-equivalent reextract path (built via the same factory), then a final retry-with-fixed-extractor that recovers. Asserts the full recovery composition works end-to-end against real production wiring.

## 4. Test-construction discipline (mechanical)

Each new test follows these mechanical rules (all enforced by the F-rules + manual sabotage):

1. **F1-clean**: no `@patch` on kairix internals, no `monkeypatch.setattr` on kairix modules. Stdlib monkeypatch (e.g. `tempfile`) allowed but discouraged — prefer constructor seams.
2. **F46-clean (BDD)**: step impls invoke at call-graph depth ≤ 2 through factory or CLI.
3. **F47-clean (integration)**: multi-component pipelines constructed via `kairix.core.factory.build_*` with `paths=FakePaths(...)`.
4. **F48-clean (E2E)**: full config → factory → ingest → query → assert composition; carries `@pytest.mark.e2e`.
5. **Canonical Fakes**: reach for `tests/fakes.py` (61 classes) before defining new inline `_Stub` classes. New Fakes added there with rationale.
6. **Sabotage proof for every test**: mutate production to remove the asserted invariant, observe test fails, restore, observe pass. The mutation + outcome recorded inline in the docstring.

## 5. Execution plan + defect-discovery strategy

The implementation phase runs in three waves:

**Wave 1 — Contract + Integration (highest ROI):** Tests 1-3 contract + 1-5 integration. Failure-mode integration tests are most likely to surface real bugs because they exercise production composition paths under stress.

**Wave 2 — E2E (composition-level surface):** Tests 1-2 e2e. These take longer to write because of fixture setup, but each catches a class of defects unit/integration can't.

**Wave 3 — BDD (specification-level surface):** 8 new scenarios across 3 features. Written last because they depend on the integration-layer plumbing being in place.

**Defect discovery rules:**
- Run each new test against current production code. **A green test means production is robust to that failure mode.**
- A failing new test = potential defect. Investigate before "fixing" the test. The test might be wrong; the production code might be wrong; either way the discovery is real.
- Document each failure with: (a) what production does today, (b) what the test asserts, (c) decision (fix prod, fix test, accept and document tradeoff).

## 6. Time budget

Targeting 4-5 hours of focused work in the window before the reextract finishes draining:

- **30 min** — Plan doc (this file) + landscape inventory + test enumeration
- **90 min** — Wave 1 (3 contract + 5 integration tests)
- **60 min** — Wave 2 (2 E2E tests)
- **60 min** — Wave 3 (8 BDD scenarios across 3 features)
- **45 min** — Run new suite, triage failures, record findings
- **30 min** — Write up report: what shipped, what defects were found, what's still hypothetical coverage

Each commit follows safe-commit discipline. Sabotage proofs executed and recorded. Defects found get GH issues filed during cherry-pick per `feedback_no_zero_bugs_papering`.

## 7. Non-goals (explicitly excluded)

- **Unit test additions.** Unit layer is already at 196 marker-decorated tests; the gap is upstream of unit. Excluded.
- **F30 outcome test paydown.** Real gap (only 1 file today) but separate paydown stream owned by Wave 0.
- **Backfill F43 plugin contract tests** for plugins missing them. Tracked separately (F43 now runs over the full tree with no baseline).
- **Migrating `tests/test_*.py` (14 root-level files) into `tests/unit/`.** One-day cleanup that's orthogonal to coverage resilience.
- **Mutation testing tooling** (e.g. mutmut, cosmic-ray). The sabotage discipline is the manual proxy; introducing automated mutation testing has its own cost/noise tradeoffs and is a separate decision.

## 8. Open questions

1. **Should BDD failure-mode features cross-cut connectors or be per-connector?** I'll start with `connector_pipeline_failure_modes.feature` (cross-cut, generic) because the failure modes are framework-level, not connector-specific. Per-connector failure modes (e.g. SharePoint Graph 429) live in the connector's own feature file.

2. **What's the canonical fixture for "constrained tmpfs"?** Linux has `mount -t tmpfs -o size=10M`. macOS doesn't have tmpfs natively (only hdiutil). Test cross-platform compat means using a temp directory pre-populated to capacity, OR skipping under `@pytest.mark.linux_only`. Decision: pre-fill to capacity, cross-platform.

3. **Do new failure-mode tests count toward the per-file 90% coverage floor (F7)?** Yes — they exercise production paths. They'll lift coverage on `kairix/extractors/markitdown/extractor.py`, `kairix/core/connectors/pipeline.py`, `kairix/core/connectors/silver.py` once they ship.

4. **Should the plan's "potentially-defect-finding" tests be marked separately?** No. Every test is potentially-defect-finding when first written. The dogfood-derived class taxonomy ensures the tests target real production-observed failure modes rather than hypothetical ones.
